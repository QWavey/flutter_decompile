"""Shared fixtures for the flutter_decompile test suite.

Everything here is inline and offline. No test in this suite touches a real
APK, a real Blutter checkout, or the network -- if one ever does, it has
stopped testing the tool and started testing the machine it runs on.

The asm snippets are trimmed but not invented: their line shapes are copied
from the Blutter output the parser was written against (see the SEEN comments
in parse_asm.py), so a format change breaks these tests the way it would break
a real run.
"""

from __future__ import annotations

import os
import sys

import pytest

# The package is used from a checkout, not installed, so put the repo root on
# sys.path. pytest prepends tests/ only.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from flutter_decompile import ir  # noqa: E402


# --------------------------------------------------------------------------- #
# asm fixtures
# --------------------------------------------------------------------------- #

#: One library with a class whose toJson() interleaves map keys and field
#: loads -- the shape stage-5 inference depends on. Offsets are deliberately
#: the real ones: body says field_7, the declaration says field_8.
ASM_CALL_LOG = """\
// lib: , url: package:chat/features/call/data/call_log_store.dart
// class id: 2247, size: 0x24, field offset: 0x8
//   const constructor,
class CallLogEntry extends Object {

  _OneByteString field_8;
  _Mint field_c;
  static late final List<int> _veilLabel; // offset: 0xe80

  Map<String, dynamic> toJson(CallLogEntry) {
    // ** addr: 0x6cf240, size: 0xc8
    // 0x6cf240: SetupParameters(CallLogEntry this /* r1 => r0, fp-0x8 */)
    // 0x6cf250: r16 = "peer"
    // 0x6cf254: LoadField: r0 = r3->field_7
    // 0x6cf258: r16 = "dir"
    // 0x6cf25c: LoadField: r0 = r3->field_b
    // 0x6cf260: StoreField: r2->field_f = r16
    // 0x6cf264: r0 = AllocateArrayStub()
    //     0x6cf268: bl              #0x559da8  ; [package:chat/core/crypto/kdf.dart] ::_argon2Stretch
    //     0x6cf26c: ldr             x16, [x27, #0x1d418]  ; [pp+0x1d418] Field <::._veilLabel@823249941>: static late final (offset: 0xe80)
  }

  static _ deriveVeilKey(/* No info */) async {
    // ** addr: 0x559d14, size: 0x10
  }

  get _ _allowPaste(/* No info */) {
    // ** addr: 0x559d24, size: 0x10
  }

  set _ state=(/* No info */) {
    // ** addr: 0x559d34, size: 0x10
  }
}

enum SecurityLevel extends _Enum {

  _Mint field_8;

  _ toString(SecurityLevel) {
    // ** addr: 0x559d44, size: 0x10
  }
}
"""

#: The awkward declaration shapes: operators, closures, dyn: forwarders,
#: qualified constructors, generic methods, a two-line class header.
ASM_ODDBALLS = """\
// lib: , url: package:chat/core/oddballs.dart
// class id: 11, size: 0x18, field offset: 0x8
class _SecureKvStore extends Object
    implements KeyValueStore, Disposable {

  _Mint field_8;

  static _ BaselineOffset.+(/* No info */) {
    // ** addr: 0x20, size: 0x4
  }

  [closure] static void <anonymous closure>(dynamic) {
    // ** addr: 0x30, size: 0x4
  }

  void dyn:set:enabled(RenderBackdropFilter, bool) {
    // ** addr: 0x40, size: 0x4
  }

  _ ==(/* No info */) {
    // ** addr: 0x50, size: 0x4
  }

  static _ _SecureKvStore.(/* No info */) {
    // ** addr: 0x60, size: 0x4
  }

  factory _ _SecureKvStore.fromJson(/* No info */) {
    // ** addr: 0x70, size: 0x4
  }

  Future<Y0> _coalesce<Y0>(ContactsRepository, String, (dynamic) => Future<Y0>) {
    // ** addr: 0x80, size: 0x4
  }
}
"""


def write_asm(root: str, rel: str, text: str) -> str:
    """Write one asm/**/*.dart file, creating parents. Returns the path."""
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """Run every test from a scratch directory.

    cli.main() defaults its output to ./<name>_reconstructed, so a test that
    forgets -o would otherwise litter the checkout it is testing.
    """
    scratch = tmp_path / "cwd"
    scratch.mkdir()
    monkeypatch.chdir(str(scratch))


@pytest.fixture
def asm_file(tmp_path):
    """Factory: asm_file("kdf.dart", TEXT) -> path to a single asm file."""
    def make(name: str, text: str) -> str:
        return write_asm(str(tmp_path), name, text)
    return make


@pytest.fixture
def asm_tree(tmp_path):
    """A two-package asm/ tree, as Blutter would lay it out."""
    root = os.path.join(str(tmp_path), "asm")
    write_asm(root, "chat/features/call/data/call_log_store.dart", ASM_CALL_LOG)
    write_asm(root, "chat/core/oddballs.dart", ASM_ODDBALLS)
    write_asm(root, "flutter/src/widgets/framework.dart",
              "// lib: , url: package:flutter/src/widgets/framework.dart\n"
              "class Widget extends Object {\n}\n")
    return root


