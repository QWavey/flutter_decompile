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
from typing import Dict, List, Optional

from . import parse_asm as pa

RECONSTRUCT_VERSION = "1.0"

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
                  call_at: Dict[int, pa.CallEdge]) -> Optional[str]:
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

    # Anything the parser did not lift to a semantic op (branches, compares,
    # frame setup) is kept verbatim as a comment so the trace stays complete
    # and nothing is silently dropped.
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


def render_method(cls: pa.ClassIR, m: pa.MethodIR, indent: str = "  ") -> List[str]:
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
        line = _render_event(ev, cls, call_at)
        if line is not None:
            out.append("%s  %s" % (indent, line))
    out.append("%s}" % indent)
    return out


def render_class(cls: pa.ClassIR) -> List[str]:
    out: List[str] = []
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
        mods = "".join(x for x, on in (("static ", f.is_static), ("late ", f.is_late),
                                       ("final ", f.is_final), ("const ", f.is_const)) if on)
        if f.name_confidence == pa.RECOVERED:
            tag = "recovered"
        elif f.name:
            tag = "INFERRED %s" % f.name_confidence
        else:
            tag = "name destroyed"
        out.append("  %s%s %s; // offset: 0x%x  [%s]" % (
            mods, f.vm_type, f.placeholder_name, f.offset or 0, tag))

    if cls.fields and cls.methods:
        out.append("")
    for i, m in enumerate(cls.methods):
        if i:
            out.append("")
        out.extend(render_method(cls, m))
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


def render_library(lib: pa.LibraryIR) -> str:
    out: List[str] = list(_HEADER)
    out.append("")
    out.append("// library: %s" % lib.url)
    out.append("// asm:     %s" % lib.asm_path)
    for cls in lib.classes:
        out.append("")
        out.extend(render_class(cls))
    return "\n".join(out) + "\n"


def emit_tree(prog: pa.Program, dest_root: str,
              only: str = "*",
              log=lambda s: None) -> List[str]:
    """Write one .dart per library under ``dest_root``. Returns the paths."""
    from .cli import _skeleton_match, _url_to_path

    pat = "" if only in ("*", "", None) else only
    written: List[str] = []
    for lib in prog.libraries:
        if not _skeleton_match(pat, lib.url):
            continue
        dest = os.path.join(dest_root, _url_to_path(lib.url))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(render_library(lib))
        written.append(dest)
    log("[emit] %d reconstructed .dart file(s) -> %s" % (len(written), dest_root))
    return written
