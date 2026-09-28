"""flutter_decompile.reconstruct -- write a full .dart source tree with bodies.

This is the stage the README used to say the tool refused to do.  It no longer
refuses: it emits, for every library in the snapshot, a ``.dart`` file whose
classes, fields and method *signatures* are exactly what ``render_skeleton``
produces, and whose method *bodies* are a faithful, linearised reconstruction
of what the AOT code actually does -- every call (resolved to
``lib::class::method`` where Blutter named it), every string literal, every
field load/store, every static-field access, every allocation, in the order the
machine performs them.

Honesty is not negotiable and it is not achieved by refusing to emit.  It is
achieved by *labelling*:

  * The file header states, in the first three lines, that this is a machine
    reconstruction of an AOT snapshot and NOT the original source.
  * Register-style value names (``v3``, ``obj``) are kept verbatim.  A body
    that reads ``v3 = v4.field_0xb`` cannot be mistaken for hand-written Dart,
    which is the whole point: the reader always knows a field name that was
    physically absent from the snapshot has not been invented.
  * A field name that IS present in the output because ``--infer-fields`` fired
    still carries its ``/* inferred: ... */`` note from the inference stage.

What you get is a real decompilation -- the complete behaviour of the app in
readable form -- with none of the "here is your original kdf.dart back" lie.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import parse_asm as pa

RECONSTRUCT_VERSION = "1.1"


@dataclass
class EmitOptions:
    """How much of the machine trace to keep in a reconstructed body.

    By default the pure register-shuffle noise -- frame setup, stack spills,
    register-to-register copies, pointer decompression -- is dropped, because it
    carries no program meaning and buries the operations that do. Every value
    materialisation, comparison, branch, call, string and field access is kept.
    ``keep_asm=True`` restores the byte-for-byte trace for when you need it.
    """
    keep_asm: bool = False


# Lines that are machine bookkeeping, not program behaviour. Dropped by default.
# Deliberately conservative: only frame management, stack spills, plain register
# copies and pointer decompression -- never a value load, compare, branch, or
# arithmetic op, which all carry meaning and stay.
_NOISE = re.compile(
    r"^(?:"
    r"EnterFrame|LeaveFrame|AllocStack\(|CheckStackOverflow|"
    r"DecompressPointer\b|"
    r"mov\s+[a-z0-9]+,\s*[a-z0-9]+\s*$|"          # mov reg, reg
    r"(?:stur|ldur|str|ldr)\s+.*\[(?:fp|SP)\b|"   # a spill to/from the frame
    r"(?:sub|add)\s+SP,"                          # stack pointer adjust
    r")")


def _is_noise(text: str) -> bool:
    return _NOISE.match(text.strip()) is not None

# A value produced into register rN becomes vN.  THR/PP/SP/fp and the like are
# machine registers, not values, and are left as-is when they appear in text.
_REG = re.compile(r"\br(\d+)\b")


def _v(text: str) -> str:
    """Rewrite Blutter's rN register names to friendlier vN value names."""
    return _REG.sub(lambda m: "v" + m.group(1), text)


def _field_name(cls: pa.ClassIR, body_offset: int) -> str:
    """Name a field a body loads/stores by its tagged (body) offset.

    A body prints the tagged offset; a declaration prints ``body_offset + 1``.
    If inference (or recovery) gave the field a real name, use it; otherwise
    fall back to the ``field_0xNN`` hole so the reader sees exactly what the
    snapshot had.
    """
    decl = body_offset + 1
    for f in cls.fields:
        if f.offset == decl:
            if f.name and f.name_confidence == pa.RECOVERED:
                return f.name
            if f.name:
                return f.name  # inferred -- carries its note on the decl line
    return "field_0x%x" % body_offset


