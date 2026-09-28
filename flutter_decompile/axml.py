"""flutter_decompile.axml -- decode Android binary XML (AXML) back to text.

``AndroidManifest.xml`` inside an APK is not text: it is Android's chunked
binary-XML format. It *is* the real manifest, though -- decoding it is recovery,
not guesswork -- so a tool that restores an app should hand it back readable
rather than as an opaque blob.

This is a self-contained, standard-library decoder for the parts of AXML a
manifest uses: the string pool, the resource-id map, and the element / attribute
stream. Typed attribute values (strings, ints, booleans, hex, resource and
attribute references) are rendered the way aapt would. Where an attribute name
was stored only as a resource id (no string), the id is shown as ``0xNNNNNNNN``
rather than invented -- honest about what the binary actually held.

No third-party dependency; ~1 chunk-walker, no cleverness.
"""

from __future__ import annotations

import struct
from typing import List, Optional, Tuple

# Chunk types
_RES_STRING_POOL = 0x0001
_RES_XML_START_NS = 0x0100
_RES_XML_END_NS = 0x0101
_RES_XML_START_ELEM = 0x0102
_RES_XML_END_ELEM = 0x0103
_RES_XML_CDATA = 0x0104
_RES_XML_RESOURCE_MAP = 0x0180

# String pool flags
_UTF8_FLAG = 1 << 8

# Attribute value types (Res_value.dataType)
_TYPE_NULL = 0x00
_TYPE_REFERENCE = 0x01
_TYPE_ATTRIBUTE = 0x02
_TYPE_STRING = 0x03
_TYPE_FLOAT = 0x04
_TYPE_INT_DEC = 0x10
_TYPE_INT_HEX = 0x11
_TYPE_INT_BOOL = 0x12


class AxmlError(ValueError):
    pass


class _StringPool:
    def __init__(self, strings: List[str]):
        self.strings = strings

    def get(self, index: int) -> Optional[str]:
        if index < 0 or index >= len(self.strings):
            return None
        return self.strings[index]


def _read_string_pool(buf: bytes, off: int) -> Tuple[_StringPool, int]:
    # chunk header: type(2) headerSize(2) size(4)
    _type, header_size, size = struct.unpack_from("<HHI", buf, off)
    string_count, _style_count, flags, strings_start, _styles_start = \
        struct.unpack_from("<IIIII", buf, off + 8)
    is_utf8 = bool(flags & _UTF8_FLAG)
    offsets = struct.unpack_from("<%dI" % string_count, buf, off + 28)
    data_base = off + strings_start
    out: List[str] = []
    for so in offsets:
        p = data_base + so
        try:
            if is_utf8:
                # two length-prefixed values (chars, then bytes), each u8 or u16
                p, _nchars = _decode_len8(buf, p)
                p, nbytes = _decode_len8(buf, p)
                out.append(buf[p:p + nbytes].decode("utf-8", "replace"))
            else:
                p, nchars = _decode_len16(buf, p)
                out.append(buf[p:p + nchars * 2].decode("utf-16-le", "replace"))
        except (IndexError, struct.error):
            out.append("")
    return _StringPool(out), off + size


def _decode_len8(buf: bytes, p: int) -> Tuple[int, int]:
    n = buf[p]
    p += 1
    if n & 0x80:
        n = ((n & 0x7F) << 8) | buf[p]
        p += 1
    return p, n


def _decode_len16(buf: bytes, p: int) -> Tuple[int, int]:
    n = struct.unpack_from("<H", buf, p)[0]
    p += 2
    if n & 0x8000:
        hi = n & 0x7FFF
        lo = struct.unpack_from("<H", buf, p)[0]
        p += 2
        n = (hi << 16) | lo
    return p, n


def _fmt_value(pool: _StringPool, data_type: int, data: int) -> str:
    if data_type == _TYPE_STRING:
        return pool.get(data) or ""
    if data_type == _TYPE_INT_BOOL:
        return "true" if data else "false"
    if data_type == _TYPE_INT_HEX:
        return "0x%x" % (data & 0xFFFFFFFF)
    if data_type == _TYPE_REFERENCE:
        return "@0x%x" % (data & 0xFFFFFFFF)
    if data_type == _TYPE_ATTRIBUTE:
        return "?0x%x" % (data & 0xFFFFFFFF)
    if data_type == _TYPE_FLOAT:
        return "%g" % struct.unpack("<f", struct.pack("<I", data & 0xFFFFFFFF))[0]
    if data_type == _TYPE_NULL:
        return ""
    # INT_DEC and everything else numeric
    if data & 0x80000000:
        return str(data - 0x100000000)
    return str(data)


