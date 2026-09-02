"""emit.py -- IR to a Dart source tree, and the CLI's refusal to run it.

Two separate things are pinned here.

`emit.py` is a library: given an IR it renders a skeleton, and its emission
contract (throwing bodies, evidence on every inferred name, loud enum gaps) is
what stops that skeleton from reading like recovered source.

The CLI is the product, and in 0.1 it REFUSES to run stage 6 at all. That
refusal is a feature. A regression that turned it into a silent success would
hand users a tree of plausible-looking Dart the tool cannot justify, which is
the exact failure this project exists to avoid -- so it is tested as hard as
the emitter itself.
"""

from __future__ import annotations

import os

import pytest

from flutter_decompile import cli
from flutter_decompile import emit
from flutter_decompile import infer
from flutter_decompile import ir

from conftest import call_log_class, make_program


@pytest.fixture
def rendered(program):
    """The fixture program, inferred and emitted, with nothing written."""
    infer.infer_program(program)
    return emit.emit_program(
        program, emit.EmitOptions(out_dir="/does/not/exist", write=False))


def library_text(result) -> str:
    return [f for f in result.files if f.rel_path.endswith("call_log_store.dart")][0].text


# --------------------------------------------------------------------------- #
# rule 1 -- every file says what it is
# --------------------------------------------------------------------------- #

def test_every_emitted_file_says_it_is_not_the_original_source(rendered):
    """Rule 1 of the emission contract, with no exception for support files."""
    for f in rendered.files:
        flat = " ".join(f.text.lower().split())     # the banners hard-wrap
        assert "not the original source" in flat, \
            "%s could be mistaken for source" % f.rel_path


def test_every_emitted_dart_file_carries_the_full_banner(rendered):
    for f in rendered.files:
        if f.rel_path.endswith(".dart"):
            assert "RECONSTRUCTED FROM A FLUTTER AOT SNAPSHOT" in f.text
            assert "ignore_for_file:" in f.text


def test_the_banner_explains_what_is_destroyed(rendered):
    text = library_text(rendered)
    assert "machine code, not source" in text
    assert "field_<offset>" in text
    assert "Absence is not evidence of absence" in text


def test_the_banner_names_the_input_and_the_library(rendered):
    text = library_text(rendered)
    assert "app.apk" in text and "dart 3.4.0" in text
    assert "package:chat/features/call/data/call_log_store.dart" in text


def test_an_obfuscated_snapshot_is_announced_before_anything_else():
    prog = make_program(call_log_class())
    prog.meta.obfuscated = True
    result = emit.emit_program(prog, emit.EmitOptions(write=False))
    assert "OBFUSCATED" in library_text(result)


# --------------------------------------------------------------------------- #
# rule 4 -- no body is ever faked
# --------------------------------------------------------------------------- #

def test_every_method_body_throws(rendered):
    text = library_text(rendered)
    assert "throw UnimplementedError(" in text
    assert "return null;" not in text
    assert "// TODO" not in text


def test_the_thrown_message_names_the_address_and_the_asm_line(rendered):
    text = library_text(rendered)
    assert "CallLogEntry.toJson @0x6cf240" in text
    assert "call_log_store.dart:10" in text
    assert "machine code for this method, not Dart source" in text


def test_no_method_is_emitted_with_an_empty_body(rendered):
    """`{}` would read as `this method does nothing`, which is a claim the
    snapshot does not support."""
    for f in rendered.files:
        if not f.rel_path.endswith(".dart"):
            continue
        lines = [l.rstrip() for l in f.text.splitlines()]
        for i, line in enumerate(lines[:-1]):
            if line.endswith("{") and not line.lstrip().startswith("//"):
                block = lines[i + 1:]
                assert block and block[0].strip() != "}", \
                    "empty body at %s:%d" % (f.rel_path, i + 1)


def test_every_stubbed_method_is_counted(rendered):
    assert rendered.stats["methods"] == rendered.stats["methods_stubbed"]
    assert rendered.stats.get("methods_reconstructed", 0) == 0


# --------------------------------------------------------------------------- #
# rule 5 -- inferred names carry their evidence
# --------------------------------------------------------------------------- #

def test_an_inferred_field_is_labelled_INFERRED_with_its_confidence(rendered):
    text = library_text(rendered)
    assert "/// INFERRED field name (INFERRED_HIGH) for slot 0x8." in text
    assert "destroyed by AOT compilation" in text


def test_an_inferred_field_prints_its_evidence_trail(rendered):
    text = library_text(rendered)
    assert "///   evidence: json_key_pairing:" in text
    assert 'map key "peer"' in text