@pytest.fixture
def blutter_out(tmp_path, asm_tree):
    """A directory that passes blutter_driver.validate_output().

    Only the shapes the driver checks for -- an asm/ tree with .dart files
    plus objs.txt and pp.txt -- so cli.main() can adopt it with no Blutter
    installed and nothing downloaded.
    """
    out = os.path.dirname(asm_tree)          # tmp_path, which now holds asm/
    for name in ("objs.txt", "pp.txt"):
        with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
            fh.write("")
    return out


# --------------------------------------------------------------------------- #
# ir fixtures
# --------------------------------------------------------------------------- #

def ev(kind, **kw) -> ir.BodyEvent:
    return ir.BodyEvent(kind=kind, **kw)


def call_log_class() -> ir.ClassIR:
    """CallLogEntry with a toJson() whose events pair keys to field loads.

    Facts copied from the reference snapshot: class id 2247, size 0x24, first
    field at 0x8, toJson at 0x6cf240 loading field_7 / field_b / field_f.
    """
    cls = ir.ClassIR(
        name="CallLogEntry",
        class_id=2247,
        instance_size=0x24,
        field_offset_base=0x8,
        has_const_ctor=True,
        source=ir.SourceRef(asm_file="asm/chat/features/call/data/call_log_store.dart",
                            line=3),
    )
    cls.ensure_field(0x8, "_OneByteString")
    cls.ensure_field(0xC, "_Mint")
    cls.ensure_field(0x10, "_Mint")

    m = ir.MethodIR(
        name="toJson",
        return_vm_type="Map<String, dynamic>",
        address=0x6CF240,
        params_known=True,
        params=[ir.ParamIR(index=0, vm_type="CallLogEntry", is_receiver=True)],
        source=ir.SourceRef(asm_file="asm/chat/features/call/data/call_log_store.dart",
                            line=10),
    )
    m.events = [
        ev(ir.EventKind.STRING, value="peer", address=0x6CF250),
        ev(ir.EventKind.LOAD_FIELD, dst="r0", src="r3", offset=0x7, address=0x6CF254),
        ev(ir.EventKind.STRING, value="dir", address=0x6CF258),
        ev(ir.EventKind.LOAD_FIELD, dst="r0", src="r3", offset=0xB, address=0x6CF25C),
        ev(ir.EventKind.STRING, value="outcome", address=0x6CF260),
        ev(ir.EventKind.LOAD_FIELD, dst="r4", src="r3", offset=0xF, address=0x6CF264),
    ]
    cls.methods.append(m)
    return cls


def security_level_enum() -> ir.ClassIR:
    """An enum with a real ordinal gap (1 is missing) and a payload slot."""
    en = ir.ClassIR(name="SecurityLevel", super_name="_Enum", is_enum=True,
                    class_id=5907, instance_size=0x1C)
    en.ensure_field(0x8, "_Mint")
    en.ensure_field(0x10, "_OneByteString")
    en.ensure_field(0x14, "_Mint")
    en.enum_values = [
        ir.EnumValueIR(name="standard", ordinal=0, obj_address="Obj!SecurityLevel@b6a121",
                       extra={0x14: 0x10}),
        ir.EnumValueIR(name="fortress", ordinal=2, extra={0x14: 0x30}),
    ]
    return en


def make_program(*classes: ir.ClassIR,
                 url: str = "package:chat/features/call/data/call_log_store.dart"
                 ) -> ir.ProgramIR:
    lib = ir.LibraryIR(url=url,
                       asm_file="asm/chat/features/call/data/call_log_store.dart",
                       classes=list(classes))
    prog = ir.ProgramIR(
        meta=ir.ProgramMeta(input_name="app.apk", dart_version="3.4.0",
                            snapshot_hash="deadbeef", generated_at="2025-01-01T00:00:00"),
        libraries=[lib],
    )
    prog.link()
    return prog


@pytest.fixture
def program() -> ir.ProgramIR:
    return make_program(call_log_class(), security_level_enum())


# --------------------------------------------------------------------------- #
# the invariant every inference must satisfy
# --------------------------------------------------------------------------- #

def assert_no_bare_inferences(program: ir.ProgramIR) -> int:
    """No inferred name may exist without evidence and a confidence.

    This is the tool's central promise, so it is checked as a reusable
    assertion rather than as one test: any test that runs inference can call
    it. Returns how many inferred names it vetted.
    """
    checked = 0
    for _lib, cls, f in program.all_fields():
        if not f.inferred_name:
            continue
        checked += 1
        where = "%s.%s (slot 0x%x)" % (cls.name, f.inferred_name, f.offset)
        assert f.evidence, "INFERRED name emitted with no evidence: " + where
        assert f.name_confidence.is_inferred, (
            "INFERRED name %s carries confidence %s, which is not an inferred "
            "level" % (where, f.name_confidence.value))
        assert f.confidence is f.name_confidence, (
            "reported confidence disagrees with name_confidence for " + where)
        for e in f.evidence:
            assert e.rule, "evidence with no rule id on " + where
            assert e.detail, "evidence with no detail on " + where
    return checked
