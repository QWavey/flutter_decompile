"""flutter_decompile.ir -- the program model.

This module holds ONLY dataclasses + tiny pure helpers.  It is the contract
between the parser half of the tool (asm/objs/pp -> facts) and the emitter half
(facts -> Dart source tree + reports).  Nothing here reads files, runs Blutter,
or guesses anything: every guess must arrive as an ``Evidence`` attached to the
thing it names.

Vocabulary (this is the whole point of the tool, so it lives in the type system):

    RECOVERED        the fact is literally present in the AOT snapshot
                     (class name, method name, enum name+ordinal, string
                     literal, static field name, class id, instance size).
    INFERRED_*       the fact was reconstructed from surrounding evidence.
                     It carries the exact evidence trail.  HIGH/MEDIUM/LOW.
    UNKNOWN          the fact is destroyed and no inference fired.  It is
                     emitted as a hole (``field_18``) or a throwing body -- never
                     as a silent placeholder.

Reference reality this model was checked against (Nexus app, Blutter output):

    // lib: , url: package:chat/core/crypto/kdf.dart
    // class id: 2247, size: 0x24, field offset: 0x8
    //   const constructor,
    class CallLogEntry extends Object {
      _Mint field_8;
      _OneByteString field_10;
      Map<String, dynamic> toJson(CallLogEntry) { ... }
    }
    enum SecurityLevel extends _Enum { ... }
    static late final List<int> _veilLabel; // offset: 0xe80
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

TOOL_NAME = "flutter_decompile"
TOOL_VERSION = "0.1"
IR_SCHEMA_VERSION = "0.1"

# ---------------------------------------------------------------------------
# confidence
# ---------------------------------------------------------------------------


class Confidence(enum.Enum):
    RECOVERED = "RECOVERED"
    INFERRED_HIGH = "INFERRED_HIGH"
    INFERRED_MEDIUM = "INFERRED_MEDIUM"
    INFERRED_LOW = "INFERRED_LOW"
    UNKNOWN = "UNKNOWN"

    @property
    def rank(self) -> int:
        return _CONF_RANK[self]

    @property
    def is_inferred(self) -> bool:
        return self in (
            Confidence.INFERRED_HIGH,
            Confidence.INFERRED_MEDIUM,
            Confidence.INFERRED_LOW,
        )

    def __lt__(self, other: "Confidence") -> bool:
        return self.rank < other.rank

    def __le__(self, other: "Confidence") -> bool:
        return self.rank <= other.rank

    def __gt__(self, other: "Confidence") -> bool:
        return self.rank > other.rank

    def __ge__(self, other: "Confidence") -> bool:
        return self.rank >= other.rank

    @classmethod
    def parse(cls, text: str) -> "Confidence":
        key = (text or "").strip().upper().replace("-", "_")
        aliases = {
            "HIGH": "INFERRED_HIGH",
            "MEDIUM": "INFERRED_MEDIUM",
            "MED": "INFERRED_MEDIUM",
            "LOW": "INFERRED_LOW",
        }
        key = aliases.get(key, key)
        try:
            return cls(key)
        except ValueError:
            raise ValueError(
                "unknown confidence %r (expected one of %s)"
                % (text, ", ".join(c.value for c in cls))
            )


_CONF_RANK = {
    Confidence.UNKNOWN: 0,
    Confidence.INFERRED_LOW: 1,
    Confidence.INFERRED_MEDIUM: 2,
    Confidence.INFERRED_HIGH: 3,
    Confidence.RECOVERED: 4,
}


def downgrade(conf: Confidence, steps: int = 1) -> Confidence:
    """Lower a confidence by `steps`, never below UNKNOWN, never above RECOVERED."""
    order = [
        Confidence.UNKNOWN,
        Confidence.INFERRED_LOW,
        Confidence.INFERRED_MEDIUM,
        Confidence.INFERRED_HIGH,
        Confidence.RECOVERED,
    ]
    idx = max(0, min(len(order) - 1, conf.rank - steps))
    return order[idx]


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    """Where a fact came from in the Blutter output."""

    asm_file: Optional[str] = None      # e.g. "asm/chat/features/call/data/call_log_store.dart"
    line: Optional[int] = None          # 1-based line in that file
    address: Optional[int] = None       # code address, e.g. 0x6cf240
    pool_ref: Optional[str] = None      # e.g. "pp+0xfeb0"
    obj_ref: Optional[str] = None       # e.g. "Obj!SecurityLevel@b6a121"

    def label(self) -> str:
        bits: List[str] = []
        if self.asm_file:
            bits.append(self.asm_file + (":%d" % self.line if self.line else ""))
        if self.address is not None:
            bits.append("@0x%x" % self.address)
        if self.pool_ref:
            bits.append("[%s]" % self.pool_ref)
        if self.obj_ref:
            bits.append(self.obj_ref)
        return " ".join(bits) if bits else "<no source ref>"

    def to_json(self) -> Dict[str, Any]:
        return {
            k: v
            for k, v in {
                "asm_file": self.asm_file,
                "line": self.line,
                "address": ("0x%x" % self.address) if self.address is not None else None,
                "pool_ref": self.pool_ref,
                "obj_ref": self.obj_ref,
            }.items()
            if v is not None
        }


@dataclass
class Evidence:
    """One reason to believe something.  Rules produce these; nothing else may."""

    rule: str                    # stable rule id, e.g. "json_key_pairing"
    detail: str                  # human sentence, e.g. 'map key "peer" precedes load of field_7'
    confidence: Confidence = Confidence.INFERRED_MEDIUM
    weight: float = 1.0
    source: Optional[SourceRef] = None

    def render(self) -> str:
        where = (" (%s)" % self.source.label()) if self.source else ""
        return "%s: %s%s" % (self.rule, self.detail, where)

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "rule": self.rule,
            "detail": self.detail,
            "confidence": self.confidence.value,
            "weight": self.weight,
        }
        if self.source:
            out["source"] = self.source.to_json()
        return out


# ---------------------------------------------------------------------------
# types
# ---------------------------------------------------------------------------

#: VM-internal (lowered) type names -> the Dart type the developer wrote.
#: Blutter prints the *VM* class of a field, so `_OneByteString field_10` means
#: "at run time this slot held a one-byte String" -- the declaration might have
#: been `String`, `String?`, `Object`, or a type parameter.  We keep the raw
#: string in the emitted comment and never pretend otherwise.
VM_TO_DART: Dict[str, str] = {
    "_OneByteString": "String",
    "_TwoByteString": "String",
    "_StringBase": "String",
    "_ExternalOneByteString": "String",
    "String": "String",
    "_Mint": "int",
    "_Smi": "int",
    "int": "int",
    "_IntegerImplementation": "int",
    "_Double": "double",
    "double": "double",
    "bool": "bool",
    "_Bool": "bool",
    "Null": "Null",
    "_List": "List",
    "_GrowableList": "List",
    "_ImmutableList": "List",
    "_TypedList": "List<int>",
    "_Type": "Type",
    "_FunctionType": "Function",
    "_Closure": "Function",
    "_Map": "Map",
    "_ConstMap": "Map",
    "_InternalLinkedHashMap": "Map",
    "_Set": "Set",
    "_ConstSet": "Set",
    "Object": "Object",
    "dynamic": "dynamic",
    "void": "void",
    "_": "dynamic",
    "": "dynamic",
}

#: SDK types that are safe to name in emitted code, with the import they need.
SDK_TYPE_IMPORTS: Dict[str, str] = {
    "Uint8List": "dart:typed_data",
    "Int8List": "dart:typed_data",
    "Uint16List": "dart:typed_data",
    "Int32List": "dart:typed_data",
    "Int64List": "dart:typed_data",
    "Uint32List": "dart:typed_data",
    "Uint64List": "dart:typed_data",
    "Float32List": "dart:typed_data",
    "Float64List": "dart:typed_data",
    "ByteData": "dart:typed_data",
    "ByteBuffer": "dart:typed_data",
    "Future": "dart:async",
    "Stream": "dart:async",
    "Completer": "dart:async",
    "StreamController": "dart:async",
    "StreamSubscription": "dart:async",
    "Timer": "dart:async",
    "FutureOr": "dart:async",
}

#: dart:core names that need no import.
CORE_TYPES = {
    "Object", "Comparable", "String", "int", "double", "num", "bool", "List",
    "Map", "Set", "Iterable", "Iterator", "Function", "Type", "Symbol", "Null",
    "dynamic", "void", "Duration", "DateTime", "Uri", "RegExp", "StringBuffer",
    "Exception", "Error", "StackTrace", "BigInt", "Runes", "MapEntry",
    "Pattern", "Match", "Enum", "Record", "Never",
}

DART_KEYWORDS = {
    "abstract", "as", "assert", "async", "await", "base", "break", "case",
    "catch", "class", "const", "continue", "covariant", "default", "deferred",
    "do", "dynamic", "else", "enum", "export", "extends", "extension",
    "external", "factory", "false", "final", "finally", "for", "Function",
    "get", "hide", "if", "implements", "import", "in", "interface", "is",
    "late", "library", "mixin", "new", "null", "on", "operator", "part",
    "required", "rethrow", "return", "sealed", "set", "show", "static",
    "super", "switch", "sync", "this", "throw", "true", "try", "typedef",
    "var", "void", "when", "while", "with", "yield",
}

_GENERIC_RE = re.compile(r"^([^<]+)<(.*)>$", re.S)


def split_generic(raw: str) -> Tuple[str, List[str]]:
    """`Map<String, dynamic>` -> ("Map", ["String", "dynamic"]).  Nesting aware."""
    raw = (raw or "").strip()
    m = _GENERIC_RE.match(raw)
    if not m:
        return raw, []
    base, inner = m.group(1).strip(), m.group(2)
    args: List[str] = []
    depth = 0
    buf = ""
    for ch in inner:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        args.append(buf.strip())
    return base, args


def demangle(name: str) -> str:
    """Strip Blutter/VM name mangling: `_toJson@940078579` -> `_toJson`."""
    if not name:
        return name
    return name.split("@", 1)[0]


def flatten_mixin(name: str) -> Tuple[str, Optional[str]]:
    """`_Foo&Bar&Baz` -> ("_Foo", "flattened mixin application _Foo&Bar&Baz")."""
    if "&" in name:
        return name.split("&", 1)[0], "flattened mixin application " + name
    return name, None


def vm_to_dart(raw: str) -> Tuple[str, Optional[str]]:
    """Map a VM/Blutter type string to a Dart type.

    Returns (dart_type, note).  `note` is non-None when information was lost or
    changed in the mapping and therefore has to be shown in a comment.
    """
    raw = (raw or "").strip()
    if not raw or raw in ("_", "/* No info */"):
        return "dynamic", "no type info in snapshot"
    if raw.endswith("?"):
        inner, note = vm_to_dart(raw[:-1])
        return inner + "?", note
    base, args = split_generic(raw)
    base, mixnote = flatten_mixin(base)
    mapped = VM_TO_DART.get(base)
    note = mixnote
    if mapped is None:
        mapped = base
    elif mapped != base:
        note = "lowered VM type `%s`" % raw if note is None else note
    if args:
        conv = [vm_to_dart(a)[0] for a in args]
        mapped_base, mapped_args = split_generic(mapped)
        if mapped_args:
            # e.g. `_TypedList` -> `List<int>`: keep the mapped arguments.
            return mapped, note
        return "%s<%s>" % (mapped_base, ", ".join(conv)), note
    return mapped, note


def sanitize_identifier(name: str, *, private_ok: bool = True) -> str:
    """Make a Dart-legal identifier out of a recovered/inferred string."""
    name = demangle(name or "")
    name = re.sub(r"[^A-Za-z0-9_$]", "_", name)
    if not name:
        return "_unnamed"
    if name[0].isdigit():
        name = "n" + name
    if not private_ok and name.startswith("_"):
        name = name.lstrip("_") or "_unnamed"
    if name in DART_KEYWORDS:
        name = name + "_"
    return name


_ESCAPES = {
    "\\": "\\\\",
    "'": "\\'",
    "$": "\\$",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
    "\v": "\\v",
    "\x00": "\\x00",
}


def escape_dart_string(value: str) -> str:
    """Emit a string literal that is byte-for-byte the recovered constant.

    String constants are RECOVERED facts -- they are emitted verbatim, only
    escaped so Dart can parse them back to exactly the same bytes.
    """
    out = []
    for ch in value:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\u{%x}" % ord(ch))
        else:
            out.append(ch)
    return "'" + "".join(out) + "'"


# ---------------------------------------------------------------------------
# body facts (produced by the parser's pass B -- an event list, NOT a decompile)
# ---------------------------------------------------------------------------


class EventKind(str, enum.Enum):
    LOAD_FIELD = "load_field"        # LoadField: rX = rY->field_N
    STORE_FIELD = "store_field"      # StoreField: rX->field_N = rZ
    ARRAY_STORE = "array_store"      # ArrayStore: rX[i] = rY
    ARRAY_LOAD = "array_load"
    STRING = "string"                # r16 = "literal"
    CALL = "call"                    # bl #0x... ; [package:...] Class::method
    ALLOC = "alloc"                  # r0 = AllocateArray()/AllocateObject()
    STATIC_LOAD = "static_load"      # LoadStaticField(0xNNN)
    STATIC_NAME = "static_name"      # r2 = <FieldName>  (pool `Field <...>`)
    CONST = "const"                  # r0 = <int/bool/Obj literal>
    COMPARE = "compare"
    RETURN = "return"
    OTHER = "other"


@dataclass
class BodyEvent:
    """One semantic line Blutter emitted inside a method body.

    This is deliberately shallow.  We do not build an AST, because the snapshot
    does not contain one.  Inference rules read these in order.
    """

    kind: EventKind
    address: Optional[int] = None
    dst: Optional[str] = None            # destination register, e.g. "r0"
    src: Optional[str] = None            # source/base register, e.g. "r3"
    offset: Optional[int] = None         # field offset as printed (tagged)
    value: Optional[str] = None          # string literal / const text
    target: Optional[str] = None         # call target, "package:chat/..::Class::m"
    index: Optional[int] = None          # array index for ARRAY_STORE/LOAD
    raw: str = ""
    line: Optional[int] = None

    def to_json(self) -> Dict[str, Any]:
        d = {
            "kind": self.kind.value,
            "address": ("0x%x" % self.address) if self.address is not None else None,
            "dst": self.dst,
            "src": self.src,
            "offset": ("0x%x" % self.offset) if self.offset is not None else None,
            "value": self.value,
            "target": self.target,
            "index": self.index,
            "line": self.line,
        }
        return {k: v for k, v in d.items() if v is not None}


# ---------------------------------------------------------------------------
# fields
# ---------------------------------------------------------------------------

#: Blutter prints *tagged* offsets inside bodies (`r3->field_7`) and *untagged*
#: offsets in declarations (`_Mint field_8;`).  Dart object pointers carry a low
#: tag bit, so a body offset is the declared offset minus one.  Verified on
#: CallLogEntry (class size 0x24, field offset 0x8): the body loads field_7,
#: field_b, field_f, field_13, field_1b -> declared 0x8, 0xc, 0x10, 0x14, 0x1c.
POINTER_TAG_ADJUST = 1


class OffsetSpace(str, enum.Enum):
    DECLARED = "declared"   # as printed on a field declaration line
    TAGGED = "tagged"       # as printed inside a body (LoadField/StoreField)


def canonical_offset(raw_offset: int, space: OffsetSpace) -> int:
    return raw_offset + POINTER_TAG_ADJUST if space == OffsetSpace.TAGGED else raw_offset


@dataclass
class FieldIR:
    """An instance or static field.

    Instance field NAMES DO NOT EXIST in an AOT snapshot -- only offsets do.
    `recovered_name` is therefore only ever set for *static* fields, whose names
    survive in the object pool as `Field <::._veilLabel@823249941>`.
    """

    offset: int                                   # canonical (declared-space) offset
    vm_type: str = "_"
    is_static: bool = False
    is_late: bool = False
    is_final_hint: bool = False                   # from pool flags, e.g. "static late final"
    recovered_name: Optional[str] = None          # RECOVERED (statics only)
    inferred_name: Optional[str] = None           # INFERRED_* (instance fields)
    name_confidence: Confidence = Confidence.UNKNOWN
    evidence: List[Evidence] = dc_field(default_factory=list)
    rejected: List[str] = dc_field(default_factory=list)   # why a candidate lost
    source: Optional[SourceRef] = None
    static_slot: Optional[int] = None             # field-table slot, e.g. 0xe80
    owner: Optional[str] = None                   # set at link time

    @property
    def placeholder_name(self) -> str:
        return "field_%x" % self.offset

    @property
    def name(self) -> str:
        return self.recovered_name or self.inferred_name or self.placeholder_name

    @property
    def has_real_name(self) -> bool:
        return bool(self.recovered_name or self.inferred_name)

    @property
    def confidence(self) -> Confidence:
        if self.recovered_name:
            return Confidence.RECOVERED
        if self.inferred_name:
            return self.name_confidence
        return Confidence.UNKNOWN

    def dart_type(self) -> Tuple[str, Optional[str]]:
        return vm_to_dart(self.vm_type)

    def to_json(self) -> Dict[str, Any]:
        return {
            "offset": "0x%x" % self.offset,
            "name": self.name,
            "placeholder": self.placeholder_name,
            "vm_type": self.vm_type,
            "dart_type": self.dart_type()[0],
            "is_static": self.is_static,
            "confidence": self.confidence.value,
            "evidence": [e.to_json() for e in self.evidence],
            "rejected": self.rejected,
            "source": self.source.to_json() if self.source else None,
        }


# ---------------------------------------------------------------------------
# methods
# ---------------------------------------------------------------------------


class MethodKind(str, enum.Enum):
    METHOD = "method"
    GETTER = "getter"
    SETTER = "setter"
    CONSTRUCTOR = "constructor"
    FACTORY = "factory"
    CLOSURE = "closure"
    OPERATOR = "operator"


class Modifier(str, enum.Enum):
    NONE = ""
    ASYNC = "async"
    ASYNC_STAR = "async*"
    SYNC_STAR = "sync*"


class BodyStatus(str, enum.Enum):
    #: The body exists in the snapshot only as machine code.  This is the normal
    #: case and the tool emits a throwing stub for it.
    UNRECONSTRUCTED = "unreconstructed"
    #: Blutter emitted no body at all (abstract / stub-only entry).
    NO_BODY_IN_SNAPSHOT = "no_body_in_snapshot"
    #: A body was rebuilt from evidence strong enough to be Dart.  Nothing in
    #: the emitter half produces this today; it exists so a future lifter can.
    RECONSTRUCTED = "reconstructed"


@dataclass
class ParamIR:
    index: int
    vm_type: str = "_"
    name: Optional[str] = None            # positional names are DESTROYED
    name_confidence: Confidence = Confidence.UNKNOWN
    is_named: bool = False                # named param names survive at call sites
    is_receiver: bool = False             # arg0 == `this` for instance methods
    is_required: bool = False
    evidence: List[Evidence] = dc_field(default_factory=list)

    def emitted_name(self) -> str:
        if self.name:
            return sanitize_identifier(self.name)
        return "a%d" % self.index

    def to_json(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "vm_type": self.vm_type,
            "name": self.name,
            "emitted_name": self.emitted_name(),
            "is_named": self.is_named,
            "is_receiver": self.is_receiver,
            "confidence": self.name_confidence.value,
        }


@dataclass
class MethodIR:
    name: str
    kind: MethodKind = MethodKind.METHOD
    is_static: bool = False
    modifier: Modifier = Modifier.NONE
    return_vm_type: str = "_"
    params: List[ParamIR] = dc_field(default_factory=list)
    params_known: bool = False            # False when Blutter printed "/* No info */"
    address: Optional[int] = None
    size: Optional[int] = None
    body_status: BodyStatus = BodyStatus.UNRECONSTRUCTED
    body_dart: Optional[str] = None       # only when body_status == RECONSTRUCTED
    events: List[BodyEvent] = dc_field(default_factory=list)
    calls: List[str] = dc_field(default_factory=list)
    pool_refs: List[str] = dc_field(default_factory=list)
    closure_owner: Optional[str] = None   # for CLOSURE: enclosing function name
    source: Optional[SourceRef] = None
    owner: Optional[str] = None           # set at link time
    notes: List[str] = dc_field(default_factory=list)

    @property
    def clean_name(self) -> str:
        return demangle(self.name)

    @property
    def is_async(self) -> bool:
        return self.modifier == Modifier.ASYNC

    def qualified(self) -> str:
        return "%s::%s" % (self.owner or "::", self.clean_name)

    def to_json(self) -> Dict[str, Any]:
        return {
            "name": self.clean_name,
            "raw_name": self.name,
            "kind": self.kind.value,
            "is_static": self.is_static,
            "modifier": self.modifier.value,
            "return_vm_type": self.return_vm_type,
            "params_known": self.params_known,
            "params": [p.to_json() for p in self.params],
            "address": ("0x%x" % self.address) if self.address is not None else None,
            "size": self.size,
            "body_status": self.body_status.value,
            "events": len(self.events),
            "calls": self.calls,
            "source": self.source.to_json() if self.source else None,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# enums
# ---------------------------------------------------------------------------


@dataclass
class EnumValueIR:
    """One enum constant.

    Both parts are RECOVERED: `objs.txt` stores the const instance as
    `Super!_Enum { off_8: int(ordinal), off_10: "name" }`.
    """

    name: str
    ordinal: int
    obj_address: Optional[str] = None
    extra: Dict[int, Any] = dc_field(default_factory=dict)   # other const field slots
    source: Optional[SourceRef] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "ordinal": self.ordinal,
            "obj_address": self.obj_address,
            "extra": {("0x%x" % k): v for k, v in sorted(self.extra.items())},
            "source": self.source.to_json() if self.source else None,
        }


# ---------------------------------------------------------------------------
# classes / libraries / program
# ---------------------------------------------------------------------------


@dataclass
class ClassIR:
    name: str
    super_name: Optional[str] = "Object"
    class_id: Optional[int] = None
    instance_size: Optional[int] = None
    field_offset_base: Optional[int] = None
    has_const_ctor: bool = False
    is_enum: bool = False
    is_abstract: bool = False
    is_library_scope: bool = False        # Blutter's `class :: { }` pseudo-class
    type_params: List[str] = dc_field(default_factory=list)
    fields: Dict[int, FieldIR] = dc_field(default_factory=dict)   # canonical offset -> field
    methods: List[MethodIR] = dc_field(default_factory=list)
    enum_values: List[EnumValueIR] = dc_field(default_factory=list)
    source: Optional[SourceRef] = None
    library_url: Optional[str] = None
    notes: List[str] = dc_field(default_factory=list)
    mixin_of: Optional[str] = None

    # -- helpers ------------------------------------------------------------
    def field(self, offset: int) -> Optional[FieldIR]:
        return self.fields.get(offset)

    def ensure_field(self, offset: int, vm_type: str = "_") -> FieldIR:
        f = self.fields.get(offset)
        if f is None:
            f = FieldIR(offset=offset, vm_type=vm_type, owner=self.name)
            self.fields[offset] = f
        elif f.vm_type in ("_", "") and vm_type not in ("_", ""):
            f.vm_type = vm_type
        return f

    def instance_fields(self) -> List[FieldIR]:
        return [f for _, f in sorted(self.fields.items()) if not f.is_static]

    def static_fields(self) -> List[FieldIR]:
        return [f for _, f in sorted(self.fields.items()) if f.is_static]

    def method_names(self) -> List[str]:
        return [m.clean_name for m in self.methods]

    def find_methods(self, *names: str) -> List[MethodIR]:
        want = {n.lower() for n in names}
        return [m for m in self.methods if m.clean_name.lower().lstrip("_") in want
                or m.clean_name.lower() in want]

    def enum_ordinal_gaps(self) -> List[int]:
        """Ordinals missing from a contiguous 0..max run.

        A gap means the constant was tree-shaken or never const-constructed --
        it is NOT proof the source lacked that value.
        """
        if not self.enum_values:
            return []
        present = {v.ordinal for v in self.enum_values}
        top = max(present)
        return [i for i in range(top + 1) if i not in present]

    def to_json(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "super": self.super_name,
            "class_id": self.class_id,
            "instance_size": ("0x%x" % self.instance_size) if self.instance_size else None,
            "is_enum": self.is_enum,
            "is_library_scope": self.is_library_scope,
            "has_const_ctor": self.has_const_ctor,
            "fields": [f.to_json() for f in sorted(self.fields.values(), key=lambda x: x.offset)],
            "methods": [m.to_json() for m in self.methods],
            "enum_values": [v.to_json() for v in sorted(self.enum_values, key=lambda v: v.ordinal)],
            "enum_ordinal_gaps": self.enum_ordinal_gaps(),
            "source": self.source.to_json() if self.source else None,
            "notes": self.notes,
        }


@dataclass
class ConstObjectIR:
    """A const object graph from objs.txt whose class we could not match."""

    class_name: str
    address: str
    slots: Dict[int, Any] = dc_field(default_factory=dict)
    source: Optional[SourceRef] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "class_name": self.class_name,
            "address": self.address,
            "slots": {("0x%x" % k): v for k, v in sorted(self.slots.items())},
        }


@dataclass
class UnparsedLine:
    asm_file: str
    line: int
    text: str

    def to_json(self) -> Dict[str, Any]:
        return {"asm_file": self.asm_file, "line": self.line, "text": self.text[:200]}


@dataclass
class LibraryIR:
    url: str                                   # "package:chat/core/crypto/kdf.dart"
    classes: List[ClassIR] = dc_field(default_factory=list)
    top: Optional[ClassIR] = None              # the `class :: {}` library scope
    asm_file: Optional[str] = None
    unparsed: List[UnparsedLine] = dc_field(default_factory=list)
    notes: List[str] = dc_field(default_factory=list)

    @property
    def package(self) -> str:
        m = re.match(r"^package:([^/]+)/", self.url or "")
        if m:
            return m.group(1)
        if (self.url or "").startswith("dart:"):
            return self.url.split(":", 1)[1].split("/")[0]
        return "<unknown>"

    @property
    def rel_path(self) -> str:
        """`package:chat/core/crypto/kdf.dart` -> `core/crypto/kdf.dart`."""
        m = re.match(r"^package:[^/]+/(.*)$", self.url or "")
        if m:
            return m.group(1)
        if (self.url or "").startswith("dart:"):
            return self.url.replace(":", "_") + ".dart"
        safe = re.sub(r"[^A-Za-z0-9_./-]", "_", self.url or "unknown")
        return safe if safe.endswith(".dart") else safe + ".dart"

    def all_classes(self) -> List[ClassIR]:
        out = list(self.classes)
        if self.top is not None:
            out.insert(0, self.top)
        return out

    def all_methods(self) -> Iterator[MethodIR]:
        for c in self.all_classes():
            for m in c.methods:
                yield m

    def to_json(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "package": self.package,
            "rel_path": self.rel_path,
            "asm_file": self.asm_file,
            "classes": [c.to_json() for c in self.all_classes()],
            "unparsed": [u.to_json() for u in self.unparsed],
            "notes": self.notes,
        }


@dataclass
class ProgramMeta:
    input_name: str = "<unknown>"
    dart_version: Optional[str] = None
    snapshot_hash: Optional[str] = None
    version_signal: Optional[str] = None      # which of the 3 signals won (see design 1)
    blutter_version: Optional[str] = None
    blutter_out: Optional[str] = None
    abi: str = "arm64-v8a"
    obfuscated: bool = False
    generated_at: Optional[str] = None
    tool_version: str = TOOL_VERSION
    ir_schema: str = IR_SCHEMA_VERSION
    parse_lines_total: int = 0
    parse_lines_unparsed: int = 0

    @property
    def parse_coverage(self) -> float:
        if not self.parse_lines_total:
            return 0.0
        return 1.0 - (self.parse_lines_unparsed / float(self.parse_lines_total))

    def to_json(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["parse_coverage"] = round(self.parse_coverage, 4)
        return d


@dataclass
class ProgramIR:
    meta: ProgramMeta = dc_field(default_factory=ProgramMeta)
    libraries: List[LibraryIR] = dc_field(default_factory=list)
    orphan_consts: List[ConstObjectIR] = dc_field(default_factory=list)
    pool_strings: Dict[str, str] = dc_field(default_factory=dict)   # "pp+0xfeb0" -> value

    # -- helpers ------------------------------------------------------------
    def all_classes(self) -> Iterator[Tuple[LibraryIR, ClassIR]]:
        for lib in self.libraries:
            for c in lib.all_classes():
                yield lib, c

    def all_methods(self) -> Iterator[Tuple[LibraryIR, ClassIR, MethodIR]]:
        for lib, c in self.all_classes():
            for m in c.methods:
                yield lib, c, m

    def all_fields(self) -> Iterator[Tuple[LibraryIR, ClassIR, FieldIR]]:
        for lib, c in self.all_classes():
            for _, f in sorted(c.fields.items()):
                yield lib, c, f

    def find_class(self, name: str) -> Optional[ClassIR]:
        for _, c in self.all_classes():
            if c.name == name:
                return c
        return None

    def link(self) -> None:
        """Backfill owner pointers.  Idempotent; safe to call repeatedly."""
        for lib in self.libraries:
            for c in lib.all_classes():
                c.library_url = c.library_url or lib.url
                for f in c.fields.values():
                    f.owner = c.name
                for m in c.methods:
                    m.owner = c.name

    def to_json(self) -> Dict[str, Any]:
        return {
            "meta": self.meta.to_json(),
            "libraries": [l.to_json() for l in self.libraries],
            "orphan_consts": [o.to_json() for o in self.orphan_consts],
            "pool_strings": len(self.pool_strings),
        }


__all__ = [
    "TOOL_NAME", "TOOL_VERSION", "IR_SCHEMA_VERSION",
    "Confidence", "downgrade", "SourceRef", "Evidence",
    "EventKind", "BodyEvent", "OffsetSpace", "canonical_offset",
    "POINTER_TAG_ADJUST", "FieldIR", "MethodKind", "Modifier", "BodyStatus",
    "ParamIR", "MethodIR", "EnumValueIR", "ClassIR", "ConstObjectIR",
    "UnparsedLine", "LibraryIR", "ProgramMeta", "ProgramIR",
    "vm_to_dart", "split_generic", "demangle", "flatten_mixin",
    "sanitize_identifier", "escape_dart_string",
    "VM_TO_DART", "SDK_TYPE_IMPORTS", "CORE_TYPES", "DART_KEYWORDS",
]
