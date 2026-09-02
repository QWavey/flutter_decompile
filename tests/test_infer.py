"""infer.py -- instance-field name inference.

Instance field names are destroyed by AOT compilation. Everything this module
produces is therefore a reconstruction, and the tool's central promise is that
a reconstruction is never presented as a recovery. The load-bearing test in
this file is `test_no_inference_is_ever_emitted_bare`: if it fails, the tool is
lying to its users, whatever else passes.
"""

from __future__ import annotations

import pytest

from flutter_decompile import infer
from flutter_decompile import ir

from conftest import (
    assert_no_bare_inferences,
    call_log_class,
    ev,
    make_program,
    security_level_enum,
)


# --------------------------------------------------------------------------- #
# THE invariant
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mode,floor", [
    ("safe", ir.Confidence.INFERRED_MEDIUM),
    ("aggressive", ir.Confidence.INFERRED_MEDIUM),
    ("aggressive", ir.Confidence.INFERRED_LOW),
    ("off", ir.Confidence.INFERRED_MEDIUM),
])
def test_no_inference_is_ever_emitted_bare(mode, floor):
    """Every inferred name carries evidence and an inferred confidence.

    This is the whole contract. A name with no evidence trail is
    indistinguishable, to a reader, from a recovered fact -- which is exactly
    the impression this tool exists not to give.
    """
    prog = make_program(call_log_class(), security_level_enum(),
                        getter_backed_class(), tostring_class(),
                        decoder_class())
    infer.infer_program(prog, mode=mode, min_confidence=floor)
    assert_no_bare_inferences(prog)


def test_the_invariant_check_can_actually_fail():
    """A guard that would pass on an empty program guards nothing."""
    prog = make_program(call_log_class())
    infer.infer_program(prog)
    assert assert_no_bare_inferences(prog) > 0

    cls = prog.libraries[0].classes[0]
    victim = cls.fields[0x8]
    victim.evidence = []
    with pytest.raises(AssertionError, match="no evidence"):
        assert_no_bare_inferences(prog)


def test_a_rejected_candidate_leaves_the_slot_unnamed_and_says_why():
    """No name is better than a name the evidence does not support."""
    cls = tostring_class()          # one MEDIUM candidate, weight 1.5
    decisions = infer.infer_class(cls, mode="safe")
    assert decisions and not any(d.accepted for d in decisions)
    for f in cls.instance_fields():
        assert f.inferred_name is None
        assert f.name == f.placeholder_name
        assert f.confidence is ir.Confidence.UNKNOWN
    assert any("safe mode needs HIGH" in r for r in cls.fields[0x8].rejected)


# --------------------------------------------------------------------------- #
# fixtures local to this module
# --------------------------------------------------------------------------- #

def getter_backed_class() -> ir.ClassIR:
    """`String get peer => <one field load>` -- the getter_forward shape."""
    cls = ir.ClassIR(name="Contact")
    cls.ensure_field(0x8, "_OneByteString")
    g = ir.MethodIR(name="peer", kind=ir.MethodKind.GETTER,
                    return_vm_type="_OneByteString", address=0x1000)
    g.events = [ev(ir.EventKind.LOAD_FIELD, dst="r0", src="r1", offset=0x7,
                   address=0x1004)]
    s = ir.MethodIR(name="peer", kind=ir.MethodKind.SETTER, address=0x1100)
    s.events = [ev(ir.EventKind.STORE_FIELD, dst="r1", src="r2", offset=0x7,
                   address=0x1104)]
    cls.methods += [g, s]
    return cls


def tostring_class() -> ir.ClassIR:
    """`'peer: '` then a load -- MEDIUM evidence, weight 1.5."""
    cls = ir.ClassIR(name="Labelled")
    cls.ensure_field(0x8, "_OneByteString")
    m = ir.MethodIR(name="toString", address=0x2000)
    m.events = [
        ev(ir.EventKind.STRING, value="peer: ", address=0x2004),
        ev(ir.EventKind.LOAD_FIELD, dst="r0", src="r3", offset=0x7, address=0x2008),
    ]
    cls.methods.append(m)
    return cls


