"""
flutter_decompile.parse_asm -- Stage 3a + Stage 4 (partial).

Parses Blutter's ``asm/**/*.dart`` annotated-disassembly files into an IR, then
links the per-file facts into one program-wide model with a call graph.

FORMAT FINGERPRINT
------------------
This parser was written against, and verified on, the Blutter output at
``NEXUS-apk-decompiled/blutter_out/asm`` (1362 files, Dart 3.x, arm64-v8a).
The ``asm/`` text format is Blutter's *private* format and it changes.  Every
rule below carries a ``SEEN`` example taken verbatim from that output.  When a
line does not match any rule the parser does **not** guess: it records an
``UnparsedLine`` fact, and those surface in the coverage report as
``parse_coverage < 100%``.

THE HONESTY RULE (design doc s0.3)
----------------------------------
Every name this module produces is tagged with a ``Confidence``:

    RECOVERED  -- read verbatim out of the snapshot (class name, method name,
                  string literal, static field name, enum ordinal/name).
    INFERRED   -- a guess, with an evidence trail attached.  This module
                  produces *no* INFERRED names; it only collects the raw
                  evidence (``BodyFacts``) that stage 5 needs.
    UNKNOWN    -- Blutter printed ``_`` or ``/* No info */``.
    DESTROYED  -- the information is provably not in an AOT snapshot at all
                  (instance field names on non-``late`` fields, local variable
                  names, parameter names, comments, statements).

``field_8`` is DESTROYED, not UNKNOWN.  Never let it be silently renamed.

Run standalone:

    python -m flutter_decompile.parse_asm <asm-dir-or-file> [--json out.json]
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

# The Blutter revision family this parser's rules were validated against.
BLUTTER_FORMAT_FINGERPRINT = "blutter/asm-v1 (dart3, 2024-2025 line shapes)"


# --------------------------------------------------------------------------- #
# Confidence
# --------------------------------------------------------------------------- #

class Confidence(str):
    """A str subclass so it JSON-serialises without a custom encoder."""
    __slots__ = ()


RECOVERED = Confidence("RECOVERED")
INFERRED = Confidence("INFERRED")
UNKNOWN = Confidence("UNKNOWN")
DESTROYED = Confidence("DESTROYED")


# --------------------------------------------------------------------------- #
# Line grammar.  Each regex is annotated with a real SEEN line.
# --------------------------------------------------------------------------- #

# SEEN: "// lib: , url: package:chat/core/crypto/kdf.dart"
RE_LIB_HEADER = re.compile(r"^//\s*lib:\s*(?P<lib>[^,]*),\s*url:\s*(?P<url>.+?)\s*$")

# SEEN: "// class id: 2323, size: 0x20, field offset: 0x8"
# SEEN: "// class id: 1048704, size: 0x8"
RE_CLASS_ID = re.compile(
    r"^//\s*class id:\s*(?P<cid>\d+),\s*size:\s*0x(?P<size>[0-9a-fA-F]+)"
    r"(?:,\s*field offset:\s*0x(?P<foff>[0-9a-fA-F]+))?\s*$"
)

# SEEN: "//   const constructor, "
RE_CLASS_ATTR = re.compile(r"^//\s{2,}(?P<attrs>\S.*?)\s*$")

# SEEN: "class KdfParams extends Object {"
# SEEN: "class :: {"
# SEEN: "enum LayerCipher extends _Enum {"
# SEEN: "abstract class ReducedMotionRoute<X0> extends PageRoute<X0> {"
# SEEN (2 lines): "class _SecureKvStore extends Object" / "    implements KeyValueStore {"
RE_CLASS_DECL = re.compile(
    r"^(?P<abstract>abstract\s+)?(?P<kw>class|enum|mixin)\s+"
    r"(?P<name>::|[A-Za-z_$][\w$]*(?:&[\w$&]+)*)\s*"
    r"(?P<targs><.*?>)?\s*"
    r"(?:extends\s+(?P<super>[^{]+?)\s*)?"
    r"(?:implements\s+(?P<impl>[^{]+?)\s*)?"
    r"\{\s*$"
)

# SEEN: "  _Mint field_8;"
# SEEN: "  static late final List<int> _veilLabel; // offset: 0xe80"
# SEEN: "  late final Animation<double> _entrance; // offset: 0x20"
# SEEN: "  static late final JClass JniFlutterPlugin._class; // offset: 0x1344"
# SEEN: "  static late final (dynamic, Pointer<Void>) => JniResult X._get$y; // offset: 0x134c"
RE_FIELD_DECL = re.compile(
    r"^(?P<all>(?:static\s+|late\s+|final\s+|const\s+|covariant\s+)*"
    r"(?P<type>.+?))\s+(?P<name>[A-Za-z_$#][\w$#]*(?:\.[A-Za-z_$#][\w$#]*)*)\s*;"
    r"(?:\s*//\s*offset:\s*0x(?P<off>[0-9a-fA-F]+))?\s*$"
)
RE_FIELD_MODS = re.compile(r"^(?:(static|late|final|const|covariant)\s+)")

# SEEN: "  static _ deriveVeilKey(/* No info */) async {"
# SEEN: "  Map<String, dynamic> toJson(KdfParams) {"
# SEEN: "  factory _ KdfParams.fromJson(/* No info */) {"
# SEEN: "  [closure] static void <anonymous closure>(dynamic) {"
# SEEN: "  get _ _allowPaste(/* No info */) {"
# SEEN: "  set _ state=(/* No info */) {"
# SEEN: "  _ ==(/* No info */) {"                          <- operator
# SEEN: "  static _ _extension#0.characters(/* No info */) {"   <- extension member
# SEEN: "  Future<Y0> _coalesce<Y0>(ContactsRepository, String, (dynamic) => Future<Y0>) {"
# SEEN: "  void dyn:set:enabled(RenderBackdropFilter, bool) {"     <- dyn forwarder
# SEEN: "  static _ BaselineOffset.+(/* No info */) {"             <- qualified operator
# SEEN: "  static _ _AxisSize.(/* No info */) {"                   <- unnamed constructor
# SEEN: "  static Y0 JArray.[]<Y0 extends JObject?>(JObject, int) {"
# SEEN: "  [closure] JniResult #ffiClosure17(dynamic, Pointer<Void>) {"
# SEEN: "  [closure] Action<Intent>? #action#initializer(dynamic) {"
# SEEN: "  [closure] static void TransformByHandlers|_defaultHandleDone<Y0>(...) {"
_IDENT = r"[A-Za-z_$#][\w$#|]*"
_OPER = r"\[\]=|\[\]|unary-|>>>|<<|>>|==|<=|>=|~/|[-+*/%&|^~<>]"
RE_METHOD_NAME = (
    r"(?P<name>"
    r"<anonymous closure>"
    r"|dyn:[\w:$#|]+"
    r"|(?:" + _IDENT + r"\.)+(?:" + _OPER + r")"      # BaselineOffset.+ , JArray.[]=
    r"|" + _IDENT + r"(?:\." + _IDENT + r")*\.?=?"    # foo , A.b , _AxisSize.
    r"|(?:" + _OPER + r")"                            # ==
    r")"
)
RE_METHOD_DECL = re.compile(
    r"^(?P<closure>\[closure\]\s+)?"
    r"(?P<mods>(?:static\s+|get\s+|set\s+|factory\s+)*)"
    r"(?P<ret>.+?)\s+"
    + RE_METHOD_NAME +
    r"(?P<mtargs><[^()]*>)?"
    r"\((?P<params>.*)\)\s*"
    r"(?P<amod>async\*|async|sync\*)?\s*\{\s*$"
)

# SEEN: "    // ** addr: 0x558a30, size: 0xc8"
RE_ADDR = re.compile(r"^//\s*\*\*\s*addr:\s*0x(?P<addr>[0-9a-fA-F]+),\s*size:\s*0x(?P<size>[0-9a-fA-F]+)")

# Semantic body line: exactly ONE space between "//" and the address.
# SEEN: "    // 0x558a68: StoreField: r2->field_f = r16"
RE_BODY_SEMANTIC = re.compile(r"^//\s0x(?P<addr>[0-9a-fA-F]+):\s(?P<text>.*)$")
# Raw ARM64 line: FIVE spaces.
# SEEN: "    //     0x558a68: stur            w16, [x2, #0xf]"
RE_BODY_RAW = re.compile(r"^//\s{2,}0x(?P<addr>[0-9a-fA-F]+):\s(?P<text>.*)$")

# --- body-fact extraction (from semantic lines) ---------------------------- #
# SEEN: "LoadField: r4 = r3->field_7"
RE_LOAD_FIELD = re.compile(r"^LoadField:\s*r(?P<dst>\d+)\s*=\s*r(?P<obj>\d+)->field_(?P<off>[0-9a-fA-F]+)")
# SEEN: "StoreField: r2->field_f = r16"
RE_STORE_FIELD = re.compile(r"^StoreField:\s*r(?P<obj>\d+)->field_(?P<off>[0-9a-fA-F]+)\s*=\s*(?P<src>\S+)")
# SEEN: "ArrayStore: r2[0] = r16  ; List_4"
RE_ARRAY_STORE = re.compile(r"^ArrayStore:\s*r(?P<arr>\d+)\[(?P<idx>[^\]]+)\]\s*=\s*(?P<src>\S+)")
# SEEN: "ArrayLoad: r1 = r0[0]  ; List_4"
RE_ARRAY_LOAD = re.compile(r"^ArrayLoad:\s*r(?P<dst>\d+)\s*=\s*r(?P<arr>\d+)\[(?P<idx>[^\]]+)\]")
# SEEN: 'r16 = "time_cost"'
RE_STR_LOAD = re.compile(r'^r(?P<reg>\d+)\s*=\s*"(?P<val>.*)"\s*$')
# SEEN: "r0 = LoadStaticField(0xe80)"
RE_LOAD_STATIC = re.compile(r"^r(?P<reg>\d+)\s*=\s*LoadStaticField\(0x(?P<off>[0-9a-fA-F]+)\)")
# SEEN: "r0 = _hkdfSubkey()"   /  "r0 = AllocateArray()"  /  "r0 = Await()"
RE_CALL_SEM = re.compile(r"^r(?P<reg>\d+)\s*=\s*(?P<callee>[A-Za-z_$][\w$<>.:&]*)\((?P<args>.*)\)\s*$")
# SEEN: "InitAsync() -> Future<List<int>>"
RE_INIT_ASYNC = re.compile(r"^InitAsync\(\)\s*->\s*(?P<type>.+?)\s*$")
# SEEN: "SetupParameters(KdfParams this /* r1 => r0, fp-0x8 */)"
RE_SETUP_PARAMS = re.compile(r"^SetupParameters\((?P<body>.*)\)\s*$")
RE_SETUP_ONE = re.compile(r"(?P<type>[^,/]+?)\s+(?P<name>this|_)\s*/\*\s*(?P<slots>[^*]*?)\s*\*/")

# --- annotations found on RAW lines ---------------------------------------- #
# SEEN: "bl              #0x559da8  ; [package:chat/core/crypto/kdf.dart] ::_argon2Stretch"
# SEEN: "bl              #0xb9af28  ; AllocateArrayStub"
# SEEN: "b               #0x4a0ce0  ; ReturnAsyncStub"
RE_BRANCH_TARGET = re.compile(
    r"\b(?P<op>bl|b|blr)\s+#0x(?P<target>[0-9a-fA-F]+)\s*;\s*(?P<annot>.+?)\s*$"
)
# SEEN: "[package:chat/app.dart] _AuthGateState::_backToOnlineLogin"
RE_QUALIFIED_CALLEE = re.compile(r"^\[(?P<lib>[^\]]+)\]\s*(?P<cls>[\w$<>&.]*)::(?P<method>[\w$<>.&=]+)")
# SEEN: "[pp+0x1d418] Field <::._veilLabel@823249941>: static late final (offset: 0xe80)"
RE_POOL_REF = re.compile(r"\[pp\+0x(?P<off>[0-9a-fA-F]+)\]\s*(?P<payload>.*?)\s*$")
# SEEN: "Field <::._veilLabel@823249941>: static late final (offset: 0xe80)"
RE_POOL_FIELD = re.compile(
    r"^Field\s+<(?P<owner>[^.>]*)\.(?P<name>[^@>]+)(?:@(?P<hash>\d+))?>:\s*"
    r"(?P<flags>[^(]*?)\s*\(offset:\s*0x(?P<off>[0-9a-fA-F]+)\)"
)
# SEEN: "AllocateAccountBlockStub -> AccountBlock (size=0x10)"
RE_ALLOC_STUB = re.compile(r"^Allocate(?P<cls>\w+)Stub\s*->\s*(?P<target>[\w$<>&]+)\s*\(size=0x(?P<size>[0-9a-fA-F]+)\)")


# --------------------------------------------------------------------------- #
# IR
# --------------------------------------------------------------------------- #

@dataclass
class PoolRef:
    """A reference to the object pool seen in a method body."""
    offset: int                       # pp+0xNNN
    payload: str                      # raw text after the bracket
    kind: str = "raw"                 # String / Field / Obj / TypeArguments / List / Stub / ...

    def to_dict(self) -> Dict[str, Any]:
        return {"pp": hex(self.offset), "kind": self.kind, "payload": self.payload}


@dataclass
class CallEdge:
    """A `bl #target ; annotation` edge out of a method body."""
    site_addr: int
    target_addr: int
    annotation: str
    lib: Optional[str] = None
    cls: Optional[str] = None
    method: Optional[str] = None
    stub: Optional[str] = None

    @property
    def is_stub(self) -> bool:
        return self.stub is not None

    def qualified(self) -> str:
        if self.method:
            owner = self.cls or "::"
            return "%s%s::%s" % (("[%s] " % self.lib) if self.lib else "", owner, self.method)
        return self.stub or self.annotation

    def to_dict(self) -> Dict[str, Any]:
        return {
            "site": hex(self.site_addr), "target": hex(self.target_addr),
            "lib": self.lib, "class": self.cls, "method": self.method,
            "stub": self.stub, "annotation": self.annotation,
        }


@dataclass
class FieldAccess:
    """LoadField / StoreField.

    Blutter prints the *untagged* byte offset in bodies (`field_7`) while the
    field declaration list prints the *tagged* heap offset (`field_8`).
    kHeapObjectTag == 1, so decl_offset == body_offset + 1.  Both are kept so
    stage-5 inference never has to guess which convention it is looking at.
    """
    addr: int
    kind: str                 # "load" | "store"
    body_offset: int
    obj_reg: int
    other: str                # dst reg for load, src operand for store

    @property
    def decl_offset(self) -> int:
        return self.body_offset + 1

    def to_dict(self) -> Dict[str, Any]:
        return {"addr": hex(self.addr), "kind": self.kind,
                "body_offset": hex(self.body_offset), "decl_offset": hex(self.decl_offset),
                "obj_reg": self.obj_reg, "other": self.other}


@dataclass
class StaticFieldRef:
    """`Field <Owner.name@hash>: flags (offset: 0xN)` -- a RECOVERED static name."""
    owner: str
    name: str
    flags: str
    offset: int
    confidence: Confidence = RECOVERED

    def to_dict(self) -> Dict[str, Any]:
        return {"owner": self.owner or "::", "name": self.name,
                "flags": self.flags, "offset": hex(self.offset)}


@dataclass
class BodyEvent:
    """One semantic line Blutter emitted, kept in source order."""
    addr: int
    kind: str
    text: str

    def to_dict(self) -> Dict[str, Any]:
        return {"addr": hex(self.addr), "kind": self.kind, "text": self.text}


@dataclass
class BodyFacts:
    """Ordered evidence extracted from one method body.

    This is deliberately NOT an attempt at decompilation.  It is the evidence
    ledger that stage 5 (field-name inference) and the report consume.
    """
    events: List[BodyEvent] = dc_field(default_factory=list)
    calls: List[CallEdge] = dc_field(default_factory=list)
    pool_refs: List[PoolRef] = dc_field(default_factory=list)
    strings: List[Tuple[int, str]] = dc_field(default_factory=list)   # (addr, literal)
    field_access: List[FieldAccess] = dc_field(default_factory=list)
    static_fields: List[StaticFieldRef] = dc_field(default_factory=list)
    alloc_types: List[str] = dc_field(default_factory=list)
    param_setup: List[str] = dc_field(default_factory=list)
    async_return_type: Optional[str] = None
    n_semantic_lines: int = 0
    n_raw_lines: int = 0

    def summary(self) -> Dict[str, Any]:
        return {
            "events": len(self.events), "calls": len(self.calls),
            "pool_refs": len(self.pool_refs), "strings": len(self.strings),
            "field_access": len(self.field_access),
            "static_fields": len(self.static_fields),
            "alloc_types": sorted(set(self.alloc_types)),
            "async_return_type": self.async_return_type,
            "asm_lines": self.n_semantic_lines + self.n_raw_lines,
        }


@dataclass
class MethodIR:
    name: str
    kind: str = "method"     # method|getter|setter|factory|constructor|operator|closure
    type_params: Optional[str] = None
    is_static: bool = False
    is_closure: bool = False
    async_modifier: Optional[str] = None      # async | async* | sync*
    return_type: Optional[str] = None         # None == Blutter printed "_"
    return_type_confidence: Confidence = RECOVERED
    param_types: Optional[List[str]] = None   # None == "/* No info */"
    param_names_confidence: Confidence = DESTROYED
    addr: Optional[int] = None
    size: Optional[int] = None
    decl_line: int = 0
    decl_text: str = ""
    body: BodyFacts = dc_field(default_factory=BodyFacts)

    @property
    def has_body(self) -> bool:
        return self.addr is not None

    def signature(self) -> str:
        ret = self.return_type or "/*unknown*/ dynamic"
        params = "/*param types unknown*/" if self.param_types is None else ", ".join(self.param_types)
        pre = ""
        if self.is_static:
            pre += "static "
        if self.kind == "getter":
            pre += "get "
        elif self.kind == "setter":
            pre += "set "
        elif self.kind == "factory":
            pre += "factory "
        elif self.kind == "operator":
            pre += "operator "
        suf = (" " + self.async_modifier) if self.async_modifier else ""
        return "%s%s %s%s(%s)%s" % (pre, ret, self.name, self.type_params or "", params, suf)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "type_params": self.type_params,
            "static": self.is_static,
            "closure": self.is_closure, "async": self.async_modifier,
            "return_type": self.return_type,
            "return_type_confidence": str(self.return_type_confidence),
            "param_types": self.param_types,
            "param_names": {"value": None, "confidence": str(self.param_names_confidence)},
            "addr": hex(self.addr) if self.addr is not None else None,
            "size": hex(self.size) if self.size is not None else None,
            "decl_line": self.decl_line,
            "body": self.body.summary(),
        }


@dataclass
class FieldIR:
    name: Optional[str]
    vm_type: str
    offset: Optional[int]
    is_static: bool = False
    is_late: bool = False
    is_final: bool = False
    is_const: bool = False
    name_confidence: Confidence = RECOVERED
    decl_line: int = 0

    @property
    def placeholder_name(self) -> str:
        if self.name:
            return self.name
        return "field_%x" % (self.offset or 0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "placeholder": self.placeholder_name,
            "type": self.vm_type, "offset": hex(self.offset) if self.offset is not None else None,
            "static": self.is_static, "late": self.is_late, "final": self.is_final,
            "name_confidence": str(self.name_confidence), "decl_line": self.decl_line,
        }


@dataclass
class ClassIR:
    name: str
    kind: str = "class"               # class | enum | mixin
    is_abstract: bool = False
    type_params: Optional[str] = None
    superclass: Optional[str] = None
    interfaces: List[str] = dc_field(default_factory=list)
    class_id: Optional[int] = None
    size: Optional[int] = None
    field_offset: Optional[int] = None
    attrs: List[str] = dc_field(default_factory=list)   # e.g. "const constructor"
    fields: List[FieldIR] = dc_field(default_factory=list)
    methods: List[MethodIR] = dc_field(default_factory=list)
    decl_line: int = 0

    @property
    def is_library_scope(self) -> bool:
        return self.name == "::"

    @property
    def mixin_chain(self) -> List[str]:
        """`_MixinApplication369&Animation&AnimationEagerListenerMixin&...`

        RECOVERED as a flattened name; the original `with` clause ordering is
        recoverable from it, but the fact that it *was* a mixin application in
        source is INFERRED from the `_MixinApplication` prefix.
        """
        s = self.superclass or ""
        if "&" not in s:
            return []
        return s.split("&")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "abstract": self.is_abstract,
            "type_params": self.type_params, "superclass": self.superclass,
            "interfaces": self.interfaces,
            "class_id": self.class_id,
            "size": hex(self.size) if self.size is not None else None,
            "field_offset": hex(self.field_offset) if self.field_offset is not None else None,
            "attrs": self.attrs,
            "mixin_chain": self.mixin_chain,
            "fields": [f.to_dict() for f in self.fields],
            "methods": [m.to_dict() for m in self.methods],
            "decl_line": self.decl_line,
        }


@dataclass
class UnparsedLine:
    path: str
    lineno: int
    text: str

    def to_dict(self) -> Dict[str, Any]:
        return {"file": self.path, "line": self.lineno, "text": self.text}


@dataclass
class LibraryIR:
    url: str                          # package:chat/core/crypto/kdf.dart
    asm_path: str
    classes: List[ClassIR] = dc_field(default_factory=list)
    unparsed: List[UnparsedLine] = dc_field(default_factory=list)
    n_lines: int = 0

    @property
    def package(self) -> str:
        if self.url.startswith("package:"):
            return self.url[len("package:"):].split("/", 1)[0]
        if self.url.startswith("dart:"):
            return self.url.split(":", 1)[1].split("/", 1)[0]
        return "<app>"

    @property
    def dart_path(self) -> str:
        """Best-effort source-relative path (`lib/core/crypto/kdf.dart`)."""
        if self.url.startswith("package:"):
            rest = self.url[len("package:"):].split("/", 1)
            return "lib/" + (rest[1] if len(rest) > 1 else rest[0])
        return self.url.replace(":", "_").replace("//", "/")

    @property
    def library_scope(self) -> Optional[ClassIR]:
        for c in self.classes:
            if c.is_library_scope:
                return c
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url, "package": self.package, "dart_path": self.dart_path,
            "asm_path": self.asm_path, "lines": self.n_lines,
            "classes": [c.to_dict() for c in self.classes],
            "unparsed": [u.to_dict() for u in self.unparsed],
        }


@dataclass
class Program:
    """Stage-4 linked model."""
    libraries: List[LibraryIR] = dc_field(default_factory=list)
    method_by_addr: Dict[int, Tuple[LibraryIR, ClassIR, MethodIR]] = dc_field(default_factory=dict)
    callers_of: Dict[int, List[int]] = dc_field(default_factory=dict)   # target -> [caller addrs]
    callees_of: Dict[int, List[int]] = dc_field(default_factory=dict)   # caller -> [target addrs]
    blutter_fingerprint: str = BLUTTER_FORMAT_FINGERPRINT

    # -- convenience -------------------------------------------------------- #
    def libs_for_package(self, pkg: str) -> List[LibraryIR]:
        return [l for l in self.libraries if l.package == pkg]

    def packages(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for l in self.libraries:
            out[l.package] = out.get(l.package, 0) + 1
        return out

    def iter_methods(self) -> Iterator[Tuple[LibraryIR, ClassIR, MethodIR]]:
        for lib in self.libraries:
            for cls in lib.classes:
                for m in cls.methods:
                    yield lib, cls, m

    def iter_classes(self) -> Iterator[Tuple[LibraryIR, ClassIR]]:
        for lib in self.libraries:
            for cls in lib.classes:
                yield lib, cls

    def find_class(self, name: str) -> List[Tuple[LibraryIR, ClassIR]]:
        return [(l, c) for l, c in self.iter_classes() if c.name == name]

    def resolve(self, addr: int) -> Optional[Tuple[LibraryIR, ClassIR, MethodIR]]:
        return self.method_by_addr.get(addr)


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #

class AsmFileParser:
    """Two-pass-in-one-sweep parser for a single ``asm/**/*.dart`` file.

    Pass A (skeleton) and Pass B (bodies) are interleaved because the format is
    strictly nested; a real second pass would only re-read the same lines.
    """

    def __init__(self, path: str, collect_bodies: bool = True,
                 body_events: bool = False):
        self.path = path
        self.collect_bodies = collect_bodies
        self.body_events = body_events
        self.lib: Optional[LibraryIR] = None

        # transient state
        self._pending_cid: Optional[Tuple[int, int, Optional[int]]] = None
        self._pending_attrs: List[str] = []
        self._pending_decl: List[str] = []       # multi-line class decl buffer
        # Explicit scope stack of ("class"|"method"|"unknown", payload).  An
        # unrecognised `... {` pushes an "unknown" scope so that a single
        # unmatched line cannot desynchronise brace tracking for the rest of
        # the file (that bug silently swallowed 28 real methods in
        # chat/features/contacts/data/contacts_repository.dart).
        self._stack: List[Tuple[str, Any]] = []

    # ------------------------------------------------------------------ #
    def parse(self) -> LibraryIR:
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()

        self.lib = LibraryIR(url="", asm_path=self.path, n_lines=len(lines))

        for i, raw in enumerate(lines, 1):
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith("//"):
                self._comment_line(stripped, i)
            else:
                self._structure_line(stripped, i, raw)

        if not self.lib.url:
            # Header missing: fall back to the asm-relative path.  Recorded as
            # a warning, never silently invented.
            self.lib.url = "unknown:" + os.path.basename(self.path)
            self.lib.unparsed.append(UnparsedLine(self.path, 0, "<missing '// lib:' header>"))
        return self.lib

    # -- scope helpers -------------------------------------------------- #
    @property
    def _cls(self) -> Optional[ClassIR]:
        for kind, payload in reversed(self._stack):
            if kind == "class":
                return payload
        return None

    @property
    def _m(self) -> Optional[MethodIR]:
        if self._stack and self._stack[-1][0] == "method":
            return self._stack[-1][1]
        return None

    @property
    def _in_unknown(self) -> bool:
        return bool(self._stack) and self._stack[-1][0] == "unknown"

    # ------------------------------------------------------------------ #
    def _comment_line(self, s: str, lineno: int) -> None:
        assert self.lib is not None

        # inside a method body -> body facts
        if self._m is not None:
            self._body_line(s, lineno)
            return
        if self._in_unknown:
            return

        m = RE_LIB_HEADER.match(s)
        if m:
            self.lib.url = m.group("url")
            return

        m = RE_CLASS_ID.match(s)
        if m:
            self._pending_cid = (
                int(m.group("cid")),
                int(m.group("size"), 16),
                int(m.group("foff"), 16) if m.group("foff") else None,
            )
            self._pending_attrs = []
            return

        m = RE_CLASS_ATTR.match(s)
        if m and self._pending_cid is not None:
            for a in m.group("attrs").split(","):
                a = a.strip()
                if a:
                    self._pending_attrs.append(a)
            return

        # A stray "// ** addr" outside any method decl (top-level stub) -- keep
        # it out of the unparsed bucket, it is a known shape.
        if RE_ADDR.match(s):
            return

        if RE_BODY_SEMANTIC.match(s) or RE_BODY_RAW.match(s):
            return

        self.lib.unparsed.append(UnparsedLine(self.path, lineno, s))

    # ------------------------------------------------------------------ #
    def _structure_line(self, s: str, lineno: int, raw: str) -> None:
        assert self.lib is not None

        # closing brace
        if s == "}":
            if self._stack:
                self._stack.pop()
            return

        # multi-line class declaration continuation
        if self._pending_decl:
            self._pending_decl.append(s)
            joined = " ".join(self._pending_decl)
            if s.endswith("{"):
                self._pending_decl = []
                if not self._try_class(joined, lineno):
                    self.lib.unparsed.append(UnparsedLine(self.path, lineno, joined))
            return

        if not self._in_unknown and re.match(r"^(abstract\s+)?(class|enum|mixin)\s", s):
            if s.endswith("{"):
                if not self._try_class(s, lineno):
                    self._push_unknown(s, lineno)
            else:
                self._pending_decl = [s]
            return

        if self._cls is not None and not self._in_unknown:
            if "(" in s and s.endswith("{"):
                if self._try_method(s, lineno):
                    return
            elif s.endswith(";") or "; //" in s:
                if self._try_field(s, lineno):
                    return

        # Unrecognised.  If it opens a scope we must still track the brace, or
        # every following member in this file is lost.
        if s.endswith("{"):
            self._push_unknown(s, lineno)
        else:
            self.lib.unparsed.append(UnparsedLine(self.path, lineno, s))

    def _push_unknown(self, s: str, lineno: int) -> None:
        assert self.lib is not None
        self.lib.unparsed.append(UnparsedLine(self.path, lineno, s))
        self._stack.append(("unknown", s))

    # ------------------------------------------------------------------ #
    def _try_class(self, s: str, lineno: int) -> bool:
        assert self.lib is not None
        m = RE_CLASS_DECL.match(s)
        if not m:
            return False
        cid, size, foff = self._pending_cid or (None, None, None)
        impl = m.group("impl")
        cls = ClassIR(
            name=m.group("name"),
            kind=m.group("kw"),
            is_abstract=bool(m.group("abstract")),
            type_params=m.group("targs"),
            superclass=(m.group("super") or None),
            interfaces=[x.strip() for x in impl.split(",")] if impl else [],
            class_id=cid, size=size, field_offset=foff,
            attrs=list(self._pending_attrs),
            decl_line=lineno,
        )
        self.lib.classes.append(cls)
        self._stack.append(("class", cls))
        self._pending_cid = None
        self._pending_attrs = []
        return True

    # ------------------------------------------------------------------ #
    def _try_field(self, s: str, lineno: int) -> bool:
        assert self._cls is not None
        m = RE_FIELD_DECL.match(s)
        if not m:
            return False
        head = m.group("all")
        mods = set()
        while True:
            mm = RE_FIELD_MODS.match(head)
            if not mm:
                break
            mods.add(mm.group(1))
            head = head[mm.end():]
        vm_type = head.strip()
        if not vm_type:
            return False
        name = m.group("name")
        off = int(m.group("off"), 16) if m.group("off") else None

        is_static = "static" in mods
        if off is None:
            # `_Mint field_8;` -- offset is encoded in the placeholder name.
            mo = re.match(r"^field_([0-9a-fA-F]+)$", name)
            if mo:
                off = int(mo.group(1), 16)

        if re.match(r"^field_[0-9a-fA-F]+$", name):
            # DESTROYED: only the offset survived.  Do NOT store a fake name.
            fld = FieldIR(name=None, vm_type=vm_type, offset=off,
                          is_static=is_static, is_late="late" in mods,
                          is_final="final" in mods, is_const="const" in mods,
                          name_confidence=DESTROYED, decl_line=lineno)
        else:
            # RECOVERED: Blutter got the name from a pool `Field <...>` entry
            # (statics always, and `late` instance fields via their init stub).
            fld = FieldIR(name=name, vm_type=vm_type, offset=off,
                          is_static=is_static, is_late="late" in mods,
                          is_final="final" in mods, is_const="const" in mods,
                          name_confidence=RECOVERED, decl_line=lineno)
        self._cls.fields.append(fld)
        return True

    # ------------------------------------------------------------------ #
    def _try_method(self, s: str, lineno: int) -> bool:
        cls = self._cls
        assert cls is not None
        m = RE_METHOD_DECL.match(s)
        if not m:
            return False
        mods = (m.group("mods") or "").split()
        name = m.group("name")
        kind = "method"
        if "get" in mods:
            kind = "getter"
        elif "set" in mods:
            kind = "setter"
        elif "factory" in mods:
            kind = "factory"
        elif name.startswith("dyn:"):
            # Not a source-level member: an AOT dynamic-invocation forwarder.
            kind = "dyn_forwarder"
        elif re.fullmatch(_OPER, name.rsplit(".", 1)[-1]):
            kind = "operator"
        elif name == cls.name or name.startswith(cls.name + "."):
            # RECOVERED: a generative constructor -- Blutter prints it with the
            # class name, same as a factory but without the `factory` keyword.
            kind = "constructor"
        is_closure = bool(m.group("closure"))
        if is_closure and kind == "method":
            kind = "closure"

        ret = m.group("ret").strip()
        ret_conf = RECOVERED
        if ret == "_":
            ret, ret_conf = None, UNKNOWN

        params_raw = m.group("params").strip()
        if params_raw in ("/* No info */", ""):
            params = None if params_raw else []
        else:
            params = _split_top_level(params_raw)

        if name.endswith("=") and name not in ("[]=", "==", "<=", ">=") and kind == "method":
            kind = "setter"

        meth = MethodIR(
            name=name, kind=kind, type_params=m.group("mtargs"),
            is_static="static" in mods, is_closure=is_closure,
            async_modifier=m.group("amod"),
            return_type=ret, return_type_confidence=ret_conf,
            param_types=params,
            addr=None, size=None, decl_line=lineno, decl_text=s,
        )
        cls.methods.append(meth)
        self._stack.append(("method", meth))
        return True

    # ------------------------------------------------------------------ #
    # Pass B: body facts
    # ------------------------------------------------------------------ #
    def _body_line(self, s: str, lineno: int) -> None:
        m = self._m
        assert m is not None

        a = RE_ADDR.match(s)
        if a:
            m.addr = int(a.group("addr"), 16)
            m.size = int(a.group("size"), 16)
            return

        if not self.collect_bodies:
            return

        sem = RE_BODY_SEMANTIC.match(s)
        if sem:
            m.body.n_semantic_lines += 1
            self._semantic(m, int(sem.group("addr"), 16), sem.group("text"))
            return

        raw = RE_BODY_RAW.match(s)
        if raw:
            m.body.n_raw_lines += 1
            self._raw(m, int(raw.group("addr"), 16), raw.group("text"))
            return

    # ------------------------------------------------------------------ #
    def _semantic(self, m: MethodIR, addr: int, text: str) -> None:
        b = m.body
        kind = "asm"

        mm = RE_LOAD_FIELD.match(text)
        if mm:
            kind = "LoadField"
            b.field_access.append(FieldAccess(addr, "load", int(mm.group("off"), 16),
                                              int(mm.group("obj")), "r" + mm.group("dst")))
        else:
            mm = RE_STORE_FIELD.match(text)
            if mm:
                kind = "StoreField"
                b.field_access.append(FieldAccess(addr, "store", int(mm.group("off"), 16),
                                                  int(mm.group("obj")), mm.group("src")))
            else:
                mm = RE_STR_LOAD.match(text)
                if mm:
                    kind = "StringLiteral"
                    b.strings.append((addr, _unescape(mm.group("val"))))
                else:
                    mm = RE_INIT_ASYNC.match(text)
                    if mm:
                        kind = "InitAsync"
                        b.async_return_type = mm.group("type")
                    else:
                        mm = RE_LOAD_STATIC.match(text)
                        if mm:
                            kind = "LoadStaticField"
                        else:
                            mm = RE_ARRAY_STORE.match(text)
                            if mm:
                                kind = "ArrayStore"
                            else:
                                mm = RE_ARRAY_LOAD.match(text)
                                if mm:
                                    kind = "ArrayLoad"
                                else:
                                    mm = RE_SETUP_PARAMS.match(text)
                                    if mm:
                                        kind = "SetupParameters"
                                        for pm in RE_SETUP_ONE.finditer(mm.group("body")):
                                            b.param_setup.append(
                                                "%s %s [%s]" % (pm.group("type").strip(),
                                                                pm.group("name"),
                                                                pm.group("slots")))
                                    else:
                                        mm = RE_CALL_SEM.match(text)
                                        if mm:
                                            callee = mm.group("callee")
                                            kind = "Call"
                                            if callee.startswith("Allocate") and callee.endswith("Stub"):
                                                b.alloc_types.append(callee[len("Allocate"):-len("Stub")])

        if self.body_events:
            b.events.append(BodyEvent(addr, kind, text))

    # ------------------------------------------------------------------ #
    def _raw(self, m: MethodIR, addr: int, text: str) -> None:
        b = m.body

        # pool reference (may co-occur with a branch annotation)
        pm = RE_POOL_REF.search(text)
        if pm:
            payload = pm.group("payload")
            b.pool_refs.append(PoolRef(int(pm.group("off"), 16), payload, _pool_kind(payload)))
            fm = RE_POOL_FIELD.match(payload)
            if fm:
                b.static_fields.append(StaticFieldRef(
                    owner=fm.group("owner") or "",
                    name=fm.group("name"),
                    flags=fm.group("flags").strip(),
                    offset=int(fm.group("off"), 16),
                ))
            return

        bm = RE_BRANCH_TARGET.search(text)
        if bm:
            annot = bm.group("annot")
            edge = CallEdge(site_addr=addr, target_addr=int(bm.group("target"), 16),
                            annotation=annot)
            qm = RE_QUALIFIED_CALLEE.match(annot)
            if qm:
                edge.lib = qm.group("lib")
                edge.cls = qm.group("cls") or "::"
                edge.method = qm.group("method")
            elif annot.endswith("Stub") or "Stub" in annot:
                edge.stub = annot.split()[0]
                am = RE_ALLOC_STUB.match(annot)
                if am:
                    b.alloc_types.append(am.group("target"))
            b.calls.append(edge)


def _pool_kind(payload: str) -> str:
    for k in ("String:", "Field ", "Obj!", "TypeArguments:", "FunctionType:",
              "Type:", "AnonymousClosure:", "Stub:", "List("):
        if payload.startswith(k):
            return k.rstrip(":( ")
    if payload.startswith('"'):
        return "String"
    if payload == "Null":
        return "Null"
    return "raw"


def _unescape(s: str) -> str:
    return s.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')


def _split_top_level(s: str) -> List[str]:
    """Split a comma list, respecting <>, () and [] nesting.

    SEEN: `ContactsRepository, String, (dynamic) => Future<Y0>`
    """
    out: List[str] = []
    depth = 0
    cur: List[str] = []
    for ch in s:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return [x for x in out if x]


# --------------------------------------------------------------------------- #
# Directory walk + Stage 4 link
# --------------------------------------------------------------------------- #

def iter_asm_files(asm_root: str, packages: Optional[Iterable[str]] = None) -> Iterator[str]:
    """Yield ``asm/**/*.dart`` paths.  ``packages`` filters by top-level dir."""
    pkgs = set(packages) if packages else None
    if os.path.isfile(asm_root):
        yield asm_root
        return
    for dirpath, dirnames, filenames in os.walk(asm_root):
        if pkgs is not None:
            rel = os.path.relpath(dirpath, asm_root).replace("\\", "/")
            top = rel.split("/", 1)[0]
            if rel != "." and top not in pkgs:
                dirnames[:] = []
                continue
        dirnames.sort()
        for fn in sorted(filenames):
            if fn.endswith(".dart"):
                yield os.path.join(dirpath, fn)


def parse_file(path: str, collect_bodies: bool = True,
               body_events: bool = False) -> LibraryIR:
    return AsmFileParser(path, collect_bodies=collect_bodies,
                         body_events=body_events).parse()


def parse_tree(asm_root: str, packages: Optional[Iterable[str]] = None,
               collect_bodies: bool = True, body_events: bool = False,
               progress: Optional[Any] = None) -> Program:
    """Stage 3a over a tree, then Stage 4 link."""
    prog = Program()
    n = 0
    for path in iter_asm_files(asm_root, packages):
        lib = parse_file(path, collect_bodies=collect_bodies, body_events=body_events)
        prog.libraries.append(lib)
        n += 1
        if progress and n % 100 == 0:
            progress("parsed %d files..." % n)
    link(prog)
    return prog


def link(prog: Program) -> Program:
    """Stage 4: address index + call graph."""
    for lib, cls, m in prog.iter_methods():
        if m.addr is not None:
            prog.method_by_addr[m.addr] = (lib, cls, m)
    for lib, cls, m in prog.iter_methods():
        if m.addr is None:
            continue
        outs = prog.callees_of.setdefault(m.addr, [])
        for e in m.body.calls:
            if e.target_addr in prog.method_by_addr:
                outs.append(e.target_addr)
                prog.callers_of.setdefault(e.target_addr, []).append(m.addr)
    for d in (prog.callees_of, prog.callers_of):
        for k in list(d):
            d[k] = sorted(set(d[k]))
    return prog


# --------------------------------------------------------------------------- #
# Reporting helpers (consumed by cli.py)
# --------------------------------------------------------------------------- #

OBFUSCATED_NAME_RE = re.compile(r"^[A-Za-z]{1,3}\d*$")


def probe_obfuscation(prog: Program, sample: int = 200) -> Dict[str, Any]:
    """Design s1 obfuscation probe, run on the parsed model."""
    names: List[str] = []
    for _lib, _cls, m in prog.iter_methods():
        if m.is_closure or m.name.startswith("<"):
            continue
        names.append(m.name)
        if len(names) >= sample:
            break
    if not names:
        return {"obfuscated": False, "sampled": 0, "short_name_ratio": 0.0}
    short = sum(1 for x in names if OBFUSCATED_NAME_RE.match(x))
    ratio = short / float(len(names))
    return {"obfuscated": ratio > 0.30, "sampled": len(names),
            "short_name_ratio": round(ratio, 4)}


def coverage(prog: Program) -> Dict[str, Any]:
    n_lines = sum(l.n_lines for l in prog.libraries)
    n_unparsed = sum(len(l.unparsed) for l in prog.libraries)
    classes = fields = named_fields = destroyed_fields = 0
    methods = with_body = unknown_ret = unknown_params = 0
    for lib, cls in prog.iter_classes():
        classes += 1
        for f in cls.fields:
            fields += 1
            if f.name_confidence == DESTROYED:
                destroyed_fields += 1
            else:
                named_fields += 1
    for _lib, _cls, m in prog.iter_methods():
        methods += 1
        if m.has_body:
            with_body += 1
        if m.return_type is None:
            unknown_ret += 1
        if m.param_types is None:
            unknown_params += 1
    return {
        "files": len(prog.libraries),
        "packages": len(prog.packages()),
        "lines": n_lines,
        "unparsed_lines": n_unparsed,
        "parse_coverage": round(1.0 - (n_unparsed / float(n_lines or 1)), 6),
        "classes": classes,
        "fields": fields,
        "fields_named_RECOVERED": named_fields,
        "fields_name_DESTROYED": destroyed_fields,
        "methods": methods,
        "methods_with_body": with_body,
        "methods_return_type_UNKNOWN": unknown_ret,
        "methods_param_types_UNKNOWN": unknown_params,
        "call_graph_edges": sum(len(v) for v in prog.callees_of.values()),
        "indexed_addresses": len(prog.method_by_addr),
    }


def render_skeleton(lib: LibraryIR) -> str:
    """A read-only skeleton view of one library (not the stage-6 emitter)."""
    out: List[str] = []
    out.append("// url: %s" % lib.url)
    out.append("// asm: %s" % lib.asm_path)
    for cls in lib.classes:
        head = []
        if cls.is_abstract:
            head.append("abstract")
        head.append(cls.kind)
        head.append(cls.name + (cls.type_params or ""))
        if cls.superclass:
            head.append("extends " + cls.superclass)
        if cls.interfaces:
            head.append("implements " + ", ".join(cls.interfaces))
        out.append("")
        if cls.class_id is not None:
            out.append("// class id: %d, size: 0x%x%s" % (
                cls.class_id, cls.size or 0,
                (", attrs: " + "; ".join(cls.attrs)) if cls.attrs else ""))
        out.append(" ".join(head) + " {")
        for f in cls.fields:
            tag = "RECOVERED" if f.name_confidence == RECOVERED else "NAME-DESTROYED"
            mods = "".join(x for x, on in (("static ", f.is_static), ("late ", f.is_late),
                                           ("final ", f.is_final), ("const ", f.is_const)) if on)
            out.append("  %s%s %s; // offset: 0x%x  [%s]" % (
                mods, f.vm_type, f.placeholder_name, f.offset or 0, tag))
        if cls.fields and cls.methods:
            out.append("")
        for m in cls.methods:
            loc = ("0x%x" % m.addr) if m.addr is not None else "no-body"
            out.append("  %s;%s// %s" % (m.signature(), " " * 2, loc))
        out.append("}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Standalone entry point
# --------------------------------------------------------------------------- #

def _main(argv: List[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m flutter_decompile.parse_asm",
        description="Parse Blutter asm output into the flutter_decompile IR.")
    ap.add_argument("target", help="an asm/ directory or a single asm .dart file")
    ap.add_argument("--packages", help="comma list of top-level package dirs")
    ap.add_argument("--json", help="write the full model as JSON here")
    ap.add_argument("--skeleton", action="store_true", help="print skeletons")
    ap.add_argument("--no-bodies", action="store_true")
    args = ap.parse_args(argv)

    pkgs = args.packages.split(",") if args.packages else None
    prog = parse_tree(args.target, packages=pkgs,
                      collect_bodies=not args.no_bodies,
                      progress=lambda s: print("  " + s, file=sys.stderr))

    print(json.dumps(coverage(prog), indent=2))
    print(json.dumps(probe_obfuscation(prog), indent=2))
    if args.skeleton:
        for lib in prog.libraries:
            print()
            print(render_skeleton(lib))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"fingerprint": prog.blutter_fingerprint,
                       "coverage": coverage(prog),
                       "libraries": [l.to_dict() for l in prog.libraries]},
                      fh, indent=1)
        print("wrote %s" % args.json, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
