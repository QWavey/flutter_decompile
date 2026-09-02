"""ir.py -- the program model and its pure helpers.

ir.py is the contract between the two halves of the tool, so what is pinned
here is mostly vocabulary: what a Confidence means, what a lowered VM type
maps back to, and which offset space an offset is in.
"""

from __future__ import annotations

import pytest

from flutter_decompile import ir


# --------------------------------------------------------------------------- #
# Confidence
# --------------------------------------------------------------------------- #

def test_confidence_orders_unknown_below_inferred_below_recovered():
    assert ir.Confidence.UNKNOWN < ir.Confidence.INFERRED_LOW
    assert ir.Confidence.INFERRED_LOW < ir.Confidence.INFERRED_MEDIUM
    assert ir.Confidence.INFERRED_MEDIUM < ir.Confidence.INFERRED_HIGH
    assert ir.Confidence.INFERRED_HIGH < ir.Confidence.RECOVERED
    assert ir.Confidence.RECOVERED >= ir.Confidence.RECOVERED


def test_only_the_inferred_levels_are_inferred():
    """RECOVERED is not a guess and UNKNOWN is not a name. Neither is INFERRED."""
    inferred = [c for c in ir.Confidence if c.is_inferred]
    assert set(inferred) == {
        ir.Confidence.INFERRED_HIGH,
        ir.Confidence.INFERRED_MEDIUM,
        ir.Confidence.INFERRED_LOW,
    }


@pytest.mark.parametrize("text,expected", [
    ("high", ir.Confidence.INFERRED_HIGH),
    ("MED", ir.Confidence.INFERRED_MEDIUM),
    ("  low  ", ir.Confidence.INFERRED_LOW),
    ("INFERRED-HIGH", ir.Confidence.INFERRED_HIGH),
    ("RECOVERED", ir.Confidence.RECOVERED),
])
def test_confidence_parse_accepts_the_documented_aliases(text, expected):
    assert ir.Confidence.parse(text) is expected


def test_confidence_parse_refuses_to_invent_a_level():
    with pytest.raises(ValueError) as exc:
        ir.Confidence.parse("probably")
    assert "probably" in str(exc.value)


def test_downgrade_saturates_at_both_ends():
    assert ir.downgrade(ir.Confidence.RECOVERED) is ir.Confidence.INFERRED_HIGH
    assert ir.downgrade(ir.Confidence.UNKNOWN) is ir.Confidence.UNKNOWN
    assert ir.downgrade(ir.Confidence.INFERRED_HIGH, 2) is ir.Confidence.INFERRED_LOW
    assert ir.downgrade(ir.Confidence.INFERRED_LOW, 9) is ir.Confidence.UNKNOWN


# --------------------------------------------------------------------------- #
# offsets -- the tag-bit convention
# --------------------------------------------------------------------------- #

def test_pointer_tag_adjust_is_one_bit():
    """kHeapObjectTag == 1. If this ever changes, every field name shifts."""
    assert ir.POINTER_TAG_ADJUST == 1


def test_canonical_offset_lifts_body_offsets_and_leaves_declared_ones():
    # Verified on CallLogEntry: body loads field_7 -> declaration says field_8.
    assert ir.canonical_offset(0x7, ir.OffsetSpace.TAGGED) == 0x8
    assert ir.canonical_offset(0x8, ir.OffsetSpace.DECLARED) == 0x8
    assert ir.canonical_offset(0x1B, ir.OffsetSpace.TAGGED) == 0x1C


# --------------------------------------------------------------------------- #
# type mapping
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,dart", [
    ("_OneByteString", "String"),
    ("_TwoByteString", "String"),
    ("_Mint", "int"),
    ("_Smi", "int"),
    ("_Double", "double"),
    ("_GrowableList", "List"),
    ("_TypedList", "List<int>"),
    ("Map<String, dynamic>", "Map<String, dynamic>"),
    ("List<_Mint>", "List<int>"),
    ("_GrowableList<_OneByteString>", "List<String>"),
    ("String?", "String?"),
    ("Widget", "Widget"),
])
def test_vm_to_dart_maps_lowered_types(raw, dart):
    assert ir.vm_to_dart(raw)[0] == dart