def decoder_class() -> ir.ClassIR:
    """`fromJson` with as many keys as stores -- the balanced HIGH case."""
    cls = ir.ClassIR(name="Decoded")
    cls.ensure_field(0x8, "_OneByteString")
    cls.ensure_field(0xC, "_Mint")
    m = ir.MethodIR(name="fromJson", kind=ir.MethodKind.FACTORY, is_static=True,
                    address=0x3000)
    m.events = [
        ev(ir.EventKind.STRING, value="peer", address=0x3004),
        ev(ir.EventKind.STORE_FIELD, dst="r1", src="r2", offset=0x7, address=0x3008),
        ev(ir.EventKind.STRING, value="dir", address=0x300C),
        ev(ir.EventKind.STORE_FIELD, dst="r1", src="r2", offset=0xB, address=0x3010),
    ]
    cls.methods.append(m)
    return cls


# --------------------------------------------------------------------------- #
# json_key_pairing
# --------------------------------------------------------------------------- #

def test_json_key_pairing_lifts_the_body_offset_to_the_declared_slot():
    """The body loads field_7; the declaration calls that slot field_8. Getting
    this wrong shifts every name by one field."""
    cls = call_log_class()
    infer.infer_class(cls)
    assert cls.fields[0x8].name == "peer"
    assert cls.fields[0xC].name == "dir"
    assert cls.fields[0x10].name == "outcome"


def test_json_key_pairing_is_HIGH_and_names_the_compiler_invariant():
    cls = call_log_class()
    infer.infer_class(cls)
    f = cls.fields[0x8]
    assert f.confidence is ir.Confidence.INFERRED_HIGH
    ev_ = f.evidence[0]
    assert ev_.rule == "json_key_pairing"
    assert "key,value" in ev_.detail, "the evidence must state why pairing is valid"
    assert 'map key "peer"' in ev_.detail
    assert ev_.source.address == 0x6CF254, "evidence points at the exact line"


def test_json_key_pairing_ignores_keys_that_are_not_identifiers():
    cls = call_log_class()
    cls.methods[0].events[0] = ev(ir.EventKind.STRING, value="not an ident",
                                  address=0x6CF250)
    infer.infer_class(cls)
    assert cls.fields[0x8].inferred_name is None
    assert cls.fields[0xC].name == "dir", "the rest of the pairing survives"


def test_json_key_pairing_only_follows_the_receiver_register():
    """Loads off a freshly allocated map array are not `this`."""
    cls = call_log_class()
    cls.methods[0].events.insert(1, ev(ir.EventKind.LOAD_FIELD, dst="r0", src="r9",
                                       offset=0x63, address=0x6CF252))
    infer.infer_class(cls)
    assert 0x64 not in cls.fields


def test_a_slot_only_proven_by_a_body_is_materialised_and_labelled():
    """Blutter prints a field slot only when it pinned down the VM type, so a
    body can prove a slot the declaration never listed."""
    cls = call_log_class()
    del cls.fields[0x10]
    infer.infer_class(cls)
    f = cls.fields[0x10]
    assert f.name == "outcome"
    rules = [e.rule for e in f.evidence]
    assert "offset_from_body" in rules
    assert f.vm_type == "_", "no type may be invented for a slot never declared"


# --------------------------------------------------------------------------- #
# from_json_pairing
# --------------------------------------------------------------------------- #

def test_balanced_decoder_keys_are_HIGH_and_say_the_counts_matched():
    cls = decoder_class()
    infer.infer_class(cls)
    assert cls.fields[0x8].name == "peer"
    assert cls.fields[0x8].confidence is ir.Confidence.INFERRED_HIGH
    assert "key count == store count" in cls.fields[0x8].evidence[0].detail