def test_no_inferred_field_is_emitted_without_an_evidence_line(rendered):
    """The emitted file, not just the IR, must show the reasoning."""
    for f in rendered.files:
        blocks = f.text.split("\n\n")
        for block in blocks:
            if "/// INFERRED field name" in block:
                assert "///   evidence:" in block, \
                    "an INFERRED name reached the file with no evidence:\n" + block


def test_evidence_can_be_switched_off_only_by_asking_for_it(program):
    infer.infer_program(program)
    quiet = emit.emit_program(
        program, emit.EmitOptions(write=False, emit_evidence=False))
    text = library_text(quiet)
    assert "/// INFERRED field name" in text, "the label itself is never optional"
    assert "///   evidence:" not in text


def test_an_uninferred_field_keeps_its_offset_and_says_the_name_is_gone():
    cls = call_log_class()
    cls.methods = []                      # no bodies, so no evidence at all
    result = emit.emit_program(make_program(cls), emit.EmitOptions(write=False))
    text = library_text(result)
    assert "/// UNKNOWN NAME. Slot 0x8 only." in text
    assert "field_8;" in text
    assert "do not exist in an AOT snapshot" in text


def test_a_recovered_static_name_is_labelled_RECOVERED_not_inferred():
    cls = ir.ClassIR(name="Kdf")
    f = cls.ensure_field(0xE80, "List<int>")
    f.is_static = True
    f.recovered_name = "_veilLabel"
    f.static_slot = 0xE80
    result = emit.emit_program(make_program(cls), emit.EmitOptions(write=False))
    text = library_text(result)
    assert "/// RECOVERED field name" in text
    assert "Field <Kdf._veilLabel>" in text
    assert "INFERRED" not in text.split("class Kdf")[1]


def test_a_lowered_vm_type_is_annotated_and_the_raw_type_kept(rendered):
    text = library_text(rendered)
    assert "/// type note: lowered VM type `_OneByteString`" in text
    assert "// VM type: _OneByteString" in text


# --------------------------------------------------------------------------- #
# rule 2 -- enums are emitted exactly
# --------------------------------------------------------------------------- #

def test_enum_values_keep_their_recovered_ordinals(rendered):
    text = library_text(rendered)
    assert "standard, // RECOVERED: ordinal 0" in text
    assert "fortress, // RECOVERED: ordinal 2" in text


def test_a_missing_ordinal_becomes_a_loud_placeholder_not_a_shift(rendered):
    text = library_text(rendered)
    assert "$missingOrdinal1, // UNKNOWN: no const instance at ordinal 1" in text
    assert "ORDINAL GAPS at 1" in text
    body = text.split("enum SecurityLevel {")[1]
    assert body.index("standard") < body.index("$missingOrdinal1") < body.index("fortress")


def test_an_enum_with_no_const_instances_says_so_rather_than_emitting_nothing():
    en = ir.ClassIR(name="Empty", is_enum=True, super_name="_Enum")
    result = emit.emit_program(make_program(en), emit.EmitOptions(write=False))
    text = library_text(result)
    assert "$noConstInstances" in text
    assert "not one value name survived" in text


def test_enum_payload_slots_are_documented_not_re_declared(rendered):
    """A Dart enum's fields must be final and const-initialised, and the
    constructor is not recoverable, so re-declaring them would be a guess."""
    text = library_text(rendered)
    assert "const payload slots (0x14)" in text
    assert "slot 0x14 = 0x10" in text
    enum_body = text.split("enum SecurityLevel {")[1]
    assert "field_14" not in enum_body


def test_the_implicit_enum_slots_are_not_emitted_as_fields(rendered):
    enum_body = library_text(rendered).split("enum SecurityLevel {")[1]
    assert "index;" not in enum_body and "_name;" not in enum_body


# --------------------------------------------------------------------------- #
# rule 6 -- a type is only named if the tree declares it
# --------------------------------------------------------------------------- #

def test_an_undeclared_type_degrades_to_dynamic_with_a_note():
    cls = ir.ClassIR(name="Holder")
    cls.ensure_field(0x8, "SomeWidgetNobodyEmitted")
    result = emit.emit_program(make_program(cls), emit.EmitOptions(write=False))
    text = library_text(result)
    assert "unresolved type `SomeWidgetNobodyEmitted`" in text
    assert "dynamic field_8;" in text


def test_a_declared_type_survives_and_pulls_in_its_import():
    a = ir.ClassIR(name="Alpha")
    b = ir.ClassIR(name="Beta")
    b.ensure_field(0x8, "Alpha")
    lib_a = ir.LibraryIR(url="package:chat/a.dart", classes=[a])
    lib_b = ir.LibraryIR(url="package:chat/sub/b.dart", classes=[b])
    prog = ir.ProgramIR(libraries=[lib_a, lib_b])
    result = emit.emit_program(prog, emit.EmitOptions(write=False))
    text = [f for f in result.files if f.rel_path.endswith("sub/b.dart")][0].text
    assert "import '../a.dart';" in text
    assert "Alpha field_8;" in text