@pytest.mark.parametrize("raw", ["", "_", "/* No info */"])
def test_absent_type_becomes_dynamic_and_says_so(raw):
    dart, note = ir.vm_to_dart(raw)
    assert dart == "dynamic"
    assert "no type info" in note


def test_lowering_is_always_annotated():
    """A lowered type is a change of fact, so it may never be silent."""
    _dart, note = ir.vm_to_dart("_OneByteString")
    assert note and "_OneByteString" in note


def test_unlowered_type_carries_no_note():
    assert ir.vm_to_dart("Map<String, dynamic>")[1] is None


def test_flattened_mixin_keeps_the_whole_chain_in_the_note():
    dart, note = ir.vm_to_dart("_Foo&Bar&Baz")
    assert dart == "_Foo"
    assert note == "flattened mixin application _Foo&Bar&Baz"


def test_split_generic_is_nesting_aware():
    assert ir.split_generic("Map<String, List<int>>") == ("Map", ["String", "List<int>"])
    assert ir.split_generic("int") == ("int", [])


def test_demangle_drops_the_vm_hash_only():
    assert ir.demangle("_toJson@940078579") == "_toJson"
    assert ir.demangle("toJson") == "toJson"
    assert ir.demangle("") == ""


# --------------------------------------------------------------------------- #
# identifiers and string literals
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,out", [
    ("class", "class_"),        # a Dart keyword may not be an identifier
    ("9lives", "n9lives"),
    ("a-b", "a_b"),
    ("", "_unnamed"),
    ("_toJson@123", "_toJson"),
])
def test_sanitize_identifier(raw, out):
    assert ir.sanitize_identifier(raw) == out


def test_sanitize_identifier_can_strip_privacy():
    assert ir.sanitize_identifier("_peer", private_ok=False) == "peer"
    assert ir.sanitize_identifier("_peer") == "_peer"


@pytest.mark.parametrize("value,literal", [
    ("peer", "'peer'"),
    ("a'b", "'a\\'b'"),
    ("back\\slash", "'back\\\\slash'"),
    ("$interp", "'\\$interp'"),          # Dart interpolation must be defused
    ("new\nline", "'new\\nline'"),
    ("tab\there", "'tab\\there'"),
    ("\x01", "'\\u{1}'"),                # unprintable -> explicit code point
    ("\x7f", "'\\u{7f}'"),
    ("unicode é", "'unicode é'"),        # printable non-ASCII stays verbatim
])
def test_escape_dart_string_emits_the_recovered_bytes_verbatim(value, literal):
    """String constants are RECOVERED facts: escaped only so Dart re-parses
    them to exactly the same bytes, never rewritten."""
    assert ir.escape_dart_string(value) == literal


def test_escape_dart_string_always_produces_a_closed_literal():
    for value in ["", "'", "\\", "$", "'''", "\r\n"]:
        lit = ir.escape_dart_string(value)
        assert lit[0] == "'" and lit[-1] == "'" and len(lit) >= 2
        # No unescaped quote may survive inside the literal.
        inner = lit[1:-1]
        assert "'" not in inner.replace("\\'", "")


# --------------------------------------------------------------------------- #
# FieldIR
# --------------------------------------------------------------------------- #

def test_unnamed_field_is_an_offset_and_is_UNKNOWN():
    f = ir.FieldIR(offset=0x18, vm_type="_Mint")
    assert f.name == "field_18"
    assert f.placeholder_name == "field_18"
    assert not f.has_real_name
    assert f.confidence is ir.Confidence.UNKNOWN


def test_recovered_name_outranks_an_inferred_one():
    f = ir.FieldIR(offset=0x8, recovered_name="_veilLabel", inferred_name="label",
                   name_confidence=ir.Confidence.INFERRED_HIGH)
    assert f.name == "_veilLabel"
    assert f.confidence is ir.Confidence.RECOVERED