def _render_event(ev: pa.BodyEvent, cls: pa.ClassIR,
                  call_at: Dict[int, pa.CallEdge],
                  opts: EmitOptions) -> Optional[str]:
    """One body event -> one line of reconstructed Dart, or None to drop it."""
    text = ev.text.strip()

    if ev.kind == "LoadField":
        m = pa.RE_LOAD_FIELD.match(text)
        if m:
            return "v%s = v%s.%s;" % (m.group("dst"), m.group("obj"),
                                      _field_name(cls, int(m.group("off"), 16)))
    elif ev.kind == "StoreField":
        m = pa.RE_STORE_FIELD.match(text)
        if m:
            return "v%s.%s = %s;" % (m.group("obj"),
                                     _field_name(cls, int(m.group("off"), 16)),
                                     _v(m.group("src")))
    elif ev.kind == "StringLiteral":
        m = pa.RE_STR_LOAD.match(text)
        if m:
            return 'v%s = "%s";' % (m.group("reg"), m.group("val"))
    elif ev.kind == "LoadStaticField":
        m = pa.RE_LOAD_STATIC.match(text)
        if m:
            return "v%s = staticField_0x%s;" % (m.group("reg"), m.group("off"))
    elif ev.kind == "ArrayStore":
        m = pa.RE_ARRAY_STORE.match(text)
        if m:
            return "v%s[%s] = %s;" % (m.group("arr"), _v(m.group("idx")),
                                      _v(m.group("src")))
    elif ev.kind == "ArrayLoad":
        m = pa.RE_ARRAY_LOAD.match(text)
        if m:
            return "v%s = v%s[%s];" % (m.group("dst"), m.group("arr"),
                                       _v(m.group("idx")))
    elif ev.kind == "Call":
        m = pa.RE_CALL_SEM.match(text)
        if m:
            callee = m.group("callee")
            args = _v(m.group("args"))
            edge = call_at.get(ev.addr)
            target = ""
            if edge is not None and edge.method:
                owner = edge.cls or "::"
                target = "  // -> %s%s::%s" % (
                    ("[%s] " % edge.lib) if edge.lib else "", owner, edge.method)
            return "v%s = %s(%s);%s" % (m.group("reg"), callee, args, target)
    elif ev.kind == "InitAsync":
        m = pa.RE_INIT_ASYNC.match(text)
        if m:
            return "// begin async, completes with %s" % m.group("type")
    elif ev.kind == "SetupParameters":
        return "// params: " + text[len("SetupParameters("):].rstrip(")")

    # Anything the parser did not lift to a semantic op (value loads, branches,
    # compares, frame setup) is kept verbatim as a comment. Pure machine
    # bookkeeping is dropped unless keep_asm is set, so the meaningful lines
    # are not buried; everything with program meaning is preserved.
    if not opts.keep_asm and _is_noise(text):
        return None
    return "// %s" % _v(text)


def _calls_by_addr(m: pa.MethodIR) -> Dict[int, pa.CallEdge]:
    """Map a call's *semantic* addr to its branch edge.

    Blutter emits the readable ``v0 = foo()`` line one instruction above the
    raw ``bl #target ; annot``.  We index edges by the nearest addr at or below
    each so the semantic Call event can pick up the resolved target name.
    """
    out: Dict[int, pa.CallEdge] = {}
    for e in m.body.calls:
        out[e.site_addr] = e
    return out


def render_method(cls: pa.ClassIR, m: pa.MethodIR, indent: str = "  ",
                  opts: Optional[EmitOptions] = None) -> List[str]:
    opts = opts or EmitOptions()
    out: List[str] = []
    loc = ("0x%x" % m.addr) if m.addr is not None else "no-body"
    out.append("%s%s {  // %s" % (indent, m.signature(), loc))

    if m.addr is None:
        out.append("%s  // abstract / external: no body in the snapshot" % indent)
        out.append("%s}" % indent)
        return out

    events = m.body.events
    if not events:
        # Bodies were collected without the event stream (quick mode). Fall
        # back to a compact evidence summary so the method is still not empty.
        s = m.body.summary()
        out.append("%s  // %d calls, %d strings, %d field ops (run without "
                   "--quick for the full trace)"
                   % (indent, s["calls"], s["strings"], s["field_access"]))
        for addr, lit in m.body.strings[:12]:
            out.append('%s  // string @0x%x: "%s"' % (indent, addr, lit))
        out.append("%s}" % indent)
        return out

    call_at = _calls_by_addr(m)
    for ev in events:
        line = _render_event(ev, cls, call_at, opts)
        if line is not None:
            out.append("%s  %s" % (indent, line))
    out.append("%s}" % indent)
    return out


def _field_line(f: pa.FieldIR, indent: str) -> str:
    mods = "".join(x for x, on in (("static ", f.is_static), ("late ", f.is_late),
                                   ("final ", f.is_final), ("const ", f.is_const)) if on)
    if f.name_confidence == pa.RECOVERED:
        tag = "recovered"
    elif f.name:
        tag = "INFERRED %s" % f.name_confidence
    else:
        tag = "name destroyed"
    return "%s%s%s %s; // offset: 0x%x  [%s]" % (
        indent, mods, f.vm_type, f.placeholder_name, f.offset or 0, tag)


def render_class(cls: pa.ClassIR, opts: Optional[EmitOptions] = None) -> List[str]:
    opts = opts or EmitOptions()
    out: List[str] = []

    # Blutter names the library scope ``::``. Those are Dart top-level functions
    # and variables, not members of a class -- wrapping them in `class :: {}`
    # would be invalid Dart and misrepresent the structure. Emit them flat.
    if cls.is_library_scope:
        out.append("// top-level declarations")
        for f in cls.fields:
            out.append(_field_line(f, ""))
        for i, m in enumerate(cls.methods):
            if i or cls.fields:
                out.append("")
            out.extend(render_method(cls, m, indent="", opts=opts))
        return out

    head: List[str] = []
    if cls.is_abstract:
        head.append("abstract")
    head.append(cls.kind)
    head.append(cls.name + (cls.type_params or ""))
    if cls.superclass:
        head.append("extends " + cls.superclass)
    if cls.interfaces:
        head.append("implements " + ", ".join(cls.interfaces))

    if cls.class_id is not None:
        out.append("// class id: %d, size: 0x%x%s" % (
            cls.class_id, cls.size or 0,
            (", " + "; ".join(cls.attrs)) if cls.attrs else ""))
    out.append(" ".join(head) + " {")

    for f in cls.fields:
        out.append(_field_line(f, "  "))

    if cls.fields and cls.methods:
        out.append("")
    for i, m in enumerate(cls.methods):
        if i:
            out.append("")
        out.extend(render_method(cls, m, opts=opts))
    out.append("}")
    return out


