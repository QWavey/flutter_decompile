"""flutter_decompile.fallback -- Blutter-free extraction.

Blutter needs a Dart runtime whose VM internals it understands; a Dart version
newer than Blutter supports leaves it unable to disassemble the object graph.
That must not mean "no output". Everything below works straight off the ELF,
with no VM build and no version dependence:

  * The app's LIBRARY TREE. Dart AOT keeps every library URL and file path in
    the snapshot as plain text (package:app/ui/home.dart, dart:core/list.dart).
    We pull them out -- that is the real folder/file structure the author wrote,
    RECOVERED, just without bodies.
  * STRINGS. Class and function names, static field names, and every string
    literal (URLs, keys, messages, format strings) live in the snapshot data.
  * A raw ARM64/x64 DISASSEMBLY of the snapshot instructions, via Capstone when
    it is installed. Honest machine code -- no Dart structure, but real.

None of this is decompilation and it is all labelled as what it is. It is the
floor: what any Flutter app yields even when the full pipeline cannot run.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from . import apk as apk_mod

# A library URL as Dart interns it in the snapshot.
_URL_RE = re.compile(rb"(?:package:|dart:)[A-Za-z0-9_./-]+\.dart|dart:[A-Za-z0-9_./-]+")
_ASCII_RE = re.compile(rb"[\x20-\x7e]{4,}")


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def extract_library_urls(data: bytes) -> List[str]:
    """Every Dart library URL/path the snapshot names, de-duplicated & sorted."""
    seen = set()
    for m in _URL_RE.finditer(data):
        s = m.group(0).decode("ascii", "replace")
        # trim a trailing stray byte the regex sometimes catches after ".dart"
        seen.add(s)
    return sorted(seen)


def split_packages(urls: List[str]) -> Dict[str, List[str]]:
    """Group library URLs by package. package:<pkg>/rest.dart -> {pkg: [rest]}."""
    out: Dict[str, List[str]] = {}
    for u in urls:
        if u.startswith("package:"):
            pkg, _, rest = u[len("package:"):].partition("/")
        elif u.startswith("dart:"):
            pkg, rest = "dart", u[len("dart:"):]
        else:
            pkg, rest = "<other>", u
        out.setdefault(pkg, []).append(rest or u)
    for k in out:
        out[k] = sorted(set(out[k]))
    return out


def guess_app_packages(pkgs: Dict[str, List[str]]) -> List[str]:
    """The app's own package(s): not dart:, flutter, or a known pub dependency.
    Heuristic -- reported as such."""
    known = {"dart", "flutter", "flutter_localizations", "sky_engine", "collection",
             "async", "meta", "path", "characters", "vector_math",
             "material_color_utilities", "ffi", "js", "intl"}
    cands = [p for p in pkgs
             if p not in known and not p.startswith(("<", "dart"))]
    # The app package usually owns main.dart or an app/ tree; otherwise keep all
    # non-SDK, non-obvious-pub names and let the report show them.
    owns_main = [p for p in cands if any(r == "main.dart" or r.endswith("/main.dart")
                                         for r in pkgs[p])]
    return owns_main or cands


def extract_strings(data: bytes, min_len: int = 5) -> List[str]:
    """Printable ASCII runs, de-duplicated, longest-first-ish (sorted)."""
    out = set()
    for m in _ASCII_RE.finditer(data):
        s = m.group(0).decode("ascii", "replace")
        if len(s) >= min_len:
            out.add(s)
    return sorted(out)


def disassemble_text(libapp: str, dest: str, log=lambda s: None) -> Optional[str]:
    """Disassemble the snapshot instructions with Capstone, if installed.

    Returns the written path, or None when Capstone is missing (with a note in
    the log) or the code section can't be located."""
    try:
        import capstone
    except ImportError:
        log("[fallback] Capstone not installed; skipping raw disassembly "
            "(pip install capstone to enable it)")
        return None

    sections, meta = apk_mod.read_elf_sections(libapp)
    text = next((s for s in sections if s.name == ".text"), None)
    if text is None or not text.size:
        log("[fallback] no .text section found; skipping disassembly")
        return None

    machine = meta.get("machine")
    if machine == 183:          # EM_AARCH64
        md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
        arch = "arm64"
    elif machine == 62:         # EM_X86_64
        md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
        arch = "x64"
    else:
        log("[fallback] unsupported machine %r; skipping disassembly" % machine)
        return None

    data = _read(libapp)
    code = data[text.offset:text.offset + text.size]
    md.skipdata = True          # never abort on a non-instruction byte
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write("; Raw %s disassembly of the snapshot .text (Capstone).\n"
                 "; This is machine code, not Dart. There are no function\n"
                 "; boundaries or names here -- that is what the Dart runtime\n"
                 "; provides and what Blutter reconstructs when it supports the\n"
                 "; Dart version. Base address 0x%x.\n\n" % (arch, text.addr))
        n = 0
        for insn in md.disasm(code, text.addr):
            fh.write("0x%08x:  %-10s %s\n" % (insn.address, insn.mnemonic, insn.op_str))
            n += 1
    log("[fallback] disassembled %d %s instructions -> %s" % (n, arch, dest))
    return dest