def test_unbalanced_decoder_keys_are_downgraded_and_rejected_in_safe_mode():
    cls = decoder_class()
    cls.methods[0].events.append(
        ev(ir.EventKind.STRING, value="stray", address=0x3014))
    decisions = infer.infer_class(cls, mode="safe")
    assert all(not d.accepted for d in decisions)
    assert cls.fields[0x8].inferred_name is None
    cands = [c for d in decisions for c in d.candidates]
    assert all(c.confidence is ir.Confidence.INFERRED_MEDIUM for c in cands)
    assert any("counts differ" in c.detail for c in cands)


# --------------------------------------------------------------------------- #
# getter / setter forwarding
# --------------------------------------------------------------------------- #

def test_getter_and_setter_forwarding_agree_on_the_backing_field():
    cls = getter_backed_class()
    infer.infer_class(cls)
    f = cls.fields[0x8]
    assert f.name == "_peer", "a public getter implies a private backing field"
    assert {e.rule for e in f.evidence} == {"getter_forward", "setter_forward"}
    assert f.confidence is ir.Confidence.INFERRED_HIGH


def test_a_getter_that_calls_something_is_not_a_forwarder():
    cls = getter_backed_class()
    cls.methods[0].events.append(ev(ir.EventKind.CALL, target="X::y", address=0x1008))
    cls.methods.pop(1)                     # drop the corroborating setter
    infer.infer_class(cls)
    assert cls.fields[0x8].inferred_name is None


def test_a_getter_with_two_loads_is_not_a_forwarder():
    cls = getter_backed_class()
    cls.methods.pop(1)
    cls.methods[0].events.append(
        ev(ir.EventKind.LOAD_FIELD, dst="r0", src="r1", offset=0xB, address=0x1008))
    infer.infer_class(cls)
    assert all(f.inferred_name is None for f in cls.instance_fields())


def test_a_private_getter_does_not_propose_its_own_name():
    cls = getter_backed_class()
    cls.methods = [m for m in cls.methods if m.kind is ir.MethodKind.GETTER]
    cls.methods[0].name = "_peer"
    infer.infer_class(cls)
    assert cls.fields[0x8].inferred_name is None


# --------------------------------------------------------------------------- #
# vm_enum_layout
# --------------------------------------------------------------------------- #

def test_enum_slots_get_the_fixed_vm_layout_names():
    en = security_level_enum()
    infer.infer_class(en)
    assert en.fields[0x8].name == "index"
    assert en.fields[0x10].name == "_name"
    assert en.fields[0x14].inferred_name is None, \
        "a payload slot is not part of the _Enum layout and stays unnamed"
    assert en.fields[0x8].evidence[0].rule == "vm_enum_layout"


def test_the_enum_layout_rule_does_not_fire_on_a_plain_class():
    cls = ir.ClassIR(name="Plain")
    cls.ensure_field(0x8, "_Mint")
    assert infer.rule_vm_enum_layout(cls) == []


# --------------------------------------------------------------------------- #
# weak rules and modes
# --------------------------------------------------------------------------- #

def test_map_key_nearby_never_runs_in_safe_mode():
    cls = weak_class()
    infer.infer_class(cls, mode="safe")
    assert cls.fields[0x8].inferred_name is None


def test_map_key_nearby_is_still_below_the_default_floor_in_aggressive_mode():
    cls = weak_class()
    infer.infer_class(cls, mode="aggressive",
                      floor=ir.Confidence.INFERRED_MEDIUM)
    assert cls.fields[0x8].inferred_name is None


def test_map_key_nearby_applies_only_when_the_floor_is_lowered_to_LOW():
    cls = weak_class()
    infer.infer_class(cls, mode="aggressive", floor=ir.Confidence.INFERRED_LOW)
    f = cls.fields[0x8]
    assert f.name == "peer"
    assert f.confidence is ir.Confidence.INFERRED_LOW
    assert "weak" in f.evidence[0].detail, \
        "a LOW guess must say in the output that it is weak"