def test_inferred_field_reports_its_own_confidence():
    f = ir.FieldIR(offset=0x8, inferred_name="peer",
                   name_confidence=ir.Confidence.INFERRED_MEDIUM)
    assert f.name == "peer"
    assert f.confidence is ir.Confidence.INFERRED_MEDIUM


def test_field_json_never_hides_the_placeholder():
    """Even a named field keeps its slot visible, so a reader can check it."""
    f = ir.FieldIR(offset=0x8, inferred_name="peer",
                   name_confidence=ir.Confidence.INFERRED_HIGH)
    d = f.to_json()
    assert d["placeholder"] == "field_8"
    assert d["offset"] == "0x8"
    assert d["confidence"] == "INFERRED_HIGH"


# --------------------------------------------------------------------------- #
# ClassIR / LibraryIR / ProgramIR
# --------------------------------------------------------------------------- #

def test_ensure_field_is_idempotent_and_only_upgrades_unknown_types():
    cls = ir.ClassIR(name="A")
    a = cls.ensure_field(0x8, "_")
    b = cls.ensure_field(0x8, "_Mint")
    assert a is b and b.vm_type == "_Mint"
    c = cls.ensure_field(0x8, "_OneByteString")
    assert c.vm_type == "_Mint", "a known type must not be overwritten"


def test_enum_ordinal_gaps_are_reported_not_filled():
    cls = ir.ClassIR(name="E", is_enum=True)
    cls.enum_values = [ir.EnumValueIR(name="a", ordinal=0),
                       ir.EnumValueIR(name="c", ordinal=3)]
    assert cls.enum_ordinal_gaps() == [1, 2]


def test_no_enum_values_means_no_gaps_rather_than_a_guess():
    assert ir.ClassIR(name="E", is_enum=True).enum_ordinal_gaps() == []


def test_instance_and_static_fields_split_by_offset_order():
    cls = ir.ClassIR(name="A")
    cls.ensure_field(0x10, "_Mint")
    cls.ensure_field(0x8, "_Mint")
    stat = cls.ensure_field(0x20, "_Mint")
    stat.is_static = True
    assert [f.offset for f in cls.instance_fields()] == [0x8, 0x10]
    assert [f.offset for f in cls.static_fields()] == [0x20]


@pytest.mark.parametrize("url,package,rel", [
    ("package:chat/core/crypto/kdf.dart", "chat", "core/crypto/kdf.dart"),
    ("package:flutter/src/widgets/framework.dart", "flutter",
     "src/widgets/framework.dart"),
])
def test_library_url_splits_into_package_and_path(url, package, rel):
    lib = ir.LibraryIR(url=url)
    assert lib.package == package
    assert lib.rel_path == rel


def test_dart_sdk_library_still_yields_a_dart_path():
    lib = ir.LibraryIR(url="dart:core")
    assert lib.package == "core"
    assert lib.rel_path.endswith(".dart")


def test_link_backfills_owners_and_is_idempotent(program):
    program.link()
    program.link()
    for _lib, cls, f in program.all_fields():
        assert f.owner == cls.name
    for _lib, cls, m in program.all_methods():
        assert m.owner == cls.name
        assert m.qualified().startswith(cls.name + "::")


def test_source_ref_label_degrades_loudly():
    assert ir.SourceRef().label() == "<no source ref>"
    assert ir.SourceRef(asm_file="a.dart", line=3, address=0x10).label() == \
        "a.dart:3 @0x10"


def test_evidence_renders_rule_detail_and_place():
    e = ir.Evidence(rule="json_key_pairing", detail="map key \"peer\"",
                    source=ir.SourceRef(asm_file="a.dart", line=3))
    rendered = e.render()
    assert rendered.startswith("json_key_pairing: ")
    assert "a.dart:3" in rendered
    assert e.to_json()["confidence"] == "INFERRED_MEDIUM"
