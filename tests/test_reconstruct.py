"""Unit tests for the body reconstruction emitter."""
import os

import flutter_decompile.parse_asm as pa
from flutter_decompile import reconstruct as rc


def _cls_with_field(offset_decl, name=None, conf=pa.RECOVERED):
    c = pa.ClassIR(name="C")
    c.fields.append(pa.FieldIR(name=name, vm_type="_Mint", offset=offset_decl,
                               name_confidence=conf))
    return c


def _ev(kind, text, addr=0x1000):
    return pa.BodyEvent(addr=addr, kind=kind, text=text)


# ---- register renaming ---------------------------------------------------- #

def test_v_renames_registers_not_other_tokens():
    assert rc._v("mov x1, x3") == "mov x1, x3"          # x-regs untouched
    assert rc._v("r0 = r4") == "v0 = v4"
    assert rc._v("field_0x13") == "field_0x13"          # not a register


# ---- field naming --------------------------------------------------------- #

def test_field_falls_back_to_hole_when_name_destroyed():
    cls = _cls_with_field(0x14, name=None)
    # body offset 0x13 -> decl offset 0x14
    assert rc._field_name(cls, 0x13) == "field_0x13"


def test_field_uses_recovered_name():
    cls = _cls_with_field(0x14, name="userId", conf=pa.RECOVERED)
    assert rc._field_name(cls, 0x13) == "userId"


# ---- per-event rendering -------------------------------------------------- #

def test_render_load_and_store_field():
    cls = _cls_with_field(0x14, name=None)
    opts = rc.EmitOptions()
    load = rc._render_event(_ev("LoadField", "LoadField: r0 = r4->field_13"), cls, {}, opts)
    assert load == "v0 = v4.field_0x13;"
    store = rc._render_event(_ev("StoreField", "StoreField: r4->field_13 = r2"), cls, {}, opts)
    assert store == "v4.field_0x13 = v2;"


def test_render_string_literal():
    out = rc._render_event(_ev("StringLiteral", 'r2 = "."'), pa.ClassIR("C"), {}, rc.EmitOptions())
    assert out == 'v2 = ".";'


def test_render_call_resolves_target_when_edge_present():
    ev = _ev("Call", "r0 = decode()", addr=0x2000)
    edge = pa.CallEdge(site_addr=0x2000, target_addr=0x9, annotation="",
                       lib="dart:convert", cls="Base64Codec", method="decode")
    out = rc._render_event(ev, pa.ClassIR("C"), {0x2000: edge}, rc.EmitOptions())
    assert out == "v0 = decode();  // -> [dart:convert] Base64Codec::decode"


def test_render_call_without_edge_has_no_target_comment():
    ev = _ev("Call", "r0 = foo()", addr=0x2000)
    out = rc._render_event(ev, pa.ClassIR("C"), {}, rc.EmitOptions())
    assert out == "v0 = foo();"


# ---- noise filtering ------------------------------------------------------ #

def test_noise_is_dropped_by_default_but_kept_with_keep_asm():
    cls = pa.ClassIR("C")
    for noisy in ("EnterFrame", "LeaveFrame", "mov x1, x3",
                  "stur x1, [fp, #-0x60]", "DecompressPointer r0",
                  "CheckStackOverflow", "AllocStack(0x78)"):
        assert rc._is_noise(noisy), noisy
        assert rc._render_event(_ev("asm", noisy), cls, {}, rc.EmitOptions()) is None
        kept = rc._render_event(_ev("asm", noisy), cls, {}, rc.EmitOptions(keep_asm=True))
        assert kept is not None and kept.startswith("//")


def test_value_and_control_flow_lines_are_never_noise():
    # These carry meaning and must survive the default filter.
    for meaningful in ("cmp w3, NULL", "b.ne #0x8ee8cc", "r0 = Null",
                       "r0 = LoadClassIdInstr(r3)", "r0 = true", "ret"):
        assert not rc._is_noise(meaningful), meaningful


# ---- whole-library shape -------------------------------------------------- #

def test_render_library_carries_the_reconstruction_header():
    lib = pa.LibraryIR(url="package:chat/x.dart", asm_path="/x")
    text = rc.render_library(lib)
    assert "RECONSTRUCTED by flutter_decompile -- NOT original source" in text
    assert "package:chat/x.dart" in text


def test_library_scope_is_emitted_flat_not_as_a_class():
    lib = pa.LibraryIR(url="package:app/x.dart", asm_path="/x")
    scope = pa.ClassIR(name="::")            # Blutter's library scope
    scope.methods.append(pa.MethodIR(name="topLevelFn", is_static=True))
    lib.classes.append(scope)
    text = rc.render_library(lib)
    assert "class :: {" not in text
    assert "top-level declarations" in text
    assert "topLevelFn" in text


def test_emit_pubspec_recovers_dependency_names(tmp_path):
    asm = tmp_path / "asm"
    for pkg in ("chat", "http", "crypto", "flutter", "core", "async"):
        (asm / pkg).mkdir(parents=True)
    dest = tmp_path / "dart"
    path = rc.emit_pubspec(str(asm), str(dest), ["chat"])
    text = open(path, encoding="utf-8").read()

    assert "name: chat" in text
    assert "http: any" in text and "crypto: any" in text   # real pub deps
    assert "chat: any" not in text                         # the app itself
    assert "flutter: any" not in text and "core: any" not in text  # SDK excluded
    assert "version constraints the author wrote were not kept" in text


def test_one_bad_library_does_not_sink_the_whole_tree(tmp_path):
    good = pa.LibraryIR(url="package:app/good.dart", asm_path="/g")
    bad = pa.LibraryIR(url="package:app/bad.dart", asm_path="/b")
    bad.classes = [object()]          # not a ClassIR -> render raises
    good2 = pa.LibraryIR(url="package:app/good2.dart", asm_path="/g2")
    prog = pa.Program(libraries=[good, bad, good2])

    logs = []
    written = rc.emit_tree(prog, str(tmp_path), log=logs.append)

    assert len(written) == 3, "every library must still produce a file"
    assert any("could not be fully reconstructed" in m for m in logs)
    bad_file = [w for w in written if os.path.basename(w) == "bad.dart"][0]
    with open(bad_file, encoding="utf-8") as fh:
        assert "RECONSTRUCTION FAILED for this file" in fh.read()