def weak_class() -> ir.ClassIR:
    cls = ir.ClassIR(name="Weak")
    cls.ensure_field(0x8, "_OneByteString")
    m = ir.MethodIR(name="doSomething", address=0x4000)
    m.events = [
        ev(ir.EventKind.STRING, value="peer", address=0x4004),
        ev(ir.EventKind.LOAD_FIELD, dst="r0", src="r3", offset=0x7, address=0x4008),
    ]
    cls.methods.append(m)
    return cls


def test_mode_off_applies_nothing_and_says_so():
    cls = call_log_class()
    decisions = infer.infer_class(cls, mode="off")
    assert decisions, "rules still run, so the report can show what was declined"
    assert all(not d.accepted for d in decisions)
    assert all("inference disabled" in d.reason for d in decisions)
    assert all(f.inferred_name is None for f in cls.instance_fields())


def test_an_unknown_mode_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="off|safe|aggressive"):
        infer.infer_program(make_program(call_log_class()), mode="sloppy")


# --------------------------------------------------------------------------- #
# arbitration
# --------------------------------------------------------------------------- #

def test_a_close_second_candidate_downgrades_the_winner():
    """Two rules disagreeing is itself evidence that the answer is shaky."""
    cands = [
        infer.Candidate(offset=0x8, name="peer", rule="a",
                        confidence=ir.Confidence.INFERRED_HIGH, weight=3.0,
                        detail="x"),
        infer.Candidate(offset=0x8, name="other", rule="b",
                        confidence=ir.Confidence.INFERRED_HIGH, weight=2.5,
                        detail="y"),
    ]
    dec = infer._decide(ir.ClassIR(name="A"), 0x8, cands)
    assert dec.winner == "peer" and dec.runner_up == "other"
    assert dec.final_confidence is ir.Confidence.INFERRED_MEDIUM


def test_a_clear_winner_keeps_its_confidence():
    cands = [
        infer.Candidate(offset=0x8, name="peer", rule="a",
                        confidence=ir.Confidence.INFERRED_HIGH, weight=3.0,
                        detail="x"),
        infer.Candidate(offset=0x8, name="other", rule="b",
                        confidence=ir.Confidence.INFERRED_LOW, weight=0.6,
                        detail="y"),
    ]
    dec = infer._decide(ir.ClassIR(name="A"), 0x8, cands)
    assert dec.final_confidence is ir.Confidence.INFERRED_HIGH


def test_the_losing_candidate_is_recorded_on_the_field():
    cls = call_log_class()
    cls.methods[0].events.insert(
        2, ev(ir.EventKind.STRING, value="alias", address=0x6CF255))
    cls.methods.append(alias_tostring())
    infer.infer_class(cls)
    f = cls.fields[0x8]
    assert f.rejected, "a runner-up must be visible, not silently dropped"


def alias_tostring() -> ir.MethodIR:
    m = ir.MethodIR(name="toString", address=0x5000)
    m.events = [
        ev(ir.EventKind.STRING, value="alias: ", address=0x5004),
        ev(ir.EventKind.LOAD_FIELD, dst="r0", src="r3", offset=0x7, address=0x5008),
    ]
    return m


def test_a_recovered_name_is_never_overwritten_by_an_inference():
    cls = call_log_class()
    cls.fields[0x8].recovered_name = "_veilLabel"
    infer.infer_class(cls)
    assert cls.fields[0x8].name == "_veilLabel"
    assert cls.fields[0x8].confidence is ir.Confidence.RECOVERED
    assert any("RECOVERED name" in r for r in cls.fields[0x8].rejected)


def test_a_name_that_collides_with_a_method_is_privatised():
    cls = call_log_class()
    cls.methods.append(ir.MethodIR(name="peer"))
    decisions = infer.infer_class(cls)
    dec = [d for d in decisions if d.offset == 0x8][0]
    assert dec.winner == "_peer"
    assert "collision" in dec.reason


# The most important guard in this suite. A winner that collided with an
# existing member was renamed to `_<name>`, after which no candidate matched
# the new name and the field shipped with an INFERRED name, INFERRED_HIGH
# confidence and NO evidence - a guess presented as a fact, which is the one
# thing this tool exists not to do.
def test_a_privatised_name_still_carries_its_evidence():
    cls = call_log_class()
    cls.methods.append(ir.MethodIR(name="peer"))
    infer.infer_class(cls)
    f = cls.fields[0x8]
    assert f.inferred_name == "_peer"
    assert f.evidence, "renaming to dodge a collision must not drop the evidence"