def decode(buf: bytes) -> str:
    """Decode AXML bytes into indented, readable XML text."""
    if len(buf) < 8:
        raise AxmlError("too short to be AXML")
    magic, _header_size, _size = struct.unpack_from("<HHI", buf, 0)
    if magic != 0x0003:
        raise AxmlError("not AXML (magic 0x%04x)" % magic)

    off = 8
    pool: Optional[_StringPool] = None
    # (kind, name, attrs) events; rendered in a second pass so a start
    # immediately followed by its own end collapses to a self-closing tag.
    events: List[Tuple[str, str, str]] = []

    while off + 8 <= len(buf):
        ctype, _hsize, csize = struct.unpack_from("<HHI", buf, off)
        if csize <= 0:
            break

        if ctype == _RES_STRING_POOL:
            pool, _ = _read_string_pool(buf, off)
        elif ctype == _RES_XML_START_ELEM and pool is not None:
            name, attrs = _start_element(buf, off, pool)
            events.append(("start", name, attrs))
        elif ctype == _RES_XML_END_ELEM and pool is not None:
            name = pool.get(struct.unpack_from("<I", buf, off + 20)[0]) or "?"
            events.append(("end", name, ""))

        off += csize

    if pool is None:
        raise AxmlError("no string pool found")
    return _render(events)


def _render(events: List[Tuple[str, str, str]]) -> str:
    """Render (kind, name, attrs) events to indented XML, self-closing any
    element whose start is immediately followed by its own end."""
    lines: List[str] = ['<?xml version="1.0" encoding="utf-8"?>']
    depth = 0
    i = 0
    while i < len(events):
        kind, name, attrs = events[i]
        if kind == "start":
            body = (" " + attrs) if attrs else ""
            if i + 1 < len(events) and events[i + 1] == ("end", name, ""):
                lines.append("  " * depth + "<%s%s/>" % (name, body))
                i += 2
                continue
            lines.append("  " * depth + "<%s%s>" % (name, body))
            depth += 1
        else:
            depth = max(0, depth - 1)
            lines.append("  " * depth + "</%s>" % name)
        i += 1
    return "\n".join(lines) + "\n"


def _start_element(buf: bytes, off: int, pool: _StringPool) -> Tuple[str, str]:
    # ResXMLTree_node = header(8) lineNumber(4) comment(4); the attribute
    # extension follows at ext = off+16:
    #   ns(4) name(4) attrStart(2) attrSize(2) attrCount(2)
    #   idIndex(2) classIndex(2) styleIndex(2)
    # attrStart is measured from ext, so the first attribute is ext+attrStart.
    ext = off + 16
    name_idx = struct.unpack_from("<I", buf, ext + 4)[0]
    attr_start, attr_size, attr_count = struct.unpack_from("<HHH", buf, ext + 8)
    name = pool.get(name_idx) or "?"

    attrs: List[str] = []
    base = ext + attr_start
    stride = attr_size or 20
    for i in range(attr_count):
        a = base + i * stride
        ns_i, name_i, raw_i = struct.unpack_from("<III", buf, a)
        # then: size(2) res0(1) dataType(1) data(4)
        _size, _res0, dtype = struct.unpack_from("<HBB", buf, a + 12)
        data = struct.unpack_from("<I", buf, a + 16)[0]
        aname = pool.get(name_i)
        if not aname:
            aname = "attr_0x%x" % (name_i & 0xFFFFFFFF) if name_i != 0xFFFFFFFF else "attr"
        ns = pool.get(ns_i) if ns_i != 0xFFFFFFFF else None
        prefix = "android:" if ns and ns.endswith("android") else ""
        if dtype == _TYPE_STRING and raw_i != 0xFFFFFFFF:
            value = pool.get(raw_i) or _fmt_value(pool, dtype, data)
        else:
            value = _fmt_value(pool, dtype, data)
        attrs.append('%s%s="%s"' % (prefix, aname, value.replace('"', "&quot;")))

    return name, " ".join(attrs)


def summarize(xml_text: str) -> dict:
    """Pull the headline facts out of decoded manifest XML, cheaply and by
    text -- package, versionName/Code, and the permissions requested."""
    import re
    out: dict = {}
    m = re.search(r'<manifest[^>]*\bpackage="([^"]+)"', xml_text)
    if m:
        out["package"] = m.group(1)
    m = re.search(r'\bandroid:versionName="([^"]+)"', xml_text)
    if m:
        out["versionName"] = m.group(1)
    m = re.search(r'\bandroid:versionCode="([^"]+)"', xml_text)
    if m:
        out["versionCode"] = m.group(1)
    perms = re.findall(r'<uses-permission[^>]*android:name="([^"]+)"', xml_text)
    if perms:
        out["permissions"] = sorted(set(perms))
    return out