def run(libapp: str, out_dir: str, dart_version: Optional[str] = None,
        log=lambda s: None) -> Dict[str, Any]:
    """Produce the Blutter-free artefacts under out_dir/snapshot/. Returns a
    small summary dict."""
    snap_dir = os.path.join(out_dir, "snapshot")
    os.makedirs(snap_dir, exist_ok=True)
    data = _read(libapp)

    urls = extract_library_urls(data)
    pkgs = split_packages(urls)
    app_pkgs = guess_app_packages(pkgs)
    strings = extract_strings(data)

    with open(os.path.join(snap_dir, "library_urls.txt"), "w", encoding="utf-8") as fh:
        fh.write("# Dart library URLs / file paths recovered from the snapshot.\n"
                 "# These names are real (RECOVERED); bodies are not here.\n\n")
        fh.write("\n".join(urls) + "\n")

    with open(os.path.join(snap_dir, "app_libraries.txt"), "w", encoding="utf-8") as fh:
        fh.write("# The app's own library tree (best-effort: non-SDK, non-obvious-"
                 "pub packages).\n\n")
        for p in app_pkgs:
            fh.write("package:%s/\n" % p)
            for rest in pkgs.get(p, []):
                fh.write("  %s\n" % rest)
            fh.write("\n")

    with open(os.path.join(snap_dir, "strings.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(strings) + "\n")

    asm = disassemble_text(libapp, os.path.join(snap_dir, "snapshot_disasm.asm"), log=log)

    log("[fallback] %d library URLs (%d app), %d strings -> %s"
        % (len(urls), sum(len(pkgs.get(p, [])) for p in app_pkgs), len(strings), snap_dir))
    return {
        "snapshot_dir": snap_dir,
        "library_urls": len(urls),
        "app_packages": app_pkgs,
        "app_library_count": sum(len(pkgs.get(p, [])) for p in app_pkgs),
        "strings": len(strings),
        "disassembly": asm,
    }


def write_report(out_dir: str, summary: Dict[str, Any],
                 dart_version: Optional[str]) -> str:
    app_pkgs = summary.get("app_packages") or []
    lines = [
        "# Fallback extraction (Blutter-free)",
        "",
        "The full reconstruction could not run: Blutter does not support Dart "
        "%s. This is\nwhat was recovered directly from the snapshot instead -- "
        "no Dart VM required." % (dart_version or "this version"),
        "",
        "| Artefact | What it is | Fidelity |",
        "|---|---|---|",
        "| `snapshot/app_libraries.txt` | the app's own library/file tree | "
        "**RECOVERED** names (no bodies) |",
        "| `snapshot/library_urls.txt` | every Dart library URL in the snapshot | "
        "**RECOVERED** |",
        "| `snapshot/strings.txt` | class/function/field names + string literals | "
        "**RECOVERED** |",
        "| `snapshot/snapshot_disasm.asm` | raw machine-code disassembly | "
        "**RAW** (no Dart structure) |",
        "| `apk_contents/`, `RESTORED.md` | the rest of the APK | **RECOVERED** |",
        "",
        "Recovered: %d library URLs, %d strings."
        % (summary.get("library_urls", 0), summary.get("strings", 0)),
    ]
    if app_pkgs:
        lines.append("")
        lines.append("App package(s): %s" % ", ".join(str(p) for p in app_pkgs))
    if not summary.get("disassembly"):
        lines += ["", "Raw disassembly was skipped (install `capstone` to enable it)."]
    lines += [
        "",
        "## Why not full bodies",
        "",
        "Method bodies live in the snapshot as a Dart-runtime object graph. "
        "Reading that\ngraph needs a Dart runtime of the matching version, which "
        "is what Blutter builds\nand walks. Until Blutter supports Dart %s, the "
        "structure, names and strings\nabove are what can be recovered honestly. "
        "Build the app with an older stable\nFlutter for the full reconstruction."
        % (dart_version or "this version"),
        "",
    ]
    path = os.path.join(out_dir, "FALLBACK_REPORT.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path