def test_a_collision_with_no_free_alternative_is_declined():
    cls = call_log_class()
    cls.methods.append(ir.MethodIR(name="peer"))
    cls.methods.append(ir.MethodIR(name="_peer"))
    decisions = infer.infer_class(cls)
    dec = [d for d in decisions if d.offset == 0x8][0]
    assert not dec.accepted and "collides" in dec.reason
    assert cls.fields[0x8].inferred_name is None


# --------------------------------------------------------------------------- #
# receiver_register
# --------------------------------------------------------------------------- #

def test_receiver_register_prefers_the_most_loaded_base_register():
    """SetupParameters' register goes stale: in the reference body `this` is
    set up in r0 and re-materialised into r3 before every load."""
    m = ir.MethodIR(name="toJson")
    m.events = [
        ev(ir.EventKind.OTHER,
           raw="SetupParameters(CallLogEntry this /* r1 => r0, fp-0x8 */)"),
        ev(ir.EventKind.LOAD_FIELD, dst="r0", src="r3", offset=0x7),
        ev(ir.EventKind.LOAD_FIELD, dst="r0", src="r3", offset=0xB),
        ev(ir.EventKind.LOAD_FIELD, dst="r0", src="r9", offset=0x3),
    ]
    assert infer.receiver_register(m) == "r3"


def test_receiver_register_falls_back_to_setup_parameters():
    m = ir.MethodIR(name="toJson")
    m.events = [
        ev(ir.EventKind.OTHER,
           raw="SetupParameters(CallLogEntry this /* r1 => r0, fp-0x8 */)"),
        ev(ir.EventKind.STORE_FIELD, dst="r2", src="r16", offset=0xF),
    ]
    assert infer.receiver_register(m) == "r0"


def test_receiver_register_is_None_when_the_body_shows_nothing():
    assert infer.receiver_register(ir.MethodIR(name="x")) is None


# --------------------------------------------------------------------------- #
# program-level bookkeeping
# --------------------------------------------------------------------------- #

def test_infer_program_reports_what_it_did_per_rule_and_confidence():
    prog = make_program(call_log_class(), security_level_enum())
    result = infer.infer_program(prog)
    assert result.mode == "safe"
    assert result.applied == 5          # 3 json keys + enum index/_name
    assert result.by_rule["json_key_pairing"] == 3
    assert result.by_rule["vm_enum_layout"] == 2
    assert result.by_confidence[ir.Confidence.INFERRED_HIGH] == 5
    assert result.fields_seen >= 5


def test_inference_result_serialises_with_every_decision_and_its_reason():
    import json
    prog = make_program(call_log_class(), tostring_class())
    blob = json.loads(json.dumps(infer.infer_program(prog).to_json()))
    assert blob["mode"] == "safe"
    assert blob["min_confidence"] == "INFERRED_MEDIUM"
    assert blob["decisions"], "the report must show declined guesses too"
    for d in blob["decisions"]:
        assert d["reason"], "every decision states why it went the way it did"
        assert d["candidates"]


def test_infer_program_is_idempotent():
    prog = make_program(call_log_class())
    infer.infer_program(prog)
    first = {f.offset: (f.name, f.confidence) for _l, _c, f in prog.all_fields()}
    infer.infer_program(prog)
    second = {f.offset: (f.name, f.confidence) for _l, _c, f in prog.all_fields()}
    assert first == second
    assert_no_bare_inferences(prog)


def test_aggressive_only_rules_are_declared_and_are_the_weak_ones():
    assert infer.AGGRESSIVE_ONLY == {"map_key_nearby"}
    names = {r.__name__.replace("rule_", "") for r in infer.ALL_RULES}
    assert infer.AGGRESSIVE_ONLY <= names