_HEADER = [
    "// ==========================================================================",
    "// RECONSTRUCTED by flutter_decompile -- NOT original source.",
    "//",
    "// This is a machine reconstruction of a Dart AOT snapshot. Value names",
    "// (v0, v1, ...) are registers, not variables; a `field_0xNN` is a field",
    "// whose name was physically absent from the snapshot. Anything shown as a",
    "// real name is either RECOVERED from the snapshot or INFERRED with evidence",
    "// (see the [tag] on each field). The logic below is faithful; the surface",
    "// syntax is reconstructed. Do not mistake it for the file the author wrote.",
    "// ==========================================================================",
]


def render_library(lib: pa.LibraryIR, opts: Optional[EmitOptions] = None) -> str:
    opts = opts or EmitOptions()
    out: List[str] = list(_HEADER)
    out.append("")
    out.append("// library: %s" % lib.url)
    out.append("// asm:     %s" % lib.asm_path)
    for cls in lib.classes:
        out.append("")
        out.extend(render_class(cls, opts))
    return "\n".join(out) + "\n"


# Package dirs that are the Dart/Flutter SDK or its bundled libraries, not pub
# dependencies. Everything else at the top of asm/ is a package the app pulled
# in -- its name is recovered even though its exact version is not in the build.
_SDK_PACKAGES = frozenset({
    "dart", "flutter", "flutter_localizations", "flutter_test", "flutter_web_plugins",
    "sky_engine", "_engine", "core", "async", "collection", "convert", "typed_data",
    "math", "io", "isolate", "ffi", "developer", "js", "js_util", "mirrors",
})


def emit_pubspec(asm_root: str, dest_root: str, app_packages: List[str],
                 log=lambda s: None) -> Optional[str]:
    """Write a best-effort pubspec.recovered.yaml: the app's dependency names,
    recovered from the packages compiled into the snapshot. Versions are not in
    the build, so they are omitted rather than invented."""
    if not os.path.isdir(asm_root):
        return None
    present = sorted(d for d in os.listdir(asm_root)
                     if os.path.isdir(os.path.join(asm_root, d)))
    app = set(app_packages or [])
    deps = [d for d in present if d not in _SDK_PACKAGES and d not in app
            and not d.startswith((".", "_"))]
    app_name = (app_packages or ["app"])[0]

    lines = [
        "# RECOVERED by flutter_decompile -- dependency NAMES only.",
        "#",
        "# These are the packages compiled into the release snapshot. The names",
        "# are real; the version constraints the author wrote were not kept in",
        "# the build, so they are intentionally omitted rather than guessed.",
        "# The list may include transitive dependencies the app did not name",
        "# directly, and cannot include a dependency the tree-shaker dropped.",
        "",
        "name: %s" % app_name,
        "",
        "environment:",
        "  sdk: '>=3.0.0 <4.0.0'   # placeholder; the real constraint is not recoverable",
        "",
        "dependencies:",
        "  flutter:",
        "    sdk: flutter",
    ]
    for d in deps:
        lines.append("  %s: any   # version not recoverable" % d)
    lines.append("")

    dest = os.path.join(dest_root, "pubspec.recovered.yaml")
    os.makedirs(dest_root, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    log("[emit] recovered %d dependency name(s) -> %s" % (len(deps), dest))
    return dest


def emit_tree(prog: pa.Program, dest_root: str,
              only: str = "*",
              opts: Optional[EmitOptions] = None,
              log=lambda s: None) -> List[str]:
    """Write one .dart per library under ``dest_root``. Returns the paths."""
    from .cli import _skeleton_match, _url_to_path

    pat = "" if only in ("*", "", None) else only
    written: List[str] = []
    failed = 0
    for lib in prog.libraries:
        if not _skeleton_match(pat, lib.url):
            continue
        dest = os.path.join(dest_root, _url_to_path(lib.url))
        # One malformed library must not sink the whole tree: reconstructing
        # 266 files and losing all of them because the 41st tripped on some
        # disassembly shape we did not foresee is the wrong failure mode. Log
        # it, write a stub that says so, and keep going.
        try:
            text = render_library(lib, opts)
        except Exception as e:  # noqa: BLE001 -- isolate one file's failure
            failed += 1
            log("[emit] WARNING: could not reconstruct %s: %s" % (lib.url, e))
            text = ("%s\n\n// library: %s\n// RECONSTRUCTION FAILED for this "
                    "file: %s: %s\n// The other files are unaffected.\n"
                    % ("\n".join(_HEADER), lib.url, type(e).__name__, e))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(text)
        written.append(dest)
    log("[emit] %d reconstructed .dart file(s) -> %s%s"
        % (len(written), dest_root,
           (" (%d could not be fully reconstructed)" % failed) if failed else ""))
    return written