def test_an_ambiguous_type_name_is_refused_rather_than_picked():
    a1 = ir.ClassIR(name="Dup")
    a2 = ir.ClassIR(name="Dup")
    holder = ir.ClassIR(name="Holder")
    holder.ensure_field(0x8, "Dup")
    prog = ir.ProgramIR(libraries=[
        ir.LibraryIR(url="package:chat/one.dart", classes=[a1]),
        ir.LibraryIR(url="package:chat/two.dart", classes=[a2]),
        ir.LibraryIR(url="package:chat/holder.dart", classes=[holder]),
    ])
    result = emit.emit_program(prog, emit.EmitOptions(write=False))
    text = [f for f in result.files if f.rel_path.endswith("holder.dart")][0].text
    assert "declared in 2 libraries (ambiguous)" in text
    assert "dynamic field_8;" in text


def test_an_sdk_type_brings_its_dart_import():
    cls = ir.ClassIR(name="Holder")
    cls.ensure_field(0x8, "Uint8List")
    result = emit.emit_program(make_program(cls), emit.EmitOptions(write=False))
    assert "import 'dart:typed_data';" in library_text(result)


# --------------------------------------------------------------------------- #
# parameters -- names are destroyed, arity may be unknown
# --------------------------------------------------------------------------- #

def test_an_unknown_parameter_list_is_not_emitted_as_no_parameters():
    cls = ir.ClassIR(name="A")
    cls.methods.append(ir.MethodIR(name="f", params_known=False, address=0x10))
    text = library_text(emit.emit_program(make_program(cls),
                                          emit.EmitOptions(write=False)))
    assert "(/* parameter list UNKNOWN in snapshot */)" in text
    assert "do not read that as `takes none`" in text


def test_a_destroyed_parameter_name_is_emitted_positionally_and_flagged():
    cls = ir.ClassIR(name="A")
    cls.methods.append(ir.MethodIR(
        name="f", params_known=True, address=0x10,
        params=[ir.ParamIR(index=0, vm_type="_Mint")]))
    text = library_text(emit.emit_program(make_program(cls),
                                          emit.EmitOptions(write=False)))
    assert "int a0" in text
    assert "name is DESTROYED" in text


def test_the_receiver_is_not_emitted_as_a_parameter(rendered):
    assert "toJson()" in library_text(rendered)


# --------------------------------------------------------------------------- #
# name collisions inside the emitted tree
# --------------------------------------------------------------------------- #

def test_two_members_with_the_same_name_are_suffixed_and_the_clash_noted():
    cls = ir.ClassIR(name="A")
    cls.methods.append(ir.MethodIR(name="f", address=0x10))
    cls.methods.append(ir.MethodIR(name="f", address=0x20))
    text = library_text(emit.emit_program(make_program(cls),
                                          emit.EmitOptions(write=False)))
    assert "f$2" in text
    assert "Name collision inside this class" in text


def test_a_member_named_like_its_class_is_renamed_not_dropped():
    cls = ir.ClassIR(name="A")
    cls.ensure_field(0x8, "_Mint")
    cls.fields[0x8].recovered_name = "A"
    text = library_text(emit.emit_program(make_program(cls),
                                          emit.EmitOptions(write=False)))
    assert "A$field" in text


# --------------------------------------------------------------------------- #
# const pool + manifest
# --------------------------------------------------------------------------- #

def test_string_constants_are_emitted_verbatim():
    prog = make_program(ir.ClassIR(name="A"))
    prog.pool_strings = {"pp+0xfeb0": "peer's key\n"}
    result = emit.emit_program(prog, emit.EmitOptions(write=False))
    pool = [f for f in result.files if f.rel_path.endswith("_const_pool.dart")][0]
    assert "'pp+0xfeb0': 'peer\\'s key\\n'," in pool.text
    assert result.stats["pool_strings"] == 1


def test_an_unmatched_const_object_is_kept_as_data_not_reconstructed():
    prog = make_program(ir.ClassIR(name="A"))
    prog.orphan_consts = [ir.ConstObjectIR(class_name="Mystery", address="@b6a121",
                                           slots={0x8: 3, 0x10: "x"})]
    result = emit.emit_program(prog, emit.EmitOptions(write=False))
    pool = [f for f in result.files if f.rel_path.endswith("_const_pool.dart")][0]
    assert "UNMATCHED" in pool.text
    assert "slot 0x8" in pool.text
    assert "Mystery(" not in pool.text, "a constructor call would be invented"


