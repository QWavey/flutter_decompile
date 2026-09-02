"""flutter_decompile.infer -- instance-field name inference.

THE PROBLEM
-----------
Instance field names are **destroyed** by AOT compilation.  Blutter can only
print offsets::

    class CallLogEntry extends Object {   // class id: 2247, size: 0x24
      ...
      // 0x6cf280: LoadField: r0 = r3->field_7
    }

THE ONLY HONEST WAY BACK
------------------------
Names sometimes leak *indirectly*, through string constants that the compiler
could not remove.  The canonical case, taken verbatim from the Nexus snapshot
(``asm/chat/features/call/data/call_log_store.dart``, ``CallLogEntry.toJson``
@0x6cf240)::

    r16 = "peer"                 -> stored into the map-literal array
    LoadField: r0 = r3->field_7  -> the value that follows that key
    r16 = "dir"
    LoadField: r0 = r3->field_b
    r16 = "outcome"
    LoadField: r0 = r3->field_f
    r16 = "at"
    LoadField: r4 = r3->field_13
    r16 = "dur"
    LoadField: r4 = r3->field_1b

The key/value interleaving of a Dart map literal is a *compiler* invariant, not
a guess, so pairing key N with the field loaded for value N is strong evidence.
It is still evidence, not recovery: the JSON key may differ from the field name
(``@JsonKey(name: ...)``), so every applied name carries its evidence trail and
a confidence, and the emitter prints both.

RULES IMPLEMENTED
-----------------
  json_key_pairing    toJson/toMap map-literal key -> following field load   HIGH
  from_json_pairing   fromJson/fromMap key -> following field store          MED/HIGH
  getter_forward      `T get foo` whose body is one field load               HIGH
  setter_forward      `set foo(v)` whose body is one field store             HIGH
  vm_enum_layout      _Enum slots 0x8/0x10 are `index` / `_name`             HIGH
  tostring_label      "foo: " in toString -> following field load            MEDIUM
  map_key_nearby      any lowerCamel string literal -> next field load       LOW

Everything below MEDIUM is off unless --infer-fields aggressive.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .ir import (
    BodyEvent,
    ClassIR,
    Confidence,
    Evidence,
    EventKind,
    MethodIR,
    MethodKind,
    ProgramIR,
    SourceRef,
    downgrade,
    sanitize_identifier,
)

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LOWER_CAMEL_RE = re.compile(r"^_?[a-z][A-Za-z0-9]*$")
SETUP_THIS_RE = re.compile(r"this\s*/\*\s*(r\d+)\s*=>\s*(r\d+)")
TOSTRING_LABEL_RE = re.compile(r"^[\s,({\[]*([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*$")

JSON_OUT_NAMES = {"tojson", "tomap", "asmap", "asjson", "encode", "toentry"}
JSON_IN_NAMES = {"fromjson", "frommap", "parse", "decode", "fromentry"}


def _norm_method_name(m: MethodIR) -> str:
    return m.clean_name.lower().lstrip("_")


def receiver_register(method: MethodIR) -> Optional[str]:
    """Which register holds `this` in this body.

    Preference order:
      1. the most frequent base register of LoadField events
      2. the SetupParameters line, when the parser kept it
         (``SetupParameters(CallLogEntry this /* r1 => r0, fp-0x8 */)``)
      3. the most frequent base register of StoreField events

    Loads come first on purpose, and the SetupParameters register is only a
    fallback.  Two facts from the real Nexus snapshot force that order:

      * in ``CallLogEntry.toJson`` the receiver is set up in r0 and then
        re-materialised into r3 (``ldur x3, [fp, #-8]``) before every field
        load, so the SetupParameters register is stale by the time it matters;
      * the StoreField events in that same body target the freshly allocated
        map-literal array (r2), not `this`, so counting loads and stores
        together picks the array and every key/field pairing goes wrong.
    """
    loads: Counter = Counter()
    stores: Counter = Counter()
    for ev in method.events:
        if ev.kind == EventKind.LOAD_FIELD and ev.src:
            loads[ev.src] += 1
        elif ev.kind == EventKind.STORE_FIELD and ev.dst:
            stores[ev.dst] += 1
    if loads:
        return loads.most_common(1)[0][0]
    for ev in method.events:
        if ev.raw and "SetupParameters" in ev.raw:
            m = SETUP_THIS_RE.search(ev.raw)
            if m:
                return m.group(2)
    if stores:
        return stores.most_common(1)[0][0]
    return None


def _ev_source(method: MethodIR, ev: BodyEvent) -> SourceRef:
    base = method.source or SourceRef()
    return SourceRef(
        asm_file=base.asm_file,
        line=ev.line if ev.line is not None else base.line,
        address=ev.address if ev.address is not None else base.address,
    )


# ---------------------------------------------------------------------------
# candidates / decisions
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    offset: int
    name: str
    rule: str
    confidence: Confidence
    weight: float
    detail: str
    source: Optional[SourceRef] = None

    def as_evidence(self) -> Evidence:
        return Evidence(
            rule=self.rule,
            detail=self.detail,
            confidence=self.confidence,
            weight=self.weight,
            source=self.source,
        )


@dataclass
class FieldDecision:
    class_name: str
    offset: int
    winner: Optional[str] = None
    winner_weight: float = 0.0
    runner_up: Optional[str] = None
    runner_up_weight: float = 0.0
    final_confidence: Confidence = Confidence.UNKNOWN
    accepted: bool = False
    reason: str = ""
    candidates: List[Candidate] = dc_field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "class": self.class_name,
            "offset": "0x%x" % self.offset,
            "winner": self.winner,
            "winner_weight": round(self.winner_weight, 3),
            "runner_up": self.runner_up,
            "runner_up_weight": round(self.runner_up_weight, 3),
            "confidence": self.final_confidence.value,
            "accepted": self.accepted,
            "reason": self.reason,
            "candidates": [
                {
                    "name": c.name,
                    "rule": c.rule,
                    "confidence": c.confidence.value,
                    "weight": c.weight,
                    "detail": c.detail,
                    "source": c.source.to_json() if c.source else None,
                }
                for c in self.candidates
            ],
        }


@dataclass
class InferenceResult:
    mode: str = "safe"
    min_confidence: Confidence = Confidence.INFERRED_MEDIUM
    decisions: List[FieldDecision] = dc_field(default_factory=list)
    by_rule: Counter = dc_field(default_factory=Counter)
    by_confidence: Counter = dc_field(default_factory=Counter)
    applied: int = 0
    rejected: int = 0
    fields_seen: int = 0

    def to_json(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "min_confidence": self.min_confidence.value,
            "fields_considered": self.fields_seen,
            "applied": self.applied,
            "rejected": self.rejected,
            "by_rule": dict(self.by_rule),
            "by_confidence": {k.value if isinstance(k, Confidence) else str(k): v
                              for k, v in self.by_confidence.items()},
            "decisions": [d.to_json() for d in self.decisions],
        }


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------


def rule_json_key_pairing(cls: ClassIR) -> List[Candidate]:
    """Map-literal key -> the field loaded for the matching value slot."""
    out: List[Candidate] = []
    for m in cls.methods:
        if _norm_method_name(m) not in JSON_OUT_NAMES:
            continue
        this_reg = receiver_register(m)
        if not this_reg:
            continue
        pending: Optional[BodyEvent] = None
        seen_here: Dict[int, str] = {}
        for ev in m.events:
            if ev.kind == EventKind.STRING and ev.value is not None:
                pending = ev
            elif ev.kind == EventKind.LOAD_FIELD and ev.src == this_reg:
                if pending is None or ev.offset is None:
                    continue
                key = pending.value or ""
                pending = None
                if not IDENT_RE.match(key):
                    continue
                off = ev.offset + 1  # body offsets are tagged; declared = tagged + 1
                if off in seen_here and seen_here[off] != key:
                    continue
                seen_here[off] = key
                out.append(
                    Candidate(
                        offset=off,
                        name=key,
                        rule="json_key_pairing",
                        confidence=Confidence.INFERRED_HIGH,
                        weight=3.0,
                        detail=(
                            'map key "%s" in %s.%s() is immediately followed by '
                            "LoadField this->field_%x (Dart map literals are emitted "
                            "key,value,key,value)" % (key, cls.name, m.clean_name, ev.offset)
                        ),
                        source=_ev_source(m, ev),
                    )
                )
    return out


def rule_from_json_pairing(cls: ClassIR) -> List[Candidate]:
    """Decoder key -> the field the decoded value is stored into."""
    out: List[Candidate] = []
    for m in cls.methods:
        if _norm_method_name(m) not in JSON_IN_NAMES:
            continue
        strings = [e for e in m.events if e.kind == EventKind.STRING
                   and e.value and IDENT_RE.match(e.value)]
        stores = [e for e in m.events if e.kind == EventKind.STORE_FIELD and e.offset is not None]
        if not strings or not stores:
            continue
        balanced = len(strings) == len(stores)
        conf = Confidence.INFERRED_HIGH if balanced else Confidence.INFERRED_MEDIUM
        weight = 2.5 if balanced else 1.2
        pending: Optional[BodyEvent] = None
        for ev in m.events:
            if ev.kind == EventKind.STRING and ev.value and IDENT_RE.match(ev.value):
                pending = ev
            elif ev.kind == EventKind.STORE_FIELD and ev.offset is not None and pending:
                key = pending.value or ""
                pending = None
                out.append(
                    Candidate(
                        offset=ev.offset + 1,
                        name=key,
                        rule="from_json_pairing",
                        confidence=conf,
                        weight=weight,
                        detail=(
                            'decoder key "%s" in %s.%s() precedes StoreField '
                            "this->field_%x%s"
                            % (key, cls.name, m.clean_name, ev.offset,
                               "; key count == store count" if balanced else
                               "; key/store counts differ (%d vs %d)" % (len(strings), len(stores)))
                        ),
                        source=_ev_source(m, ev),
                    )
                )
    return out


def rule_getter_forward(cls: ClassIR) -> List[Candidate]:
    """`T get foo` whose whole body is one field load -> backing field `_foo`."""
    out: List[Candidate] = []
    for m in cls.methods:
        if m.kind != MethodKind.GETTER or m.is_static:
            continue
        loads = [e for e in m.events if e.kind == EventKind.LOAD_FIELD and e.offset is not None]
        calls = [e for e in m.events if e.kind == EventKind.CALL]
        if len(loads) != 1 or calls:
            continue
        base = m.clean_name.lstrip("_")
        if not base:
            continue
        proposed = "_" + base
        if proposed == m.clean_name:
            # `_foo` getter forwarding to a field would collide with itself.
            continue
        ev = loads[0]
        out.append(
            Candidate(
                offset=ev.offset + 1,
                name=proposed,
                rule="getter_forward",
                confidence=Confidence.INFERRED_HIGH,
                weight=3.0,
                detail=(
                    "getter %s.%s has a single-load body (LoadField field_%x, no calls); "
                    "backing field named after the getter"
                    % (cls.name, m.clean_name, ev.offset)
                ),
                source=_ev_source(m, ev),
            )
        )
    return out


def rule_setter_forward(cls: ClassIR) -> List[Candidate]:
    out: List[Candidate] = []
    for m in cls.methods:
        if m.kind != MethodKind.SETTER or m.is_static:
            continue
        stores = [e for e in m.events if e.kind == EventKind.STORE_FIELD and e.offset is not None]
        calls = [e for e in m.events if e.kind == EventKind.CALL]
        if len(stores) != 1 or calls:
            continue
        base = m.clean_name.lstrip("_")
        if not base:
            continue
        proposed = "_" + base
        ev = stores[0]
        out.append(
            Candidate(
                offset=ev.offset + 1,
                name=proposed,
                rule="setter_forward",
                confidence=Confidence.INFERRED_HIGH,
                weight=3.0,
                detail=(
                    "setter %s.%s has a single-store body (StoreField field_%x, no calls)"
                    % (cls.name, m.clean_name, ev.offset)
                ),
                source=_ev_source(m, ev),
            )
        )
    return out


def rule_vm_enum_layout(cls: ClassIR) -> List[Candidate]:
    """`_Enum` has a fixed VM layout: slot 0x8 = index, slot 0x10 = _name."""
    if not cls.is_enum:
        return []
    src = cls.source
    return [
        Candidate(
            offset=0x8,
            name="index",
            rule="vm_enum_layout",
            confidence=Confidence.INFERRED_HIGH,
            weight=3.0,
            detail="dart:core _Enum layout: slot 0x8 is the ordinal (`index`)",
            source=src,
        ),
        Candidate(
            offset=0x10,
            name="_name",
            rule="vm_enum_layout",
            confidence=Confidence.INFERRED_HIGH,
            weight=3.0,
            detail="dart:core _Enum layout: slot 0x10 is the constant name (`_name`)",
            source=src,
        ),
    ]


def rule_tostring_label(cls: ClassIR) -> List[Candidate]:
    """`'foo: '` inside toString(), followed by a load of the value it labels."""
    out: List[Candidate] = []
    for m in cls.methods:
        if _norm_method_name(m) != "tostring":
            continue
        this_reg = receiver_register(m)
        if not this_reg:
            continue
        pending: Optional[str] = None
        pending_ev: Optional[BodyEvent] = None
        for ev in m.events:
            if ev.kind == EventKind.STRING and ev.value:
                mm = TOSTRING_LABEL_RE.match(ev.value)
                pending = mm.group(1) if mm else None
                pending_ev = ev if pending else None
            elif ev.kind == EventKind.LOAD_FIELD and ev.src == this_reg and ev.offset is not None:
                if pending:
                    out.append(
                        Candidate(
                            offset=ev.offset + 1,
                            name=pending,
                            rule="tostring_label",
                            confidence=Confidence.INFERRED_MEDIUM,
                            weight=1.5,
                            detail=(
                                'toString() label "%s" precedes LoadField field_%x'
                                % (pending_ev.value if pending_ev else pending, ev.offset)
                            ),
                            source=_ev_source(m, ev),
                        )
                    )
                    pending = None
    return out


def rule_map_key_nearby(cls: ClassIR) -> List[Candidate]:
    """Weak, aggressive-only: any identifier-shaped literal before a field load."""
    out: List[Candidate] = []
    for m in cls.methods:
        if _norm_method_name(m) in JSON_OUT_NAMES or _norm_method_name(m) == "tostring":
            continue  # already covered by stronger rules
        this_reg = receiver_register(m)
        if not this_reg:
            continue
        pending: Optional[BodyEvent] = None
        for ev in m.events:
            if ev.kind == EventKind.STRING and ev.value and LOWER_CAMEL_RE.match(ev.value):
                pending = ev
            elif ev.kind == EventKind.LOAD_FIELD and ev.src == this_reg and ev.offset is not None:
                if pending:
                    out.append(
                        Candidate(
                            offset=ev.offset + 1,
                            name=pending.value or "",
                            rule="map_key_nearby",
                            confidence=Confidence.INFERRED_LOW,
                            weight=0.6,
                            detail=(
                                'string literal "%s" in %s.%s() precedes LoadField field_%x '
                                "(weak: not a recognised serialisation method)"
                                % (pending.value, cls.name, m.clean_name, ev.offset)
                            ),
                            source=_ev_source(m, ev),
                        )
                    )
                    pending = None
    return out


ALL_RULES = [
    rule_vm_enum_layout,
    rule_json_key_pairing,
    rule_from_json_pairing,
    rule_getter_forward,
    rule_setter_forward,
    rule_tostring_label,
    rule_map_key_nearby,
]

#: rules that only run with --infer-fields aggressive
AGGRESSIVE_ONLY = {"map_key_nearby"}


# ---------------------------------------------------------------------------
# decide + apply
# ---------------------------------------------------------------------------


def _taken_names(cls: ClassIR) -> set:
    taken = set()
    for m in cls.methods:
        taken.add(m.clean_name)
        if m.kind == MethodKind.GETTER or m.kind == MethodKind.SETTER:
            taken.add(m.clean_name)
    for f in cls.fields.values():
        if f.recovered_name:
            taken.add(f.recovered_name)
    for v in cls.enum_values:
        taken.add(v.name)
    return taken


def _decide(cls: ClassIR, offset: int, cands: Sequence[Candidate]) -> FieldDecision:
    """Aggregate candidates for one slot: sum weights per name, pick the winner."""
    per_name: Dict[str, float] = defaultdict(float)
    best_conf: Dict[str, Confidence] = {}
    for c in cands:
        per_name[c.name] += c.weight
        prev = best_conf.get(c.name)
        best_conf[c.name] = c.confidence if prev is None or c.confidence > prev else prev
    ranked = sorted(per_name.items(), key=lambda kv: (-kv[1], kv[0]))
    winner, w_weight = ranked[0]
    runner, r_weight = (ranked[1] if len(ranked) > 1 else (None, 0.0))
    conf = best_conf[winner]
    if runner is not None and r_weight > 0 and (w_weight / r_weight) < 1.5:
        conf = downgrade(conf)
    return FieldDecision(
        class_name=cls.name,
        offset=offset,
        winner=winner,
        winner_weight=w_weight,
        runner_up=runner,
        runner_up_weight=r_weight,
        final_confidence=conf,
        candidates=list(cands),
    )


def _acceptable(dec: FieldDecision, mode: str, floor: Confidence) -> Tuple[bool, str]:
    if mode == "off":
        return False, "inference disabled (--infer-fields off)"
    if dec.final_confidence < floor:
        return False, "confidence %s below floor %s" % (dec.final_confidence.value, floor.value)
    if mode == "safe":
        if dec.final_confidence >= Confidence.INFERRED_HIGH:
            return True, "high-confidence rule"
        if dec.final_confidence == Confidence.INFERRED_MEDIUM and dec.winner_weight >= 2.5:
            return True, "medium confidence corroborated (weight %.1f)" % dec.winner_weight
        return False, (
            "safe mode needs HIGH, or MEDIUM with weight >= 2.5 (had %s / %.1f)"
            % (dec.final_confidence.value, dec.winner_weight)
        )
    # aggressive
    return True, "aggressive mode accepts >= %s" % floor.value


def infer_class(
    cls: ClassIR,
    mode: str = "safe",
    floor: Confidence = Confidence.INFERRED_MEDIUM,
) -> List[FieldDecision]:
    """Run every enabled rule over one class and apply the winners in place."""
    cands_by_offset: Dict[int, List[Candidate]] = defaultdict(list)
    for rule in ALL_RULES:
        if rule.__name__.replace("rule_", "") in AGGRESSIVE_ONLY and mode != "aggressive":
            continue
        for cand in rule(cls):
            cand.name = sanitize_identifier(cand.name)
            cands_by_offset[cand.offset].append(cand)

    taken = _taken_names(cls)
    decisions: List[FieldDecision] = []
    for offset in sorted(cands_by_offset):
        cands = cands_by_offset[offset]
        dec = _decide(cls, offset, cands)
        ok, reason = _acceptable(dec, mode, floor)

        fld = cls.fields.get(offset)
        if ok and fld is not None and fld.recovered_name:
            ok, reason = False, "field already has a RECOVERED name (%s)" % fld.recovered_name

        if ok and dec.winner in taken:
            alt = "_" + dec.winner.lstrip("_")
            if alt not in taken and alt != dec.winner:
                dec.winner = alt
                reason = reason + "; renamed to %s to avoid a collision" % alt
            else:
                ok, reason = False, "name %r collides with an existing member" % dec.winner

        dec.accepted = ok
        dec.reason = reason
        decisions.append(dec)

        if not ok:
            if fld is not None:
                fld.rejected.append("%s: %s" % (dec.winner, reason))
            continue

        if fld is None:
            # The declaration line did not list this slot (Blutter only prints
            # fields whose VM type it could pin down), but a body proved it
            # exists.  Materialise it, and say so.
            fld = cls.ensure_field(offset, "_")
            fld.evidence.append(
                Evidence(
                    rule="offset_from_body",
                    detail=(
                        "slot not present on the class declaration line; existence "
                        "proven by a field access in a method body"
                    ),
                    confidence=Confidence.RECOVERED,
                    weight=0.0,
                    source=dec.candidates[0].source if dec.candidates else None,
                )
            )
        fld.inferred_name = dec.winner
        fld.name_confidence = dec.final_confidence
        for c in cands:
            if c.name == dec.winner:
                fld.evidence.append(c.as_evidence())
        if dec.runner_up:
            fld.rejected.append(
                "%s (weight %.1f, lost to %s at %.1f)"
                % (dec.runner_up, dec.runner_up_weight, dec.winner, dec.winner_weight)
            )
        taken.add(dec.winner)
    return decisions


def infer_program(
    program: ProgramIR,
    mode: str = "safe",
    min_confidence: Confidence = Confidence.INFERRED_MEDIUM,
) -> InferenceResult:
    """Infer instance-field names across the whole program, in place."""
    if mode not in ("off", "safe", "aggressive"):
        raise ValueError("mode must be off|safe|aggressive, got %r" % mode)
    program.link()
    result = InferenceResult(mode=mode, min_confidence=min_confidence)
    for _lib, cls in program.all_classes():
        result.fields_seen += len(cls.instance_fields())
        for dec in infer_class(cls, mode=mode, floor=min_confidence):
            result.decisions.append(dec)
            if dec.accepted:
                result.applied += 1
                result.by_confidence[dec.final_confidence] += 1
                for c in dec.candidates:
                    if c.name == dec.winner:
                        result.by_rule[c.rule] += 1
            else:
                result.rejected += 1
    return result


__all__ = [
    "Candidate", "FieldDecision", "InferenceResult",
    "infer_program", "infer_class", "receiver_register",
    "ALL_RULES", "AGGRESSIVE_ONLY",
]
