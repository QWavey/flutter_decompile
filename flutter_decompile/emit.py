"""flutter_decompile.emit -- IR to a Dart source tree.

Emission contract (these are hard rules, not style preferences):

1.  Every emitted file starts with a header saying it was reconstructed from an
    AOT snapshot and what that means.  No exceptions, including generated
    support files.
2.  Enum values are emitted EXACTLY: the recovered name at the recovered
    ordinal.  A missing ordinal becomes a loudly named placeholder so the
    remaining ordinals keep their real values.
3.  String constants are emitted verbatim (escaped only so Dart re-parses them
    to the same bytes).
4.  A method body that could not be reconstructed -- which today is every
    method -- emits a body that THROWS UnimplementedError naming the class,
    method, code address and asm file:line.  Never `{}`, never `return null;`,
    never a plausible-looking fake.
5.  An inferred field name carries a doc comment with the evidence and the
    confidence.  An un-inferred one keeps `field_<offset>` and says why.
6.  A type that cannot be resolved to something the emitted tree actually
    declares becomes `dynamic` with the original in a comment -- the output has
    to analyze cleanly, and a dangling type name would be a lie about what was
    recovered.
"""

from __future__ import annotations

import datetime
import os
import re
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .ir import (
    CORE_TYPES,
    SDK_TYPE_IMPORTS,
    TOOL_NAME,
    TOOL_VERSION,
    BodyStatus,
    ClassIR,
    Confidence,
    ConstObjectIR,
    EnumValueIR,
    FieldIR,
    LibraryIR,
    MethodIR,
    MethodKind,
    Modifier,
    ParamIR,
    ProgramIR,
    escape_dart_string,
    sanitize_identifier,
    split_generic,
    vm_to_dart,
)

IGNORE_LINE = (
    "// ignore_for_file: camel_case_types, non_constant_identifier_names, "
    "unused_field, unused_element, unused_import, constant_identifier_names, "
    "library_private_types_in_public_api, slash_for_doc_comments"
)


# ---------------------------------------------------------------------------
# options / results
# ---------------------------------------------------------------------------


@dataclass
class EmitOptions:
    out_dir: str = "out"
    primary_packages: Tuple[str, ...] = ()      # emitted under lib/, others under packages/
    emit_asm_refs: bool = True
    emit_const_pool: bool = True
    emit_evidence: bool = True
    write: bool = True
    line_ending: str = "\n"


@dataclass
class EmittedFile:
    rel_path: str
    abs_path: str
    library_url: Optional[str]
    text: str
    stats: Dict[str, int] = dc_field(default_factory=dict)