def test_the_manifest_reports_counts_and_refuses_to_call_the_tree_buildable(rendered):
    manifest = [f for f in rendered.files if f.rel_path == "RECONSTRUCTION.txt"][0]
    assert "it will not build as an app" in manifest.text
    assert "1, of which 1 emit a throwing stub" in manifest.text
    assert "0 recovered name, 3 inferred name" in manifest.text


def test_coverage_string_never_rounds_a_real_gap_up_to_100_percent():
    assert emit._coverage_str(1.0) == "100.00%"
    assert "just under 100%" in emit._coverage_str(0.9999999)
    assert emit._coverage_str(0.5) == "50.00%"


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #

def test_write_false_touches_no_disk(tmp_path, program):
    out = os.path.join(str(tmp_path), "out")
    os.makedirs(out)
    emit.emit_program(program, emit.EmitOptions(out_dir=out, write=False))
    assert os.listdir(out) == []


def test_write_true_mirrors_the_package_tree(tmp_path, program):
    infer.infer_program(program)
    result = emit.emit_program(program, emit.EmitOptions(out_dir=str(tmp_path)))
    expected = os.path.join(str(tmp_path), "lib", "features", "call", "data",
                            "call_log_store.dart")
    assert os.path.isfile(expected)
    assert os.path.isfile(os.path.join(str(tmp_path), "RECONSTRUCTION.txt"))
    with open(expected, encoding="utf-8") as fh:
        assert fh.read() == [f for f in result.files
                             if f.abs_path == expected][0].text


def test_non_primary_packages_go_under_packages(tmp_path):
    prog = make_program(ir.ClassIR(name="Widget"),
                        url="package:flutter/src/widgets/framework.dart")
    result = emit.emit_program(
        prog, emit.EmitOptions(out_dir=str(tmp_path), write=False,
                               primary_packages=("chat",)))
    rels = [f.rel_path for f in result.files]
    assert "packages/flutter/lib/src/widgets/framework.dart" in rels


# --------------------------------------------------------------------------- #
# THE REFUSAL -- stage 6 is not implemented and the CLI must not pretend
# --------------------------------------------------------------------------- #

def dart_files_under(root: str):
    return [os.path.join(d, f)
            for d, _dirs, files in os.walk(root)
            for f in files if f.endswith(".dart")]


def test_emit_exits_non_zero_and_writes_no_dart(tmp_path, blutter_out, capsys):
    out = os.path.join(str(tmp_path), "out")
    rc = cli.main(["--blutter-out", blutter_out, "-o", out,
                   "--report", "none", "--emit"])
    assert rc == 4, "a refusal that exits 0 is not a refusal"
    assert dart_files_under(out) == [], \
        "stage 6 is unimplemented, so not one line of Dart may be written"


def test_the_refusal_explains_itself(tmp_path, blutter_out, capsys):
    out = os.path.join(str(tmp_path), "out")
    cli.main(["--blutter-out", blutter_out, "-o", out, "--report", "none", "--emit"])
    err = capsys.readouterr().err
    assert "NOT IMPLEMENTED" in err
    assert "will not write plausible-looking Dart it cannot justify" in err


def test_emit_is_advertised_as_unimplemented_in_the_help():
    help_text = cli.build_parser().format_help()
    assert "--emit" in help_text
    assert "NOT IMPLEMENTED" in help_text


def test_the_unimplemented_stages_are_declared_in_the_package():
    from flutter_decompile import IMPLEMENTED_STAGES, UNIMPLEMENTED_STAGES
    assert "verify" in UNIMPLEMENTED_STAGES
    assert set(IMPLEMENTED_STAGES) & set(UNIMPLEMENTED_STAGES) == set()


def test_a_normal_run_writes_a_listing_not_compilable_dart(tmp_path, blutter_out):
    """--skeleton writes files ending in .dart. They are a LISTING: signatures
    terminated by `;` with confidence tags, never a body. Anything else would
    be stage 6 by the back door."""
    out = os.path.join(str(tmp_path), "out")
    rc = cli.main(["--blutter-out", blutter_out, "-o", out, "--skeleton", "*",
                   "--report", "none"])
    assert rc == 0
    written = dart_files_under(out)
    assert written, "the skeleton stage produced nothing to check"
    for path in written:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        assert "throw UnimplementedError" not in text
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("//") or not stripped:
                continue
            if "(" not in stripped:
                continue                      # a class header or a `}`
            code = stripped.split("//")[0].rstrip()
            assert code.endswith(";"), \
                "%s emits a member as something other than a declaration: %r" % (
                    path, stripped)
