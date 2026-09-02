"""flutter_decompile.report -- what was reconstructed, what was stubbed, and why.

The report is the part of the tool that keeps it honest.  Two numbers matter and
they must never be blended into one "percent decompiled":

  STRUCTURE coverage   how many declared members came out with a real (recovered
                       or inferred) name.  This is genuinely high.
  BEHAVIOUR coverage   how many method BODIES are Dart again.  For an AOT
                       snapshot this is 0% by construction, and the report says
                       so in words, every time.

Everything else is a breakdown of where the holes are, so a human can decide
which files are worth hand-reconstructing first.
"""

from __future__ import annotations

import datetime
import json
import os
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Tuple

from .ir import (
    BodyStatus,
    ClassIR,
    Confidence,
    LibraryIR,
    MethodKind,
    ProgramIR,
    TOOL_NAME,
    TOOL_VERSION,
)
from .infer import InferenceResult
from .emit import EmitResult

DART_IDENT_OK = __import__("re").compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


# ---------------------------------------------------------------------------
# counting
# ---------------------------------------------------------------------------


@dataclass
class LibraryStats:
    url: str
    package: str
    asm_file: Optional[str] = None
    classes: int = 0
    enums: int = 0
    enum_values: int = 0
    enum_gaps: int = 0
    methods: int = 0
    methods_stubbed: int = 0
    methods_reconstructed: int = 0
    closures: int = 0
    fields: int = 0
    fields_recovered: int = 0
    fields_inferred: int = 0
    fields_unknown: int = 0
    params_unknown_methods: int = 0
    unparsed_lines: int = 0
    #: facts the emitter documents in comments because Dart cannot legally
    #: re-declare them: enum payload slots and enum constructors.
    enum_payload_slots: int = 0
    methods_not_emitted: int = 0

    @property
    def named_fields(self) -> int:
        return self.fields_recovered + self.fields_inferred

    @property
    def field_name_coverage(self) -> float:
        return (self.named_fields / self.fields) if self.fields else 1.0

    @property
    def body_coverage(self) -> float:
        return (self.methods_reconstructed / self.methods) if self.methods else 0.0

    def to_json(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["field_name_coverage"] = round(self.field_name_coverage, 4)
        d["body_coverage"] = round(self.body_coverage, 4)
        return d


def _count_library(lib: LibraryIR) -> LibraryStats:
    st = LibraryStats(url=lib.url, package=lib.package, asm_file=lib.asm_file)
    st.unparsed_lines = len(lib.unparsed)
    for cls in lib.all_classes():
        if cls.is_enum:
            st.enums += 1
            st.enum_values += len(cls.enum_values)
            st.enum_gaps += len(cls.enum_ordinal_gaps())
        elif not cls.is_library_scope:
            st.classes += 1
        for f in cls.fields.values():
            if cls.is_enum and f.offset in (0x8, 0x10):
                continue      # `index` / `_name` are implicit in a Dart enum
            if cls.is_enum:
                st.enum_payload_slots += 1
            st.fields += 1
            if f.recovered_name:
                st.fields_recovered += 1
            elif f.inferred_name:
                st.fields_inferred += 1
            else:
                st.fields_unknown += 1
        for m in cls.methods:
            st.methods += 1
            if m.kind == MethodKind.CLOSURE:
                st.closures += 1
            if m.body_status == BodyStatus.RECONSTRUCTED and m.body_dart:
                st.methods_reconstructed += 1
            else:
                st.methods_stubbed += 1
            if not m.params_known:
                st.params_unknown_methods += 1
            if cls.is_enum and m.kind in (MethodKind.CONSTRUCTOR, MethodKind.FACTORY):
                st.methods_not_emitted += 1
    return st


# ---------------------------------------------------------------------------
# invariants (--strict)
# ---------------------------------------------------------------------------


@dataclass
class Violation:
    severity: str          # "error" | "warning"
    rule: str
    where: str
    detail: str

    def to_json(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    def render(self) -> str:
        return "[%s] %s @ %s: %s" % (self.severity.upper(), self.rule, self.where, self.detail)


def check_invariants(program: ProgramIR, emit: Optional[EmitResult] = None) -> List[Violation]:
    """Structural checks that must hold for the output to be trustworthy."""
    v: List[Violation] = []

    for lib in program.libraries:
        seen_classes: Dict[str, int] = {}
        for cls in lib.all_classes():
            seen_classes[cls.name] = seen_classes.get(cls.name, 0) + 1
        for name, n in seen_classes.items():
            if n > 1:
                v.append(Violation("error", "duplicate_class", "%s::%s" % (lib.url, name),
                                   "%d classes share this name in one library" % n))

    for lib, cls in program.all_classes():
        if not cls.name:
            v.append(Violation("error", "unnamed_class", lib.url, "class with an empty name"))
        if cls.is_enum:
            ordinals: Dict[int, int] = {}
            names: Dict[str, int] = {}
            for val in cls.enum_values:
                ordinals[val.ordinal] = ordinals.get(val.ordinal, 0) + 1
                names[val.name] = names.get(val.name, 0) + 1
                if not val.name:
                    v.append(Violation("error", "enum_value_unnamed", cls.name,
                                       "ordinal %d has no name" % val.ordinal))
            for o, n in ordinals.items():
                if n > 1:
                    v.append(Violation("error", "enum_ordinal_dup", cls.name,
                                       "ordinal %d appears %d times" % (o, n)))
            for nm, n in names.items():
                if n > 1:
                    v.append(Violation("error", "enum_name_dup", cls.name,
                                       "value name %r appears %d times" % (nm, n)))
            gaps = cls.enum_ordinal_gaps()
            if gaps:
                v.append(Violation("warning", "enum_ordinal_gap", cls.name,
                                   "no const instance at ordinal(s) %s: tree-shaken or never "
                                   "const-constructed" % ", ".join(str(g) for g in gaps)))

        for f in cls.fields.values():
            if f.inferred_name and not f.evidence:
                v.append(Violation("error", "inference_without_evidence",
                                   "%s.%s" % (cls.name, f.name),
                                   "an inferred name must carry its evidence trail"))
            if f.inferred_name and f.name_confidence == Confidence.UNKNOWN:
                v.append(Violation("error", "inference_without_confidence",
                                   "%s.%s" % (cls.name, f.name), "confidence is UNKNOWN"))
            if f.recovered_name and f.inferred_name:
                v.append(Violation("warning", "name_conflict", "%s@0x%x" % (cls.name, f.offset),
                                   "slot has both a recovered and an inferred name"))
            if f.has_real_name and not DART_IDENT_OK.match(f.name):
                v.append(Violation("error", "bad_identifier", "%s.%s" % (cls.name, f.name),
                                   "not a legal Dart identifier"))

        for m in cls.methods:
            if m.address is None and (m.source is None or m.source.asm_file is None):
                v.append(Violation("error", "unlocatable_method", "%s.%s" % (cls.name, m.name),
                                   "no address and no asm reference: the stub message could not "
                                   "name where the body lives"))
            if m.body_status == BodyStatus.RECONSTRUCTED and not m.body_dart:
                v.append(Violation("error", "empty_reconstructed_body",
                                   "%s.%s" % (cls.name, m.name),
                                   "marked RECONSTRUCTED with no body text"))

    if emit is not None:
        for f in emit.files:
            if "RECONSTRUCTED FROM A FLUTTER AOT SNAPSHOT" not in f.text and \
               "reconstruction manifest" not in f.text:
                v.append(Violation("error", "missing_header", f.rel_path,
                                   "emitted file has no reconstruction header"))
        stubbed = emit.stats.get("methods_stubbed", 0)
        methods = emit.stats.get("methods", 0)
        if methods and stubbed != methods - emit.stats.get("methods_reconstructed", 0):
            v.append(Violation("error", "stub_accounting", "<emit>",
                               "methods (%d) != stubbed (%d) + reconstructed (%d)"
                               % (methods, stubbed, emit.stats.get("methods_reconstructed", 0))))

    if program.meta.parse_lines_total and program.meta.parse_coverage < 1.0:
        v.append(Violation("warning", "parse_coverage", "<parser>",
                           "%d of %d asm lines were not understood by the parser"
                           % (program.meta.parse_lines_unparsed, program.meta.parse_lines_total)))
    return v


# ---------------------------------------------------------------------------
# report building
# ---------------------------------------------------------------------------


def build_report(
    program: ProgramIR,
    inference: Optional[InferenceResult] = None,
    emit: Optional[EmitResult] = None,
) -> Dict[str, Any]:
    libs = [_count_library(l) for l in program.libraries]
    totals = LibraryStats(url="<all>", package="<all>")
    for st in libs:
        totals.classes += st.classes
        totals.enums += st.enums
        totals.enum_values += st.enum_values
        totals.enum_gaps += st.enum_gaps
        totals.methods += st.methods
        totals.methods_stubbed += st.methods_stubbed
        totals.methods_reconstructed += st.methods_reconstructed
        totals.closures += st.closures
        totals.fields += st.fields
        totals.fields_recovered += st.fields_recovered
        totals.fields_inferred += st.fields_inferred
        totals.fields_unknown += st.fields_unknown
        totals.params_unknown_methods += st.params_unknown_methods
        totals.unparsed_lines += st.unparsed_lines
        totals.enum_payload_slots += st.enum_payload_slots
        totals.methods_not_emitted += st.methods_not_emitted

    violations = check_invariants(program, emit)
    report: Dict[str, Any] = {
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "meta": program.meta.to_json(),
        "totals": totals.to_json(),
        "libraries": [st.to_json() for st in sorted(libs, key=lambda s: s.url)],
        "inference": inference.to_json() if inference else None,
        "emit": ({"files": [f.rel_path for f in emit.files],
                  "stats": emit.stats,
                  "warnings": emit.warnings} if emit else None),
        "violations": [x.to_json() for x in violations],
        "coverage": {
            "structure_field_names": round(
                (totals.fields_recovered + totals.fields_inferred) / totals.fields, 4
            ) if totals.fields else 1.0,
            "behaviour_bodies": round(
                totals.methods_reconstructed / totals.methods, 4
            ) if totals.methods else 0.0,
            "parse": round(program.meta.parse_coverage, 4),
        },
        "honest_statement": (
            "Method bodies are ARM64 machine code in the snapshot. This tool does not "
            "lift them, so behaviour coverage is %d%% and every emitted body throws "
            "UnimplementedError naming its address. Structure coverage says how many "
            "members came out with a real name, nothing more."
            % round(
                (totals.methods_reconstructed / totals.methods * 100) if totals.methods else 0
            )
        ),
    }
    return report


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _bar(fraction: float, width: int = 20) -> str:
    filled = int(round(max(0.0, min(1.0, fraction)) * width))
    return "#" * filled + "." * (width - filled)


def render_markdown(report: Dict[str, Any]) -> str:
    t = report["totals"]
    meta = report["meta"]
    cov = report["coverage"]
    out: List[str] = []
    out.append("# %s %s reconstruction report" % (report["tool"]["name"], report["tool"]["version"]))
    out.append("")
    out.append("Generated %s" % report["generated_at"])
    out.append("")
    out.append("## Read this first")
    out.append("")
    out.append("This tool does **not** decompile Dart. An AOT snapshot stores machine code,")
    out.append("not source, so no statement, local name, comment or import can be recovered.")
    out.append("What it reconstructs is a **compilable skeleton with documented holes**.")
    out.append("")
    out.append("> " + report["honest_statement"])
    out.append("")
    out.append("## Input")
    out.append("")
    out.append("| field | value |")
    out.append("|---|---|")
    for key in ("input_name", "abi", "dart_version", "snapshot_hash", "version_signal",
                "blutter_version", "blutter_out", "obfuscated"):
        if meta.get(key) is not None:
            out.append("| %s | `%s` |" % (key, meta[key]))
    parse_pct = cov["parse"] * 100
    parse_txt = ("just under 100%% (%.4f%%)" % parse_pct
                 if 99.995 <= parse_pct < 100.0 else "%.2f%%" % parse_pct)
    out.append("| parse coverage | %s (%s unparsed of %s lines) |"
               % (parse_txt, meta.get("parse_lines_unparsed", 0),
                  meta.get("parse_lines_total", 0)))
    out.append("")
    out.append("## Coverage")
    out.append("")
    out.append("```")
    out.append("structure (field names)  %s  %.1f%%   %d recovered + %d inferred of %d slots"
               % (_bar(cov["structure_field_names"]), cov["structure_field_names"] * 100,
                  t["fields_recovered"], t["fields_inferred"], t["fields"]))
    out.append("behaviour (method bodies)%s  %.1f%%   %d of %d bodies are Dart again"
               % (_bar(cov["behaviour_bodies"]), cov["behaviour_bodies"] * 100,
                  t["methods_reconstructed"], t["methods"]))
    out.append("```")
    out.append("")
    out.append("| what | count |")
    out.append("|---|---:|")
    out.append("| libraries | %d |" % len(report["libraries"]))
    out.append("| classes | %d |" % t["classes"])
    out.append("| enums | %d (%d values, %d ordinal gaps) |"
               % (t["enums"], t["enum_values"], t["enum_gaps"]))
    out.append("| methods | %d (%d closures) |" % (t["methods"], t["closures"]))
    out.append("| methods emitting a throwing stub | %d |" % t["methods_stubbed"])
    out.append("| methods with an UNKNOWN parameter list | %d |" % t["params_unknown_methods"])
    out.append("| enum payload slots (documented, not re-declared) | %d |"
               % t["enum_payload_slots"])
    out.append("| enum constructors (documented, not re-declared) | %d |"
               % t["methods_not_emitted"])
    out.append("| field slots | %d |" % t["fields"])
    out.append("| &nbsp;&nbsp;with a RECOVERED name | %d |" % t["fields_recovered"])
    out.append("| &nbsp;&nbsp;with an INFERRED name | %d |" % t["fields_inferred"])
    out.append("| &nbsp;&nbsp;offset only (UNKNOWN) | %d |" % t["fields_unknown"])
    out.append("")
    out.append("These are counts of **facts in the snapshot**, not of lines emitted.")
    out.append("`RECONSTRUCTION.txt` counts emitted declarations and is therefore lower:")
    out.append("enum payload slots and enum constructors cannot legally be re-declared in")
    out.append("a Dart enum, so the emitter documents them in comments instead.")
    out.append("")

    inf = report.get("inference")
    if inf:
        out.append("## Field-name inference")
        out.append("")
        out.append("Mode `%s`, floor `%s`. %d applied, %d rejected, of %d instance slots."
                   % (inf["mode"], inf["min_confidence"], inf["applied"], inf["rejected"],
                      inf["fields_considered"]))
        out.append("")
        if inf["by_rule"]:
            out.append("| rule | names applied |")
            out.append("|---|---:|")
            for rule, n in sorted(inf["by_rule"].items(), key=lambda kv: -kv[1]):
                out.append("| `%s` | %d |" % (rule, n))
            out.append("")
        if inf["by_confidence"]:
            out.append("| confidence | names applied |")
            out.append("|---|---:|")
            for conf, n in sorted(inf["by_confidence"].items()):
                out.append("| %s | %d |" % (conf, n))
            out.append("")
        rejected = [d for d in inf["decisions"] if not d["accepted"]]
        if rejected:
            out.append("<details><summary>Rejected candidates (%d)</summary>" % len(rejected))
            out.append("")
            for d in rejected[:200]:
                out.append("- `%s` slot `%s`: proposed `%s` -- %s"
                           % (d["class"], d["offset"], d["winner"], d["reason"]))
            if len(rejected) > 200:
                out.append("- ... %d more in report.json" % (len(rejected) - 200))
            out.append("")
            out.append("</details>")
            out.append("")

    out.append("## Per-library")
    out.append("")
    out.append("| library | classes | enums | methods | stubbed | field slots | named | unknown |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for st in report["libraries"]:
        out.append("| `%s` | %d | %d | %d | %d | %d | %d | %d |"
                   % (st["url"], st["classes"], st["enums"], st["methods"],
                      st["methods_stubbed"], st["fields"],
                      st["fields_recovered"] + st["fields_inferred"], st["fields_unknown"]))
    out.append("")

    worst = sorted(report["libraries"],
                   key=lambda s: (-s["fields_unknown"], -s["methods_stubbed"]))[:10]
    if worst and worst[0]["fields_unknown"]:
        out.append("### Where hand-reconstruction pays off first")
        out.append("")
        out.append("Libraries with the most unnamed field slots:")
        out.append("")
        for st in worst:
            if not st["fields_unknown"]:
                continue
            out.append("- `%s`: %d unnamed slot(s), %d stubbed method(s)"
                       % (st["url"], st["fields_unknown"], st["methods_stubbed"]))
        out.append("")

    viol = report["violations"]
    out.append("## Invariant checks")
    out.append("")
    errs = [x for x in viol if x["severity"] == "error"]
    warns = [x for x in viol if x["severity"] == "warning"]
    if not viol:
        out.append("All structural invariants hold.")
    else:
        out.append("%d error(s), %d warning(s)." % (len(errs), len(warns)))
        out.append("")
        for x in errs + warns:
            out.append("- **%s** `%s` at `%s`: %s"
                       % (x["severity"], x["rule"], x["where"], x["detail"]))
    out.append("")
    return "\n".join(out)


def render_text_summary(report: Dict[str, Any]) -> str:
    t = report["totals"]
    cov = report["coverage"]
    return (
        "%s %s: %d libraries, %d classes, %d enums (%d values), %d methods "
        "(%d stubbed), %d field slots (%d recovered, %d inferred, %d unknown). "
        "Structure %.1f%%, behaviour %.1f%%."
        % (report["tool"]["name"], report["tool"]["version"], len(report["libraries"]),
           t["classes"], t["enums"], t["enum_values"], t["methods"], t["methods_stubbed"],
           t["fields"], t["fields_recovered"], t["fields_inferred"], t["fields_unknown"],
           cov["structure_field_names"] * 100, cov["behaviour_bodies"] * 100)
    )


def write_reports(out_dir: str, report: Dict[str, Any], fmt: str = "both") -> List[str]:
    written: List[str] = []
    os.makedirs(out_dir, exist_ok=True)
    if fmt in ("json", "both"):
        p = os.path.join(out_dir, "report.json")
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(report, fh, indent=2, sort_keys=False, default=str)
            fh.write("\n")
        written.append(p)
    if fmt in ("md", "both"):
        p = os.path.join(out_dir, "report.md")
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render_markdown(report))
        written.append(p)
    return written


def strict_exit_code(report: Dict[str, Any]) -> int:
    return 1 if any(x["severity"] == "error" for x in report["violations"]) else 0


__all__ = [
    "LibraryStats", "Violation", "check_invariants", "build_report",
    "render_markdown", "render_text_summary", "write_reports", "strict_exit_code",
]