@dataclass
class EmitResult:
    files: List[EmittedFile] = dc_field(default_factory=list)
    warnings: List[str] = dc_field(default_factory=list)
    stats: Dict[str, int] = dc_field(default_factory=dict)

    def bump(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    def file_map(self) -> Dict[str, str]:
        return {f.rel_path: f.text for f in self.files}


# ---------------------------------------------------------------------------
# type resolution
# ---------------------------------------------------------------------------


def sanitize_type_name(name: str) -> str:
    """`_Foo&Bar&Baz` -> `_Foo$Bar$Baz`; keeps the flattened identity visible."""
    out = re.sub(r"[^A-Za-z0-9_$]", "$", name or "")
    if not out:
        return r"$anon"
    if out[0].isdigit():
        out = "T" + out
    return out


class TypeResolver:
    """Decides how a recovered VM type may appear in emitted Dart.

    A type name is only emitted when the tree actually declares it (or dart:core
    / a known dart: SDK type provides it).  Anything else degrades to `dynamic`
    with the original preserved in a trailing comment.
    """

    def __init__(self, program: ProgramIR, plan: Dict[str, str]):
        self.program = program
        self.plan = plan                       # library url -> emitted rel path
        self.declared: Dict[str, List[str]] = {}
        for lib, cls in program.all_classes():
            if cls.is_library_scope:
                continue
            self.declared.setdefault(sanitize_type_name(cls.name), []).append(lib.url)

    def resolve(self, raw: str, lib: LibraryIR) -> Tuple[str, Optional[str], Set[str]]:
        """Return (dart_text, note, imports)."""
        dart, note = vm_to_dart(raw)
        text, imports, notes = self._resolve_dart(dart, lib)
        all_notes = [n for n in ([note] if note else []) + notes if n]
        return text, ("; ".join(all_notes) if all_notes else None), imports

    # -- internals ----------------------------------------------------------
    def _resolve_dart(self, dart: str, lib: LibraryIR) -> Tuple[str, Set[str], List[str]]:
        imports: Set[str] = set()
        notes: List[str] = []
        dart = (dart or "").strip()
        if not dart:
            return "dynamic", imports, ["empty type"]
        nullable = dart.endswith("?")
        if nullable:
            dart = dart[:-1]
        base, args = split_generic(dart)
        base = sanitize_type_name(base.strip()) if base.strip() not in ("dynamic", "void") else base.strip()

        if base in ("dynamic", "void", "Never"):
            resolved = base
        elif base in CORE_TYPES:
            resolved = base
        elif base in SDK_TYPE_IMPORTS:
            imports.add(SDK_TYPE_IMPORTS[base])
            resolved = base
        elif base in self.declared:
            owners = self.declared[base]
            if len(owners) > 1:
                return "dynamic", imports, ["type `%s` is declared in %d libraries (ambiguous)"
                                            % (base, len(owners))]
            owner_url = owners[0]
            if owner_url != lib.url:
                target = self.plan.get(owner_url)
                if target is None:
                    return "dynamic", imports, ["type `%s` lives in a library that was not emitted"
                                                % base]
                imports.add("::" + owner_url)     # resolved to a relative path later
            resolved = base
        else:
            return "dynamic", imports, ["unresolved type `%s`" % base]

        if args:
            conv: List[str] = []
            for a in args:
                t, imp, n = self._resolve_dart(a, lib)
                conv.append(t)
                imports |= imp
                notes.extend(n)
            resolved = "%s<%s>" % (resolved, ", ".join(conv))
        if nullable:
            resolved += "?"
        return resolved, imports, notes


# ---------------------------------------------------------------------------
# the emitter
# ---------------------------------------------------------------------------


class DartEmitter:
    def __init__(self, program: ProgramIR, options: Optional[EmitOptions] = None):
        self.program = program
        self.opt = options or EmitOptions()
        self.program.link()
        self.plan: Dict[str, str] = {}
        for lib in program.libraries:
            self.plan[lib.url] = self._plan_path(lib)
        self.types = TypeResolver(program, self.plan)
        self.result = EmitResult()

    # -- paths --------------------------------------------------------------
    def _plan_path(self, lib: LibraryIR) -> str:
        rel = lib.rel_path.replace("\\", "/").lstrip("/")
        if not self.opt.primary_packages or lib.package in self.opt.primary_packages:
            return "lib/" + rel
        return "packages/%s/lib/%s" % (sanitize_identifier(lib.package), rel)

    def _import_for(self, from_rel: str, to_url: str) -> Optional[str]:
        to_rel = self.plan.get(to_url)
        if not to_rel:
            return None
        rel = os.path.relpath(to_rel, os.path.dirname(from_rel)).replace("\\", "/")
        if not rel.startswith("."):
            rel = "./" + rel
        return rel

    # -- public API ---------------------------------------------------------
    def emit_program(self) -> EmitResult:
        for lib in self.program.libraries:
            self._emit_library(lib)
        if self.opt.emit_const_pool and (self.program.orphan_consts or self.program.pool_strings):
            self._emit_const_pool()
        self._emit_manifest()
        if self.opt.write:
            for f in self.result.files:
                os.makedirs(os.path.dirname(f.abs_path), exist_ok=True)
                with open(f.abs_path, "w", encoding="utf-8", newline=self.opt.line_ending) as fh:
                    fh.write(f.text)
        return self.result

    # -- headers ------------------------------------------------------------
    def _banner(self, lib: Optional[LibraryIR], extra: Sequence[str] = ()) -> List[str]:
        meta = self.program.meta
        when = meta.generated_at or datetime.datetime.now().isoformat(timespec="seconds")
        lines = [
            "// " + "=" * 74,
            "//  RECONSTRUCTED FROM A FLUTTER AOT SNAPSHOT -- THIS IS NOT THE ORIGINAL SOURCE.",
            "//  %s %s   generated %s" % (TOOL_NAME, TOOL_VERSION, when),
            "//",
            "//  A Dart AOT snapshot stores machine code, not source. There is no AST in",
            "//  it, so there are no statements, no local names, no comments and no",
            "//  imports to recover. What follows is a SKELETON with documented holes.",
            "//",
            "//    RECOVERED  present in the snapshot itself: class and method names,",
            "//               enum names + ordinals, string constants, static field",
            "//               names, class ids, instance sizes.",
            "//    INFERRED   rebuilt from evidence. Every inferred name below carries",
            "//               its evidence trail and a confidence level.",
            "//    UNKNOWN    destroyed. Instance field names appear as `field_<offset>`;",
            "//               every method body throws UnimplementedError naming its",
            "//               address, because the body is machine code, not Dart.",
            "//",
            "//  Absence is not evidence of absence: anything tree-shaken out of the",
            "//  build is simply not in the snapshot and therefore not in this file.",
            "//",
        ]
        if lib is not None:
            lines.append("//  library : %s" % lib.url)
            if lib.asm_file:
                lines.append("//  asm     : %s" % lib.asm_file)
        src_bits = [meta.input_name or "<unknown input>"]
        if meta.dart_version:
            src_bits.append("dart " + meta.dart_version)
        if meta.snapshot_hash:
            src_bits.append("snapshot " + meta.snapshot_hash)
        if meta.abi:
            src_bits.append(meta.abi)
        lines.append("//  source  : %s" % ", ".join(src_bits))
        if meta.obfuscated:
            lines.append("//  WARNING : the snapshot is OBFUSCATED. Names below are meaningless;")
            lines.append("//            only the structure is real.")
        for e in extra:
            lines.append("//  %s" % e)
        lines.append("// " + "=" * 74)
        lines.append("")
        lines.append(IGNORE_LINE)
        lines.append("")
        return lines

    # -- library ------------------------------------------------------------
    def _emit_library(self, lib: LibraryIR) -> None:
        rel = self.plan[lib.url]
        body: List[str] = []
        imports: Set[str] = set()
        stats: Dict[str, int] = {}

        if lib.top is not None:
            body.extend(self._emit_library_scope(lib, lib.top, imports, stats))
        for cls in lib.classes:
            if cls.is_enum:
                body.extend(self._emit_enum(lib, cls, imports, stats))
            else:
                body.extend(self._emit_class(lib, cls, imports, stats))
            body.append("")

        extra: List[str] = []
        if lib.unparsed:
            extra.append(
                "PARSE GAPS: %d line(s) of the asm file were not understood by the"
                % len(lib.unparsed)
            )
            extra.append("            parser and are listed in report.json (parse coverage < 100%).")
        for note in lib.notes:
            extra.append("note: " + note)

        head = self._banner(lib, extra)
        head.extend(self._render_imports(rel, imports))
        text = self.opt.line_ending.join(head + body).rstrip() + self.opt.line_ending
        self._add_file(rel, lib.url, text, stats)

    def _render_imports(self, from_rel: str, imports: Set[str]) -> List[str]:
        lines: List[str] = []
        sdk = sorted({i for i in imports if i.startswith("dart:")})
        libs = sorted({i[2:] for i in imports if i.startswith("::")})
        for i in sdk:
            lines.append("import '%s';" % i)
        for url in libs:
            rel = self._import_for(from_rel, url)
            if rel:
                lines.append("import '%s';" % rel)
                self.result.bump("imports_emitted")
        if lines:
            lines.append("")
        return lines

    def _add_file(self, rel: str, url: Optional[str], text: str, stats: Dict[str, int]) -> None:
        abs_path = os.path.join(self.opt.out_dir, rel.replace("/", os.sep))
        self.result.files.append(
            EmittedFile(rel_path=rel, abs_path=abs_path, library_url=url, text=text, stats=stats)
        )
        self.result.bump("files")

    # -- library scope (Blutter's `class :: {}`) ----------------------------
    def _emit_library_scope(
        self, lib: LibraryIR, top: ClassIR, imports: Set[str], stats: Dict[str, int]
    ) -> List[str]:
        out: List[str] = []
        out.append("// ---- library scope of %s -----------------------------------" % lib.url)
        out.append("// Blutter models top-level declarations as a pseudo-class `::`.")
        out.append("")
        for f in sorted(top.fields.values(), key=lambda x: x.offset):
            out.extend(self._emit_field(lib, top, f, imports, stats, indent="", top_level=True))
            out.append("")
        seen: Set[Tuple[str, str]] = set()
        for m in top.methods:
            out.extend(
                self._emit_method(lib, top, m, imports, stats, indent="", top_level=True, seen=seen)
            )
            out.append("")
        return out

    # -- classes ------------------------------------------------------------
    def _class_doc(self, cls: ClassIR) -> List[str]:
        doc: List[str] = []
        facts: List[str] = []
        if cls.class_id is not None:
            facts.append("class id %d" % cls.class_id)
        if cls.instance_size is not None:
            facts.append("instance size 0x%x" % cls.instance_size)
        if cls.field_offset_base is not None:
            facts.append("first field offset 0x%x" % cls.field_offset_base)
        if facts:
            doc.append("/// RECOVERED: %s." % ", ".join(facts))
        if cls.source and self.opt.emit_asm_refs:
            doc.append("/// asm: %s" % cls.source.label())
        if cls.has_const_ctor:
            doc.append(
                "/// The snapshot records a const constructor for this class, so its"
            )
            doc.append(
                "/// fields were almost certainly `final` in the original. `final` itself"
            )
            doc.append("/// is not recoverable, so it is not emitted.")
        if cls.mixin_of:
            doc.append("/// INFERRED: flattened mixin application of %s." % cls.mixin_of)
        for n in cls.notes:
            doc.append("/// note: %s" % n.replace("\n", " "))
        return doc

    def _emit_class(
        self, lib: LibraryIR, cls: ClassIR, imports: Set[str], stats: Dict[str, int]
    ) -> List[str]:
        out: List[str] = self._class_doc(cls)
        name = sanitize_type_name(cls.name)
        if name != cls.name:
            out.append("/// RECOVERED name in the snapshot: `%s`" % cls.name)
        header = "class %s" % name
        if cls.super_name and cls.super_name not in ("Object", None):
            sup, note, imps = self.types.resolve(cls.super_name, lib)
            imports |= imps
            if sup != "dynamic":
                header += " extends %s" % sup
            else:
                out.append(
                    "/// UNKNOWN supertype: snapshot says `extends %s`, which is not part of"
                    % cls.super_name
                )
                out.append("/// the emitted tree, so the `extends` clause was dropped.")
        out.append(header + " {")

        fields = sorted(cls.fields.values(), key=lambda x: x.offset)
        if not fields:
            out.append("  // No fields were listed for this class. Blutter only prints a")
            out.append("  // field slot when it could pin down the slot's VM type, so this")
            out.append("  // class may still have had fields.")
            out.append("")
        for f in fields:
            out.extend(self._emit_field(lib, cls, f, imports, stats, indent="  "))
            out.append("")

        seen: Set[Tuple[str, str]] = set()
        for m in cls.methods:
            out.extend(self._emit_method(lib, cls, m, imports, stats, indent="  ", seen=seen))
            out.append("")
        while out and out[-1] == "":
            out.pop()
        out.append("}")
        self.result.bump("classes")
        stats["classes"] = stats.get("classes", 0) + 1
        return out

    # -- enums --------------------------------------------------------------
    def _emit_enum(
        self, lib: LibraryIR, cls: ClassIR, imports: Set[str], stats: Dict[str, int]
    ) -> List[str]:
        out: List[str] = self._class_doc(cls)
        out.insert(
            0,
            "/// RECOVERED enum. Both the constant names and their ordinals survive in"
            "",
        )
        out.insert(1, "/// the snapshot's const object table (`Super!_Enum { off_8: ordinal,")
        out.insert(2, "/// off_10: name }`), so the values below are emitted exactly.")
        values = sorted(cls.enum_values, key=lambda v: v.ordinal)
        gaps = cls.enum_ordinal_gaps()
        if gaps:
            out.append(
                "/// ORDINAL GAPS at %s: no const instance with that ordinal exists in the"
                % ", ".join(str(g) for g in gaps)
            )
            out.append("/// snapshot. The value was tree-shaken or never const-constructed.")
            out.append("/// A loud placeholder keeps every other ordinal at its real value.")
        declared_slots = [f for f in sorted(cls.fields.values(), key=lambda x: x.offset)
                          if f.offset not in (0x8, 0x10)]
        if declared_slots:
            out.append(
                "/// Declared field slots on this enum (NOT emitted as Dart fields: an"
            )
            out.append("/// enum's instance fields must be final and initialised by a const")
            out.append("/// constructor, which is not recoverable):")
            for f in declared_slots:
                out.append(
                    "///   slot 0x%x : %s   name %s"
                    % (f.offset, f.vm_type,
                       ("%s (%s)" % (f.name, f.confidence.value)) if f.has_real_name
                       else "UNKNOWN")
                )
        payload_slots = sorted({o for v in values for o in v.extra})
        if payload_slots:
            out.append(
                "/// This enum carries const payload slots (%s). Their names are DESTROYED"
                % ", ".join("0x%x" % o for o in payload_slots)
            )
            out.append("/// and the constructor is not recoverable, so the payload is documented")
            out.append("/// per value below instead of being re-declared as enum fields.")

        name = sanitize_type_name(cls.name)
        out.append("enum %s {" % name)
        entries: List[str] = []
        by_ordinal = {v.ordinal: v for v in values}
        if not by_ordinal:
            out.append(
                "  $noConstInstances, // UNKNOWN: the snapshot holds no const instance of"
            )
            out.append(
                "  // this enum, so not one value name survived. A Dart enum cannot be"
            )
            out.append("  // empty, so this placeholder stands in for the whole value list.")
            out.append("}")
            self.result.bump("enums")
            self.result.bump("enums_without_values")
            stats["enums"] = stats.get("enums", 0) + 1
            return out
        top = max(by_ordinal)
        for i in range(top + 1):
            v = by_ordinal.get(i)
            if v is None:
                entries.append(
                    "  $missingOrdinal%d, // UNKNOWN: no const instance at ordinal %d" % (i, i)
                )
                stats["enum_gaps"] = stats.get("enum_gaps", 0) + 1
                self.result.bump("enum_gaps")
                continue
            comment_bits = ["ordinal %d" % v.ordinal]
            if v.obj_address:
                comment_bits.append(v.obj_address)
            for off, val in sorted(v.extra.items()):
                comment_bits.append("slot 0x%x = %s" % (off, _const_repr(val)))
            entries.append(
                "  %s, // RECOVERED: %s" % (sanitize_identifier(v.name), ", ".join(comment_bits))
            )
            stats["enum_values"] = stats.get("enum_values", 0) + 1
            self.result.bump("enum_values")
        out.extend(entries)

        real_methods = [
            m for m in cls.methods
            if m.kind not in (MethodKind.CONSTRUCTOR, MethodKind.FACTORY)
        ]
        skipped = [m for m in cls.methods if m not in real_methods]
        for m in skipped:
            out.append(
                "  // NOT EMITTED: %s `%s` @%s. A Dart enum's constructor must be const"
                % (m.kind.value, m.clean_name,
                   ("0x%x" % m.address) if m.address is not None else "?")
            )
            out.append(
                "  // and its arguments are not recoverable, so re-declaring it would be"
            )
            out.append("  // a guess. It stays a hole rather than a fake.")
            self.result.bump("enum_members_skipped")
        if real_methods:
            out.append("  ;")
            out.append("")
            seen: Set[Tuple[str, str]] = set()
            for m in real_methods:
                out.extend(self._emit_method(lib, cls, m, imports, stats, indent="  ", seen=seen))
                out.append("")
            while out and out[-1] == "":
                out.pop()
        out.append("}")
        self.result.bump("enums")
        stats["enums"] = stats.get("enums", 0) + 1
        return out

    # -- fields -------------------------------------------------------------
    def _emit_field(
        self,
        lib: LibraryIR,
        cls: ClassIR,
        f: FieldIR,
        imports: Set[str],
        stats: Dict[str, int],
        indent: str = "  ",
        top_level: bool = False,
    ) -> List[str]:
        if cls.is_enum and f.offset in (0x8, 0x10):
            return []      # `index` / `_name` are implicit in a Dart enum
        dart_type, note, imps = self.types.resolve(f.vm_type, lib)
        imports |= imps
        doc: List[str] = []

        if f.recovered_name:
            doc.append(
                "%s/// RECOVERED field name: the object pool stores it as"
                % indent
            )
            doc.append(
                "%s/// `Field <%s.%s>`%s."
                % (
                    indent,
                    cls.name if not cls.is_library_scope else "::",
                    f.recovered_name,
                    (", field-table slot 0x%x" % f.static_slot) if f.static_slot else "",
                )
            )
            stats["fields_recovered"] = stats.get("fields_recovered", 0) + 1
            self.result.bump("fields_recovered")
        elif f.inferred_name:
            doc.append(
                "%s/// INFERRED field name (%s) for slot 0x%x."
                % (indent, f.name_confidence.value, f.offset)
            )
            doc.append(
                "%s/// Instance field names are destroyed by AOT compilation; this name was"
                % indent
            )
            doc.append("%s/// reconstructed from the evidence below, not recovered." % indent)
            if self.opt.emit_evidence:
                for e in f.evidence:
                    doc.append("%s///   evidence: %s" % (indent, _one_line(e.render())))
                for r in f.rejected:
                    doc.append("%s///   rejected: %s" % (indent, _one_line(r)))
            stats["fields_inferred"] = stats.get("fields_inferred", 0) + 1
            self.result.bump("fields_inferred")
            self.result.bump("fields_inferred_" + f.name_confidence.value)
        else:
            doc.append("%s/// UNKNOWN NAME. Slot 0x%x only." % (indent, f.offset))
            doc.append(
                "%s/// Instance field names do not exist in an AOT snapshot and no"
                % indent
            )
            doc.append("%s/// inference rule produced a name for this slot." % indent)
            if self.opt.emit_evidence and f.rejected:
                for r in f.rejected:
                    doc.append("%s///   rejected: %s" % (indent, _one_line(r)))
            stats["fields_unknown"] = stats.get("fields_unknown", 0) + 1
            self.result.bump("fields_unknown")

        if note:
            doc.append("%s/// type note: %s" % (indent, _one_line(note)))
        if f.source and self.opt.emit_asm_refs:
            doc.append("%s/// asm: %s" % (indent, f.source.label()))

        fname = sanitize_identifier(f.name)
        if not top_level and fname == sanitize_type_name(cls.name):
            doc.append(
                "%s/// Renamed to `%s$field`: a field may not share its class's name."
                % (indent, fname)
            )
            fname = fname + r"$field"

        prefix = ""
        if f.is_static and not top_level:
            prefix += "static "
        if dart_type != "dynamic":
            prefix += "late "
        elif f.is_static and top_level:
            prefix += "late "
        decl = "%s%s%s %s; // VM type: %s" % (
            indent,
            prefix,
            dart_type,
            fname,
            f.vm_type,
        )
        self.result.bump("fields")
        return doc + [decl]

    # -- methods ------------------------------------------------------------
    def _emit_method(
        self,
        lib: LibraryIR,
        cls: ClassIR,
        m: MethodIR,
        imports: Set[str],
        stats: Dict[str, int],
        indent: str = "  ",
        top_level: bool = False,
        seen: Optional[Set[Tuple[str, str]]] = None,
    ) -> List[str]:
        seen = seen if seen is not None else set()
        doc: List[str] = []
        notes: List[str] = []

        base_name = m.clean_name
        if m.kind == MethodKind.CLOSURE:
            base_name = r"$closure_0x%x" % (m.address or 0)
            notes.append(
                "RECOVERED: anonymous closure%s. Blutter attributes it to `%s`."
                % ("", m.closure_owner or (cls.name + "." + m.clean_name))
            )
        elif m.kind == MethodKind.OPERATOR:
            base_name = r"$operator_" + re.sub(r"[^A-Za-z0-9]", "_", m.clean_name)
            notes.append(
                "RECOVERED as `operator %s`. Emitted as a plain method because the"
                % m.clean_name
            )
            notes.append("operator's arity is not recorded in the snapshot.")

        name = sanitize_identifier(base_name)
        if (
            not top_level
            and m.kind not in (MethodKind.CONSTRUCTOR, MethodKind.FACTORY)
            and name == sanitize_type_name(cls.name)
        ):
            notes.append(
                "RECOVERED name is `%s`, identical to the class name; Dart forbids that"
                % name
            )
            notes.append("for a member, so `$member` was appended.")
            name = name + r"$member"
        kind_key = {
            MethodKind.GETTER: "get",
            MethodKind.SETTER: "set",
        }.get(m.kind, "m")
        key = (kind_key, name)
        if key in seen:
            n = 2
            while (kind_key, "%s$%d" % (name, n)) in seen:
                n += 1
            notes.append(
                "Name collision inside this class (two entries named `%s` at different"
                % name
            )
            notes.append("addresses); suffixed to keep the file analyzable.")
            name = "%s$%d" % (name, n)
            key = (kind_key, name)
        seen.add(key)

        ret_raw = m.return_vm_type
        ret, ret_note, imps = self.types.resolve(ret_raw, lib)
        imports |= imps
        if m.modifier == Modifier.ASYNC and not ret.startswith("Future"):
            ret = "dynamic"
            notes.append(
                "async method: snapshot return type `%s` is not a Future, emitted as dynamic."
                % ret_raw
            )
        if m.modifier in (Modifier.ASYNC_STAR, Modifier.SYNC_STAR):
            ret = "dynamic"

        params, pnotes, pimports = self._params(lib, m, indent)
        imports |= pimports
        notes.extend(pnotes)

        # doc block ---------------------------------------------------------
        facts = ["RECOVERED signature"]
        if m.address is not None:
            facts.append("address 0x%x" % m.address)
        if m.size:
            facts.append("size 0x%x" % m.size)
        doc.append("%s/// %s." % (indent, ", ".join(facts)))
        if m.source and self.opt.emit_asm_refs:
            doc.append("%s/// asm: %s" % (indent, m.source.label()))
        if ret_note:
            doc.append("%s/// return type note: %s" % (indent, _one_line(ret_note)))
        for n in notes + m.notes:
            doc.append("%s/// %s" % (indent, _one_line(n)))
        doc.append(
            "%s/// BODY NOT RECONSTRUCTED: the snapshot holds ARM64 machine code for this"
            % indent
        )
        doc.append(
            "%s/// method, not Dart. The body below throws so nothing can silently run a"
            % indent
        )
        doc.append("%s/// fake implementation." % indent)

        # signature ---------------------------------------------------------
        mods = ""
        if m.is_static and not top_level and m.kind not in (
            MethodKind.CONSTRUCTOR,
            MethodKind.FACTORY,
        ):
            mods += "static "
        tail = " %s" % m.modifier.value if m.modifier != Modifier.NONE else ""

        if m.kind == MethodKind.GETTER:
            sig = "%s%s%s get %s%s" % (indent, mods, ret, name, tail)
        elif m.kind == MethodKind.SETTER:
            setter_params = params if params.strip("()") else "(dynamic value)"
            sig = "%s%sset %s%s%s" % (indent, mods, name, setter_params, tail)
        elif m.kind == MethodKind.CONSTRUCTOR:
            cname = sanitize_type_name(cls.name)
            suffix = m.clean_name
            if suffix in ("", cls.name, cname):
                sig = "%s%s%s" % (indent, cname, params)
            else:
                sig = "%s%s.%s%s" % (indent, cname, sanitize_identifier(suffix), params)
        elif m.kind == MethodKind.FACTORY:
            cname = sanitize_type_name(cls.name)
            suffix = m.clean_name
            if suffix in ("", cls.name, cname):
                sig = "%sfactory %s%s" % (indent, cname, params)
            else:
                sig = "%sfactory %s.%s%s" % (indent, cname, sanitize_identifier(suffix), params)
        else:
            sig = "%s%s%s %s%s%s" % (indent, mods, ret, name, params, tail)

        body = self._throwing_body(lib, cls, m, indent)
        self.result.bump("methods")
        self.result.bump("methods_stubbed")
        stats["methods"] = stats.get("methods", 0) + 1
        stats["methods_stubbed"] = stats.get("methods_stubbed", 0) + 1
        if m.body_status == BodyStatus.RECONSTRUCTED and m.body_dart:
            # Reserved for a future lifter. Emitted verbatim, clearly labelled.
            self.result.stats["methods_stubbed"] -= 1
            stats["methods_stubbed"] -= 1
            self.result.bump("methods_reconstructed")
            lifted = [indent + line for line in m.body_dart.splitlines()]
            return doc + [sig + " {"] + lifted + [indent + "}"]
        return doc + [sig + " {"] + body + [indent + "}"]

    def _params(
        self, lib: LibraryIR, m: MethodIR, indent: str
    ) -> Tuple[str, List[str], Set[str]]:
        imports: Set[str] = set()
        notes: List[str] = []
        if m.kind in (MethodKind.GETTER,):
            return "", notes, imports
        if not m.params_known:
            notes.append(
                "UNKNOWN parameter list: Blutter reported `/* No info */` for this"
            )
            notes.append(
                "method, so neither the arity nor the types survived. Emitted with no"
            )
            notes.append("parameters; do not read that as `takes none`.")
            return "(/* parameter list UNKNOWN in snapshot */)", notes, imports

        positional: List[str] = []
        named: List[str] = []
        for p in m.params:
            if p.is_receiver:
                continue
            ptype, pnote, pimp = self.types.resolve(p.vm_type, lib)
            imports |= pimp
            if pnote:
                notes.append("parameter %d: %s" % (p.index, pnote))
            pname = p.emitted_name()
            if p.name is None:
                notes.append(
                    "parameter %d name is DESTROYED (positional names are not in the"
                    % p.index
                )
                notes.append("snapshot); emitted as `%s`." % pname)
            if p.is_named:
                named.append("%s %s" % (ptype, pname))
            else:
                positional.append("%s %s" % (ptype, pname))
        parts = list(positional)
        if named:
            parts.append("{%s}" % ", ".join(named))
        return "(%s)" % ", ".join(parts), notes, imports

    def _throwing_body(
        self, lib: LibraryIR, cls: ClassIR, m: MethodIR, indent: str
    ) -> List[str]:
        where = ("%s ::%s" % (lib.url, m.clean_name) if cls.is_library_scope
                 else "%s.%s" % (cls.name, m.clean_name))
        addr = ("0x%x" % m.address) if m.address is not None else "unknown address"
        asm = m.source.label() if m.source else (lib.asm_file or "unknown asm file")
        msg = (
            "%s %s: body not reconstructed. %s @%s (asm: %s). "
            "The AOT snapshot contains machine code for this method, not Dart source."
            % (TOOL_NAME, TOOL_VERSION, where, addr, asm)
        )
        lit = escape_dart_string(msg)
        return [
            "%s  throw UnimplementedError(" % indent,
            "%s      %s);" % (indent, lit),
        ]

    # -- const pool ---------------------------------------------------------
    def _emit_const_pool(self) -> None:
        rel = "lib/_const_pool.dart"
        out = self._banner(
            None,
            extra=[
                "Const objects and string constants lifted out of objs.txt / pp.txt.",
                "String constants are RECOVERED and emitted verbatim.",
                "Const object FIELD NAMES are destroyed, so objects are documented by",
                "slot offset rather than reconstructed as constructor calls.",
            ],
        )
        if self.program.pool_strings:
            out.append("/// RECOVERED string constants, keyed by object-pool reference.")
            out.append("/// Emitted verbatim (escaped only so Dart re-parses the same bytes).")
            out.append("const Map<String, String> recoveredStringPool = <String, String>{")
            for ref in sorted(self.program.pool_strings):
                out.append(
                    "  %s: %s,"
                    % (escape_dart_string(ref), escape_dart_string(self.program.pool_strings[ref]))
                )
            out.append("};")
            out.append("")
            self.result.bump("pool_strings", len(self.program.pool_strings))
        for obj in self.program.orphan_consts:
            out.append("// ---- const %s @%s" % (obj.class_name, obj.address))
            out.append(
                "//      UNMATCHED: no class declaration in the snapshot lines up with"
            )
            out.append("//      these slots, so it is preserved as data, not as code.")
            for off, val in sorted(obj.slots.items()):
                out.append("//      slot 0x%-4x = %s" % (off, _const_repr(val)))
            out.append("")
            self.result.bump("orphan_consts")
        text = self.opt.line_ending.join(out).rstrip() + self.opt.line_ending
        self._add_file(rel, None, text, {})

    def _emit_manifest(self) -> None:
        rel = "RECONSTRUCTION.txt"
        meta = self.program.meta
        lines = [
            "%s %s reconstruction manifest" % (TOOL_NAME, TOOL_VERSION),
            "=" * 60,
            "",
            "This tree was rebuilt from an Android Flutter AOT snapshot. It is NOT the",
            "original source and it will not build as an app. Read it as a map of what",
            "the binary contains, with every hole labelled.",
            "",
            "input            : %s" % (meta.input_name or "<unknown>"),
            "abi              : %s" % meta.abi,
            "dart version     : %s (%s)" % (meta.dart_version or "unknown",
                                            meta.version_signal or "no signal recorded"),
            "snapshot hash    : %s" % (meta.snapshot_hash or "unknown"),
            "blutter          : %s" % (meta.blutter_version or "unknown"),
            "blutter output   : %s" % (meta.blutter_out or "unknown"),
            "obfuscated       : %s" % ("YES" if meta.obfuscated else "no"),
            "parse coverage   : %s (%d unparsed line(s) of %d)"
            % (_coverage_str(meta.parse_coverage), meta.parse_lines_unparsed,
               meta.parse_lines_total),
            "",
            "files emitted    : %d (including this manifest)" % (len(self.result.files) + 1),
            "classes          : %d" % self.result.stats.get("classes", 0),
            "enums            : %d" % self.result.stats.get("enums", 0),
            "enum values      : %d (%d ordinal gap placeholders)"
            % (self.result.stats.get("enum_values", 0), self.result.stats.get("enum_gaps", 0)),
            "methods          : %d, of which %d emit a throwing stub"
            % (self.result.stats.get("methods", 0), self.result.stats.get("methods_stubbed", 0)),
            "fields           : %d recovered name, %d inferred name, %d offset only"
            % (
                self.result.stats.get("fields_recovered", 0),
                self.result.stats.get("fields_inferred", 0),
                self.result.stats.get("fields_unknown", 0),
            ),
            "",
            "These counts are of DECLARATIONS EMITTED. report.json counts FACTS, so it",
            "is higher: enum payload slots and enum constructors are real facts that a",
            "Dart enum cannot legally re-declare, so they are documented in comments",
            "instead of emitted. The report names both numbers.",
            "",
            "See report.json / report.md for the per-library breakdown and for every",
            "inference's evidence trail.",
            "",
        ]
        text = self.opt.line_ending.join(lines)
        self._add_file(rel, None, text, {})


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _coverage_str(fraction: float) -> str:
    """Never round a real gap up to a clean 100%."""
    pct = fraction * 100.0
    if 99.995 <= pct < 100.0:
        return "just under 100%% (%.4f%%)" % pct
    return "%.2f%%" % pct


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _const_repr(value: Any) -> str:
    if isinstance(value, str):
        return escape_dart_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return "0x%x (%d)" % (value, value) if abs(value) > 9 else str(value)
    if isinstance(value, (list, tuple)):
        return "[%s]" % ", ".join(_const_repr(v) for v in value)
    return str(value)


def emit_program(program: ProgramIR, options: Optional[EmitOptions] = None) -> EmitResult:
    return DartEmitter(program, options).emit_program()


__all__ = [
    "EmitOptions", "EmitResult", "EmittedFile", "DartEmitter", "TypeResolver",
    "emit_program", "sanitize_type_name",
]
