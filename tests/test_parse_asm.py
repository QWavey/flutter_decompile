"""parse_asm.py -- Blutter listing -> facts.

The parser is the tool's only contact with Blutter's private text format, so
these tests are written as format pins: each one asserts something that would
break if Blutter changed a line shape, which is exactly when a human needs to
look. Nothing here parses a real APK; the snippets in conftest.py carry the
line shapes.
"""

from __future__ import annotations

import os

import pytest

from flutter_decompile import parse_asm as pa

from conftest import ASM_CALL_LOG, ASM_ODDBALLS


def parse(asm_file, name, text):
    return pa.parse_file(asm_file(name, text), body_events=True)


# --------------------------------------------------------------------------- #
# library header + class structure
# --------------------------------------------------------------------------- #

def test_library_url_comes_from_the_header(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    assert lib.url == "package:chat/features/call/data/call_log_store.dart"
    assert lib.package == "chat"
    assert lib.dart_path == "lib/features/call/data/call_log_store.dart"


def test_missing_header_is_recorded_not_invented(asm_file):
    lib = parse(asm_file, "headerless.dart", "class Foo extends Object {\n}\n")
    assert lib.url.startswith("unknown:")
    assert any("missing '// lib:' header" in u.text for u in lib.unparsed), \
        "a fabricated url must leave a trace in the coverage report"


def test_class_facts_are_recovered_verbatim(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    cls = lib.classes[0]
    assert cls.name == "CallLogEntry"
    assert cls.kind == "class"
    assert cls.class_id == 2247
    assert cls.size == 0x24
    assert cls.field_offset == 0x8
    assert cls.superclass == "Object"
    assert "const constructor" in cls.attrs


def test_enum_is_parsed_as_its_own_kind(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    en = [c for c in lib.classes if c.name == "SecurityLevel"][0]
    assert en.kind == "enum"
    assert en.superclass == "_Enum"


def test_two_line_class_header_with_implements(asm_file):
    lib = parse(asm_file, "oddballs.dart", ASM_ODDBALLS)
    cls = lib.classes[0]
    assert cls.name == "_SecureKvStore"
    assert cls.interfaces == ["KeyValueStore", "Disposable"]
    assert lib.unparsed == [], "a wrapped declaration is a known shape"


def test_library_scope_pseudo_class_is_flagged(asm_file):
    lib = parse(asm_file, "top.dart",
                "// lib: , url: package:chat/main.dart\n"
                "class :: {\n"
                "  void main(/* No info */) {\n"
                "    // ** addr: 0x10, size: 0x4\n"
                "  }\n"
                "}\n")
    assert lib.classes[0].is_library_scope
    assert lib.classes[0].name == "::"


def test_mixin_chain_is_split_but_only_from_the_flattened_name(asm_file):
    lib = parse(asm_file, "mix.dart",
                "// lib: , url: package:chat/m.dart\n"
                "class Foo extends _Mix369&Animation&EagerListenerMixin {\n"
                "}\n")
    cls = lib.classes[0]
    assert cls.mixin_chain == ["_Mix369", "Animation", "EagerListenerMixin"]
    assert pa.ClassIR(name="X", superclass="Object").mixin_chain == []


# --------------------------------------------------------------------------- #
# fields -- RECOVERED vs DESTROYED
# --------------------------------------------------------------------------- #

def test_field_n_is_DESTROYED_and_keeps_no_fake_name(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    cls = lib.classes[0]
    f = [x for x in cls.fields if x.offset == 0x8][0]
    assert f.name is None, "field_8 has no name; storing one would be a lie"
    assert f.name_confidence == pa.DESTROYED
    assert f.vm_type == "_OneByteString"
    assert f.placeholder_name == "field_8"


def test_offset_is_read_from_the_placeholder_when_no_comment_gives_it(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    offsets = sorted(f.offset for f in lib.classes[0].fields)
    assert offsets == [0x8, 0xC, 0xE80]


def test_static_field_name_is_RECOVERED_with_its_slot(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    f = [x for x in lib.classes[0].fields if x.name == "_veilLabel"][0]
    assert f.name_confidence == pa.RECOVERED
    assert f.is_static and f.is_late and f.is_final
    assert f.offset == 0xE80
    assert f.vm_type == "List<int>"


# --------------------------------------------------------------------------- #
# methods
# --------------------------------------------------------------------------- #

def test_method_signature_facts(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    m = [x for x in lib.classes[0].methods if x.name == "toJson"][0]
    assert m.kind == "method"
    assert m.return_type == "Map<String, dynamic>"
    assert m.return_type_confidence == pa.RECOVERED
    assert m.param_types == ["CallLogEntry"]
    assert m.addr == 0x6CF240 and m.size == 0xC8
    assert m.has_body


def test_underscore_return_type_is_UNKNOWN_not_dynamic(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    m = [x for x in lib.classes[0].methods if x.name == "deriveVeilKey"][0]
    assert m.return_type is None
    assert m.return_type_confidence == pa.UNKNOWN
    assert m.is_static and m.async_modifier == "async"


def test_no_info_parameter_list_is_None_not_empty(asm_file):
    """`[]` would claim the method takes nothing. It claims nothing at all."""
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    m = [x for x in lib.classes[0].methods if x.name == "deriveVeilKey"][0]
    assert m.param_types is None


def test_parameter_names_are_always_DESTROYED(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    for m in lib.classes[0].methods:
        assert m.param_names_confidence == pa.DESTROYED
        assert m.to_dict()["param_names"] == {"value": None, "confidence": "DESTROYED"}


def test_getter_and_setter_kinds(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    kinds = {m.name: m.kind for m in lib.classes[0].methods}
    assert kinds["_allowPaste"] == "getter"
    assert kinds["state="] == "setter"


@pytest.mark.parametrize("name,kind", [
    ("BaselineOffset.+", "operator"),
    ("<anonymous closure>", "closure"),
    ("dyn:set:enabled", "dyn_forwarder"),
    ("==", "operator"),
    ("_SecureKvStore.", "constructor"),
    ("_SecureKvStore.fromJson", "factory"),
    ("_coalesce", "method"),
])
def test_awkward_declaration_shapes_are_classified(asm_file, name, kind):
    lib = parse(asm_file, "oddballs.dart", ASM_ODDBALLS)
    by_name = {m.name: m for m in lib.classes[0].methods}
    assert name in by_name, "declaration shape no longer parses: %s" % name
    assert by_name[name].kind == kind


def test_generic_method_keeps_its_type_params_and_function_typed_arg(asm_file):
    lib = parse(asm_file, "oddballs.dart", ASM_ODDBALLS)
    m = [x for x in lib.classes[0].methods if x.name == "_coalesce"][0]
    assert m.type_params == "<Y0>"
    assert m.param_types == ["ContactsRepository", "String",
                             "(dynamic) => Future<Y0>"]


def test_dyn_forwarder_is_not_reported_as_a_source_member(asm_file):
    """`dyn:` entries are AOT invocation forwarders, not something the
    developer wrote. Calling one a method would overstate what was found."""
    lib = parse(asm_file, "oddballs.dart", ASM_ODDBALLS)
    m = [x for x in lib.classes[0].methods if x.name.startswith("dyn:")][0]
    assert m.kind == "dyn_forwarder"


# --------------------------------------------------------------------------- #
# body facts -- the tagged/untagged offset convention
# --------------------------------------------------------------------------- #

def test_body_offsets_are_lifted_to_declaration_offsets(asm_file):
    """Blutter prints `field_7` in a body and `field_8` on the declaration.
    kHeapObjectTag == 1, so decl == body + 1. Both are kept so no consumer
    ever has to guess which convention it is holding."""
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    m = [x for x in lib.classes[0].methods if x.name == "toJson"][0]
    loads = [a for a in m.body.field_access if a.kind == "load"]
    assert [(a.body_offset, a.decl_offset) for a in loads] == [(0x7, 0x8), (0xB, 0xC)]
    declared = {f.offset for f in lib.classes[0].fields}
    assert {a.decl_offset for a in loads} <= declared, \
        "every lifted offset must land on a declared slot"


def test_store_field_is_captured_with_its_base_register(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    m = [x for x in lib.classes[0].methods if x.name == "toJson"][0]
    store = [a for a in m.body.field_access if a.kind == "store"][0]
    assert (store.body_offset, store.decl_offset) == (0xF, 0x10)
    assert store.obj_reg == 2 and store.other == "r16"


def test_string_literals_are_kept_in_order_with_their_address(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    m = [x for x in lib.classes[0].methods if x.name == "toJson"][0]
    assert [s[1] for s in m.body.strings] == ["peer", "dir"]
    assert m.body.strings[0][0] == 0x6CF250


def test_semantic_events_are_kept_in_source_order(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    m = [x for x in lib.classes[0].methods if x.name == "toJson"][0]
    assert [e.kind for e in m.body.events] == [
        "SetupParameters", "StringLiteral", "LoadField",
        "StringLiteral", "LoadField", "StoreField", "Call",
    ]


def test_call_edge_is_split_into_library_class_and_method(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    m = [x for x in lib.classes[0].methods if x.name == "toJson"][0]
    edge = m.body.calls[0]
    assert edge.target_addr == 0x559DA8
    assert edge.lib == "package:chat/core/crypto/kdf.dart"
    assert edge.cls == "::" and edge.method == "_argon2Stretch"
    assert not edge.is_stub


def test_pool_field_entry_recovers_a_static_name(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    m = [x for x in lib.classes[0].methods if x.name == "toJson"][0]
    ref = m.body.static_fields[0]
    assert ref.name == "_veilLabel" and ref.offset == 0xE80
    assert ref.flags == "static late final"
    assert ref.confidence == pa.RECOVERED
    assert m.body.pool_refs[0].kind == "Field"


def test_allocation_stub_records_the_allocated_type(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    m = [x for x in lib.classes[0].methods if x.name == "toJson"][0]
    assert "Array" in m.body.alloc_types


def test_no_bodies_mode_keeps_addresses_but_drops_the_evidence(asm_file):
    path = asm_file("call_log_store.dart", ASM_CALL_LOG)
    lib = pa.parse_file(path, collect_bodies=False)
    m = [x for x in lib.classes[0].methods if x.name == "toJson"][0]
    assert m.addr == 0x6CF240, "the address comes from the decl, not the body"
    assert m.body.field_access == [] and m.body.strings == []


def test_body_events_are_off_by_default(asm_file):
    path = asm_file("call_log_store.dart", ASM_CALL_LOG)
    lib = pa.parse_file(path)
    m = [x for x in lib.classes[0].methods if x.name == "toJson"][0]
    assert m.body.events == []
    assert m.body.field_access, "facts are still collected; only the log is off"


# --------------------------------------------------------------------------- #
# malformed input
# --------------------------------------------------------------------------- #

def test_an_unknown_open_brace_does_not_desynchronise_the_file(asm_file):
    """A single unmatched `... {` once swallowed 28 real methods. The parser
    pushes an explicit `unknown` scope so the damage stops at that block."""
    lib = parse(asm_file, "weird.dart", """\
// lib: , url: package:chat/weird.dart
class Foo extends Object {
  some unrecognised construct {
    void swallowed(Foo) {
    }
  }
  void survivor(Foo) {
    // ** addr: 0x10, size: 0x4
  }
}
""")
    names = [m.name for m in lib.classes[0].methods]
    assert names == ["survivor"], \
        "recovery must resume after the unknown block, and not before it"
    assert any("some unrecognised construct" in u.text for u in lib.unparsed)


def test_unparsed_lines_carry_file_line_and_text(asm_file):
    lib = parse(asm_file, "weird.dart", """\
// lib: , url: package:chat/weird.dart
%%% not a Blutter line %%%
""")
    u = lib.unparsed[0]
    assert u.lineno == 2
    assert "%%%" in u.text
    assert u.path.endswith("weird.dart")


def test_an_empty_file_produces_no_classes_and_one_complaint(asm_file):
    lib = parse(asm_file, "empty.dart", "")
    assert lib.classes == []
    assert len(lib.unparsed) == 1


def test_truncated_file_does_not_raise(asm_file):
    """Blutter output can be cut short by a killed run. Parsing must degrade,
    not explode."""
    lib = parse(asm_file, "cut.dart", ASM_CALL_LOG[:len(ASM_CALL_LOG) // 3])
    assert lib.url == "package:chat/features/call/data/call_log_store.dart"
    assert lib.classes and lib.classes[0].name == "CallLogEntry"


def test_invalid_utf8_is_replaced_rather_than_fatal(tmp_path):
    p = os.path.join(str(tmp_path), "bad.dart")
    with open(p, "wb") as fh:
        fh.write(b"// lib: , url: package:chat/b.dart\n"
                 b'class A extends Object {\n  _ f(A) {\n'
                 b'    // 0x10: r16 = "\xff\xfe"\n  }\n}\n')
    lib = pa.parse_file(p)
    assert lib.classes[0].methods[0].name == "f"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,parts", [
    ("A, B", ["A", "B"]),
    ("ContactsRepository, String, (dynamic) => Future<Y0>",
     ["ContactsRepository", "String", "(dynamic) => Future<Y0>"]),
    ("Map<String, int>, bool", ["Map<String, int>", "bool"]),
    ("", []),
    ("  ,  ", []),
])
def test_split_top_level_respects_nesting(raw, parts):
    assert pa._split_top_level(raw) == parts


@pytest.mark.parametrize("payload,kind", [
    ('String: "peer"', "String"),
    ("Field <::._x@1>: static", "Field"),
    ("Obj!SecurityLevel@b6a121", "Obj!"),
    ('"peer"', "String"),
    ("Null", "Null"),
    ("something else", "raw"),
])
def test_pool_kind_classification(payload, kind):
    assert pa._pool_kind(payload) == kind


def test_unescape_handles_the_escapes_blutter_emits():
    assert pa._unescape('a\\nb') == "a\nb"
    assert pa._unescape('a\\"b') == 'a"b'


# --------------------------------------------------------------------------- #
# tree walk + stage 4 link
# --------------------------------------------------------------------------- #

def test_iter_asm_files_is_deterministic_and_filters_by_package(asm_tree):
    all_files = list(pa.iter_asm_files(asm_tree))
    assert len(all_files) == 3
    assert all_files == sorted(all_files), "order must be stable across runs"

    chat = list(pa.iter_asm_files(asm_tree, packages=["chat"]))
    assert len(chat) == 2
    assert all(os.sep + "chat" + os.sep in p for p in chat)


def test_iter_asm_files_accepts_a_single_file(asm_file):
    p = asm_file("one.dart", ASM_CALL_LOG)
    assert list(pa.iter_asm_files(p)) == [p]


def test_parse_tree_links_the_call_graph(asm_tree):
    prog = pa.parse_tree(asm_tree, packages=["chat"])
    assert len(prog.libraries) == 2
    assert prog.blutter_fingerprint == pa.BLUTTER_FORMAT_FINGERPRINT
    # toJson is indexed by its address, and resolves back to its owner.
    hit = prog.resolve(0x6CF240)
    assert hit is not None
    _lib, cls, m = hit
    assert cls.name == "CallLogEntry" and m.name == "toJson"
    # The call target 0x559da8 is not a method in this fixture, so no edge
    # may be invented for it.
    assert prog.callees_of.get(0x6CF240) == []


def test_link_records_both_directions(asm_file):
    lib = parse(asm_file, "pair.dart", """\
// lib: , url: package:chat/pair.dart
class A extends Object {
  _ callee(A) {
    // ** addr: 0x200, size: 0x4
  }
  _ caller(A) {
    // ** addr: 0x300, size: 0x4
    //     0x304: bl              #0x200  ; [package:chat/pair.dart] A::callee
  }
}
""")
    prog = pa.link(pa.Program(libraries=[lib]))
    assert prog.callees_of[0x300] == [0x200]
    assert prog.callers_of[0x200] == [0x300]


def test_packages_counts_libraries(asm_tree):
    prog = pa.parse_tree(asm_tree)
    assert prog.packages() == {"chat": 2, "flutter": 1}


# --------------------------------------------------------------------------- #
# reporting helpers
# --------------------------------------------------------------------------- #

def test_coverage_counts_facts_and_holes_separately(asm_tree):
    prog = pa.parse_tree(asm_tree, packages=["chat"])
    cov = pa.coverage(prog)
    assert cov["files"] == 2
    assert cov["unparsed_lines"] == 0
    assert cov["parse_coverage"] == 1.0
    assert cov["fields_name_DESTROYED"] >= 3
    assert cov["fields_named_RECOVERED"] == 1        # _veilLabel only
    assert cov["methods_return_type_UNKNOWN"] >= 1
    assert cov["methods_param_types_UNKNOWN"] >= 1
    assert cov["indexed_addresses"] == cov["methods_with_body"]


def test_coverage_drops_below_one_when_a_line_is_not_understood(asm_file):
    lib = parse(asm_file, "weird.dart",
                "// lib: , url: package:chat/w.dart\n%%% junk %%%\n")
    cov = pa.coverage(pa.Program(libraries=[lib]))
    assert cov["unparsed_lines"] == 1
    assert cov["parse_coverage"] < 1.0


def test_coverage_of_an_empty_program_does_not_divide_by_zero():
    cov = pa.coverage(pa.Program())
    assert cov["parse_coverage"] == 1.0
    assert cov["files"] == 0


def test_obfuscation_probe_fires_on_short_names(asm_file):
    body = "".join("  _ %s(A) {\n    // ** addr: 0x%x, size: 0x4\n  }\n" % (n, 0x100 + i)
                   for i, n in enumerate(["a", "b", "c", "d", "e", "readableName"]))
    lib = parse(asm_file, "obf.dart",
                "// lib: , url: package:chat/o.dart\nclass A extends Object {\n"
                + body + "}\n")
    probe = pa.probe_obfuscation(pa.Program(libraries=[lib]))
    assert probe["obfuscated"] is True
    assert probe["sampled"] == 6
    assert probe["short_name_ratio"] > 0.30


def test_obfuscation_probe_stays_quiet_on_real_names(asm_tree):
    probe = pa.probe_obfuscation(pa.parse_tree(asm_tree, packages=["chat"]))
    assert probe["obfuscated"] is False


def test_obfuscation_probe_on_an_empty_program_claims_nothing():
    assert pa.probe_obfuscation(pa.Program()) == {
        "obfuscated": False, "sampled": 0, "short_name_ratio": 0.0}


# --------------------------------------------------------------------------- #
# skeleton rendering
# --------------------------------------------------------------------------- #

def test_render_skeleton_labels_every_field_name_with_its_confidence(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    text = pa.render_skeleton(lib)
    assert "_OneByteString field_8; // offset: 0x8  [NAME-DESTROYED]" in text
    assert "_veilLabel; // offset: 0xe80  [RECOVERED]" in text
    # Every emitted field line is tagged, with no third unlabelled state.
    for line in text.splitlines():
        if "// offset:" in line:
            assert "[NAME-DESTROYED]" in line or "[RECOVERED]" in line


def test_render_skeleton_never_emits_a_body(asm_file):
    """The skeleton is a listing, not source. A `{` after a signature would
    read as a recovered body."""
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    for line in pa.render_skeleton(lib).splitlines():
        if "// 0x" in line and "(" in line:
            assert line.strip().split("//")[0].rstrip().endswith(";")


def test_render_skeleton_marks_unknown_types_in_the_signature(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    text = pa.render_skeleton(lib)
    assert "/*unknown*/ dynamic deriveVeilKey(/*param types unknown*/) async" in text


def test_render_skeleton_reports_the_address_or_says_there_is_none(asm_file):
    lib = parse(asm_file, "nobody.dart",
                "// lib: , url: package:chat/n.dart\n"
                "class A extends Object {\n  _ abstractish(A) {\n  }\n}\n")
    assert "// no-body" in pa.render_skeleton(lib)


def test_render_skeleton_includes_the_recovered_class_header(asm_file):
    lib = parse(asm_file, "call_log_store.dart", ASM_CALL_LOG)
    text = pa.render_skeleton(lib)
    assert "// class id: 2247, size: 0x24, attrs: const constructor" in text
    assert "class CallLogEntry extends Object {" in text
    assert "enum SecurityLevel extends _Enum {" in text


def test_skeleton_of_a_library_with_no_classes_is_still_identified(asm_file):
    lib = parse(asm_file, "bare.dart", "// lib: , url: package:chat/bare.dart\n")
    text = pa.render_skeleton(lib)
    assert "// url: package:chat/bare.dart" in text


# --------------------------------------------------------------------------- #
# serialisation
# --------------------------------------------------------------------------- #

def test_to_dict_is_json_serialisable_and_keeps_confidence_strings(asm_tree):
    import json
    prog = pa.parse_tree(asm_tree, packages=["chat"])
    blob = json.dumps([l.to_dict() for l in prog.libraries])
    assert '"name_confidence": "DESTROYED"' in blob
    assert '"name_confidence": "RECOVERED"' in blob


def test_format_fingerprint_is_recorded(asm_tree):
    """The parser's rules are pinned to a Blutter revision family. If that
    string ever silently changes, reports from different runs stop being
    comparable."""
    assert "blutter/asm-v1" in pa.BLUTTER_FORMAT_FINGERPRINT
