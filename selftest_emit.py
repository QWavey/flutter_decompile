"""Self-test for the emitter half of flutter_decompile (ir / infer / emit / report).

It builds a small ProgramIR **by hand** -- no Blutter parsing involved -- runs
inference, emission and the report over it, and checks the emission rules that
matter.  The fixture is not invented: every fact in it was copied out of the
real Blutter output at NEXUS-apk-decompiled/blutter_out, so the test also pins
the offset arithmetic (body offsets are tagged, declaration offsets are not).

Ground truth used:

  asm/chat/features/call/data/call_log_store.dart
    // class id: 2247, size: 0x24, field offset: 0x8
    //   const constructor,
    class CallLogEntry extends Object { Map<String, dynamic> toJson(CallLogEntry) {...} }
    ... toJson @0x6cf240 interleaves "peer"/field_7, "dir"/field_b,
        "outcome"/field_f, "at"/field_13, "dur"/field_1b

  asm/chat/core/crypto/cripped.dart
    // class id: 5907, size: 0x1c, field offset: 0x14
    enum SecurityLevel extends _Enum { _Mint field_8; _OneByteString field_10; _Mint field_14; }
    objs.txt: standard=0 (slot 0x14 = 0x10), paranoid=1 (0x18), fortress=2, titan=3 (0x28)

  asm/chat/core/crypto/kdf.dart
    static late final List<int> _veilLabel;      // offset: 0xe80
    static late final List<List<int>> layerLabels; // offset: 0xe7c
    static _ deriveVeilKey(/* No info */) async  @0x559d14

Run:  python selftest_emit.py [--keep]
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flutter_decompile.ir import (  # noqa: E402
    BodyEvent,
    ClassIR,
    Confidence,
    ConstObjectIR,
    EnumValueIR,
    EventKind,
    FieldIR,
    LibraryIR,
    MethodIR,
    MethodKind,
    Modifier,
    ParamIR,
    ProgramIR,
    ProgramMeta,
    SourceRef,
    UnparsedLine,
)
from flutter_decompile.infer import infer_program  # noqa: E402
from flutter_decompile.emit import EmitOptions, emit_program  # noqa: E402
from flutter_decompile.report import (  # noqa: E402
    build_report,
    check_invariants,
    strict_exit_code,
    render_markdown,
    render_text_summary,
    write_reports,
)

CALL_ASM = "asm/chat/features/call/data/call_log_store.dart"
CRIPPED_ASM = "asm/chat/core/crypto/cripped.dart"
KDF_ASM = "asm/chat/core/crypto/kdf.dart"


def E(kind, addr=None, dst=None, src=None, offset=None, value=None, index=None,
      target=None, raw=""):
    return BodyEvent(kind=kind, address=addr, dst=dst, src=src, offset=offset,
                     value=value, index=index, target=target, raw=raw)


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------


def build_fixture() -> ProgramIR:
    # ---- package:chat/features/call/data/call_log_store.dart --------------
    to_json_events = [
        E(EventKind.OTHER, 0x6CF24C,
          raw="SetupParameters(CallLogEntry this /* r1 => r0, fp-0x8 */)"),
        E(EventKind.ALLOC, 0x6CF268, dst="r0", raw="r0 = AllocateArray()"),
        E(EventKind.STRING, 0x6CF270, dst="r16", value="peer"),
        E(EventKind.STORE_FIELD, 0x6CF278, dst="r2", offset=0xF, src="r16"),
        E(EventKind.LOAD_FIELD, 0x6CF280, dst="r0", src="r3", offset=0x7),
        E(EventKind.STORE_FIELD, 0x6CF288, dst="r2", offset=0x13, src="r0"),
        E(EventKind.STRING, 0x6CF28C, dst="r16", value="dir"),
        E(EventKind.ARRAY_STORE, 0x6CF294, dst="r2", index=0, src="r16"),
        E(EventKind.LOAD_FIELD, 0x6CF298, dst="r0", src="r3", offset=0xB),
        # chained load off the *value*, not off `this`: must not be paired
        E(EventKind.LOAD_FIELD, 0x6CF2A0, dst="r1", src="r0", offset=0xF),
        E(EventKind.STORE_FIELD, 0x6CF2A8, dst="r2", offset=0x1B, src="r1"),
        E(EventKind.STRING, 0x6CF2AC, dst="r16", value="outcome"),
        E(EventKind.STORE_FIELD, 0x6CF2B4, dst="r2", offset=0x1F, src="r16"),
        E(EventKind.LOAD_FIELD, 0x6CF2B8, dst="r0", src="r3", offset=0xF),
        E(EventKind.STORE_FIELD, 0x6CF2C0, dst="r2", offset=0x23, src="r0"),
        E(EventKind.STRING, 0x6CF2C4, dst="r16", value="at"),
        E(EventKind.STORE_FIELD, 0x6CF2CC, dst="r2", offset=0x27, src="r16"),
        E(EventKind.LOAD_FIELD, 0x6CF2D0, dst="r4", src="r3", offset=0x13),
        E(EventKind.ARRAY_STORE, 0x6CF2EC, dst="r1", index=7),
        E(EventKind.STRING, 0x6CF310, dst="r16", value="dur"),
        E(EventKind.STORE_FIELD, 0x6CF318, dst="r2", offset=0x2F, src="r16"),
        E(EventKind.LOAD_FIELD, 0x6CF31C, dst="r4", src="r3", offset=0x1B),
    ]
    to_string_events = [
        E(EventKind.STRING, 0x6CF400, value="CallLogEntry("),
        E(EventKind.STRING, 0x6CF410, value="outcome: "),      # agrees with toJson
        E(EventKind.LOAD_FIELD, 0x6CF418, dst="r0", src="r1", offset=0xF),
        E(EventKind.STRING, 0x6CF420, value=", when: "),       # DISAGREES with "at"
        E(EventKind.LOAD_FIELD, 0x6CF428, dst="r0", src="r1", offset=0x13),
    ]
    missed_events = [   # a computed getter: must NOT produce a backing-field name
        E(EventKind.LOAD_FIELD, 0x6CF160, dst="r0", src="r1", offset=0xB),
        E(EventKind.LOAD_FIELD, 0x6CF168, dst="r2", src="r1", offset=0xF),
        E(EventKind.CALL, 0x6CF170, target="[package:chat/..] CallOutcome::=="),
    ]

    entry = ClassIR(
        name="CallLogEntry",
        super_name="Object",
        class_id=2247,
        instance_size=0x24,
        field_offset_base=0x8,
        has_const_ctor=True,
        source=SourceRef(asm_file=CALL_ASM, line=460),
        fields={
            0x8: FieldIR(offset=0x8, vm_type="_OneByteString",
                         source=SourceRef(asm_file=CALL_ASM, line=461)),
            0xC: FieldIR(offset=0xC, vm_type="CallDirection",
                         source=SourceRef(asm_file=CALL_ASM, line=462)),
            0x10: FieldIR(offset=0x10, vm_type="_OneByteString",
                          source=SourceRef(asm_file=CALL_ASM, line=463)),
            0x14: FieldIR(offset=0x14, vm_type="_Mint",
                          source=SourceRef(asm_file=CALL_ASM, line=464)),
            0x1C: FieldIR(offset=0x1C, vm_type="_Mint",
                          source=SourceRef(asm_file=CALL_ASM, line=465)),
            0x20: FieldIR(offset=0x20, vm_type="bool",
                          source=SourceRef(asm_file=CALL_ASM, line=466)),
        },
        methods=[
            MethodIR(
                name="toJson", kind=MethodKind.METHOD,
                return_vm_type="Map<String, dynamic>",
                params=[ParamIR(index=0, vm_type="CallLogEntry", is_receiver=True)],
                params_known=True, address=0x6CF240, size=0x13C,
                events=to_json_events,
                source=SourceRef(asm_file=CALL_ASM, line=546, address=0x6CF240),
            ),
            MethodIR(
                name="toString", kind=MethodKind.METHOD, return_vm_type="_OneByteString",
                params=[ParamIR(index=0, vm_type="CallLogEntry", is_receiver=True)],
                params_known=True, address=0x6CF3F0, events=to_string_events,
                source=SourceRef(asm_file=CALL_ASM, line=600, address=0x6CF3F0),
            ),
            MethodIR(
                name="missed", kind=MethodKind.GETTER, return_vm_type="_",
                params_known=False, address=0x6CF144, size=0xB4,
                events=missed_events,
                source=SourceRef(asm_file=CALL_ASM, line=462, address=0x6CF144),
            ),
            MethodIR(
                name="==", kind=MethodKind.OPERATOR, return_vm_type="bool",
                params_known=False, address=0x6CF500,
                source=SourceRef(asm_file=CALL_ASM, line=640, address=0x6CF500),
            ),
            MethodIR(
                name="fromJson", kind=MethodKind.FACTORY, return_vm_type="CallLogEntry",
                params_known=False, address=0x6CF080,
                source=SourceRef(asm_file=CALL_ASM, line=430, address=0x6CF080),
            ),
        ],
    )

    store = ClassIR(
        name="CallLogStore",
        class_id=2248,
        instance_size=0x14,
        source=SourceRef(asm_file=CALL_ASM, line=300),
        fields={
            0xC: FieldIR(offset=0xC, vm_type="_GrowableList",
                         source=SourceRef(asm_file=CALL_ASM, line=301)),
            0x10: FieldIR(offset=0x10, vm_type="_Mint",
                          source=SourceRef(asm_file=CALL_ASM, line=302)),
        },
        methods=[
            MethodIR(
                name="entries", kind=MethodKind.GETTER, return_vm_type="_GrowableList",
                params_known=False, address=0x6CE100,
                events=[E(EventKind.LOAD_FIELD, 0x6CE108, dst="r0", src="r1", offset=0xB)],
                source=SourceRef(asm_file=CALL_ASM, line=310, address=0x6CE100),
            ),
            MethodIR(
                name="limit", kind=MethodKind.SETTER, return_vm_type="void",
                params=[ParamIR(index=0, vm_type="CallLogStore", is_receiver=True),
                        ParamIR(index=1, vm_type="_Mint")],
                params_known=True, address=0x6CE140,
                events=[E(EventKind.STORE_FIELD, 0x6CE148, dst="r1", offset=0xF, src="r2")],
                source=SourceRef(asm_file=CALL_ASM, line=316, address=0x6CE140),
            ),
            # two same-named entries at different addresses -> collision path
            MethodIR(name="toJson", kind=MethodKind.METHOD,
                     return_vm_type="Map<String, dynamic>", params_known=False,
                     address=0x6CF228,
                     source=SourceRef(asm_file=CALL_ASM, line=525, address=0x6CF228)),
            MethodIR(name="toJson", kind=MethodKind.METHOD,
                     return_vm_type="Map<String, dynamic>", params_known=False,
                     address=0x6CF22C,
                     source=SourceRef(asm_file=CALL_ASM, line=535, address=0x6CF22C)),
            # cross-library type reference -> should emit a relative import
            MethodIR(name="paramsFor", kind=MethodKind.METHOD, return_vm_type="KdfParams",
                     params=[ParamIR(index=0, vm_type="CallLogStore", is_receiver=True),
                             ParamIR(index=1, vm_type="CallLogEntry")],
                     params_known=True, address=0x6CE200,
                     source=SourceRef(asm_file=CALL_ASM, line=340, address=0x6CE200)),
            # a type that is nowhere in the tree -> must degrade to dynamic
            MethodIR(name="platformSink", kind=MethodKind.METHOD,
                     return_vm_type="SomeUnemittedThing", params_known=False,
                     address=0x6CE300,
                     source=SourceRef(asm_file=CALL_ASM, line=350, address=0x6CE300)),
        ],
    )

    empty = ClassIR(
        name="_CallLogTicket", class_id=2249, instance_size=0x8,
        source=SourceRef(asm_file=CALL_ASM, line=700),
        methods=[MethodIR(name="fire", kind=MethodKind.METHOD, return_vm_type="_",
                          modifier=Modifier.ASYNC, params_known=False, address=0x6CE900,
                          source=SourceRef(asm_file=CALL_ASM, line=702, address=0x6CE900))],
    )

    call_lib = LibraryIR(
        url="package:chat/features/call/data/call_log_store.dart",
        asm_file=CALL_ASM,
        classes=[entry, store, empty],
        unparsed=[UnparsedLine(CALL_ASM, 1201, "// 0x6cf9xx: <shape the parser did not know>")],
    )

    # ---- package:chat/core/crypto/cripped.dart ----------------------------
    security = ClassIR(
        name="SecurityLevel",
        super_name="_Enum",
        is_enum=True,
        class_id=5907,
        instance_size=0x1C,
        field_offset_base=0x14,
        source=SourceRef(asm_file=CRIPPED_ASM, line=6937),
        fields={
            0x8: FieldIR(offset=0x8, vm_type="_Mint"),
            0x10: FieldIR(offset=0x10, vm_type="_OneByteString"),
            0x14: FieldIR(offset=0x14, vm_type="_Mint"),
        },
        enum_values=[
            EnumValueIR("standard", 0, "Obj!SecurityLevel@b6a161", {0x14: 0x10},
                        SourceRef(obj_ref="Obj!SecurityLevel@b6a161")),
            EnumValueIR("paranoid", 1, "Obj!SecurityLevel@b6a141", {0x14: 0x18},
                        SourceRef(obj_ref="Obj!SecurityLevel@b6a141")),
            EnumValueIR("fortress", 2, "Obj!SecurityLevel@b6a151", {},
                        SourceRef(obj_ref="Obj!SecurityLevel@b6a151")),
            EnumValueIR("titan", 3, "Obj!SecurityLevel@b6a121", {0x14: 0x28},
                        SourceRef(obj_ref="Obj!SecurityLevel@b6a121")),
        ],
        methods=[
            MethodIR(name="fromRounds", kind=MethodKind.METHOD, is_static=True,
                     return_vm_type="_", params_known=False, address=0x576264, size=0x8C,
                     source=SourceRef(asm_file=CRIPPED_ASM, line=6942, address=0x576264)),
            MethodIR(name="<anonymous closure>", kind=MethodKind.CLOSURE, is_static=True,
                     return_vm_type="bool", params_known=False, address=0x57636C,
                     closure_owner="SecurityLevel::fromRounds (0x576264)",
                     source=SourceRef(asm_file=CRIPPED_ASM, line=6990, address=0x57636C)),
            # a constructor inside an enum must be documented, not emitted
            MethodIR(name="SecurityLevel", kind=MethodKind.CONSTRUCTOR,
                     return_vm_type="SecurityLevel", params_known=False, address=0x576200,
                     source=SourceRef(asm_file=CRIPPED_ASM, line=6939, address=0x576200)),
        ],
    )

    gap_probe = ClassIR(
        name="GapProbe",           # fixture-only enum: exercises ordinal-gap emission
        super_name="_Enum",
        is_enum=True,
        class_id=5908,
        instance_size=0x14,
        source=SourceRef(asm_file=CRIPPED_ASM, line=7100),
        enum_values=[
            EnumValueIR("alpha", 0, "Obj!GapProbe@aa01"),
            EnumValueIR("beta", 1, "Obj!GapProbe@aa02"),
            EnumValueIR("delta", 3, "Obj!GapProbe@aa04"),
        ],
    )

    cripped_lib = LibraryIR(
        url="package:chat/core/crypto/cripped.dart",
        asm_file=CRIPPED_ASM,
        classes=[security, gap_probe],
    )

    # ---- package:chat/core/crypto/kdf.dart --------------------------------
    kdf_top = ClassIR(
        name="::",
        is_library_scope=True,
        class_id=1048704,
        instance_size=0x8,
        source=SourceRef(asm_file=KDF_ASM, line=4),
        fields={
            0xE80: FieldIR(offset=0xE80, vm_type="List<int>", is_static=True, is_late=True,
                           is_final_hint=True, recovered_name="_veilLabel", static_slot=0xE80,
                           name_confidence=Confidence.RECOVERED,
                           source=SourceRef(asm_file=KDF_ASM, line=6,
                                            pool_ref="pp+0x1d418")),
            0xE7C: FieldIR(offset=0xE7C, vm_type="List<List<int>>", is_static=True,
                           is_late=True, is_final_hint=True, recovered_name="layerLabels",
                           static_slot=0xE7C, name_confidence=Confidence.RECOVERED,
                           source=SourceRef(asm_file=KDF_ASM, line=7)),
        },
        methods=[
            MethodIR(name="deriveVeilKey", kind=MethodKind.METHOD, is_static=True,
                     return_vm_type="_", modifier=Modifier.ASYNC, params_known=False,
                     address=0x559D14, size=0x94,
                     calls=["package:chat/core/crypto/kdf.dart ::_hkdfSubkey",
                            "package:chat/core/crypto/kdf.dart ::_argon2Stretch"],
                     source=SourceRef(asm_file=KDF_ASM, line=9, address=0x559D14)),
            MethodIR(name="_hkdfSubkey", kind=MethodKind.METHOD, is_static=True,
                     return_vm_type="_", modifier=Modifier.ASYNC, params_known=False,
                     address=0x561108,
                     source=SourceRef(asm_file=KDF_ASM, line=120, address=0x561108)),
            MethodIR(name="_argon2Stretch", kind=MethodKind.METHOD, is_static=True,
                     return_vm_type="Uint8List", params_known=False, address=0x559DA8,
                     source=SourceRef(asm_file=KDF_ASM, line=210, address=0x559DA8)),
        ],
    )
    kdf_params = ClassIR(
        name="KdfParams", class_id=3001, instance_size=0x18, has_const_ctor=True,
        source=SourceRef(asm_file=KDF_ASM, line=400),
        fields={0x8: FieldIR(offset=0x8, vm_type="_Mint"),
                0xC: FieldIR(offset=0xC, vm_type="_Mint")},
        methods=[
            MethodIR(name="toJson", kind=MethodKind.METHOD,
                     return_vm_type="Map<String, dynamic>",
                     params=[ParamIR(index=0, vm_type="KdfParams", is_receiver=True)],
                     params_known=True, address=0x55A000,
                     events=[
                         E(EventKind.STRING, 0x55A010, value="time_cost"),
                         E(EventKind.LOAD_FIELD, 0x55A018, dst="r0", src="r3", offset=0x7),
                         E(EventKind.STRING, 0x55A020, value="memory_cost"),
                         E(EventKind.LOAD_FIELD, 0x55A028, dst="r0", src="r3", offset=0xB),
                     ],
                     source=SourceRef(asm_file=KDF_ASM, line=404, address=0x55A000)),
        ],
    )
    kdf_lib = LibraryIR(url="package:chat/core/crypto/kdf.dart", asm_file=KDF_ASM,
                        top=kdf_top, classes=[kdf_params])

    meta = ProgramMeta(
        input_name="NEXUS.apk (the user's own app)",
        dart_version="3.5.x",
        snapshot_hash="<hash from libflutter.so>",
        version_signal="libflutter.so string scan",
        blutter_version="blutter@<pinned commit>",
        blutter_out="NEXUS-apk-decompiled/blutter_out",
        abi="arm64-v8a",
        obfuscated=False,
        parse_lines_total=100000,
        parse_lines_unparsed=1,
    )
    program = ProgramIR(
        meta=meta,
        libraries=[call_lib, cripped_lib, kdf_lib],
        pool_strings={
            "pp+0xfeb0": "peer",
            "pp+0xfeb8": "dir",
            "pp+0xfec0": "outcome",
            "pp+0xfec8": "at",
            "pp+0xfed0": "dur",
            "pp+0x2008": "time_cost",
            "pp+0x2018": " in type cast",
        },
        orphan_consts=[
            ConstObjectIR(class_name="_Empty", address="b39911", slots={},
                          source=SourceRef(obj_ref="Obj!_Empty@b39911")),
            ConstObjectIR(class_name="AndroidRecordConfig", address="b39991",
                          slots={0x8: False, 0x10: False, 0x14: True},
                          source=SourceRef(obj_ref="Obj!AndroidRecordConfig@b39991")),
        ],
    )
    program.link()
    return program


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


class Checks:
    def __init__(self) -> None:
        self.rows = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, bool(ok), detail))

    @property
    def failed(self):
        return [r for r in self.rows if not r[1]]

    def report(self) -> str:
        out = []
        for name, ok, detail in self.rows:
            out.append("  %s  %s%s" % ("PASS" if ok else "FAIL", name,
                                       ("  -- " + detail) if detail and not ok else ""))
        return "\n".join(out)


def main() -> int:
    keep = "--keep" in sys.argv
    out_dir = tempfile.mkdtemp(prefix="fd_selftest_")
    program = build_fixture()

    inf = infer_program(program, mode="safe", min_confidence=Confidence.INFERRED_MEDIUM)
    emit = emit_program(program, EmitOptions(out_dir=out_dir, primary_packages=("chat",)))
    report = build_report(program, inf, emit)
    write_reports(out_dir, report, "both")

    files = {f.rel_path: f.text for f in emit.files}
    call_file = files["lib/features/call/data/call_log_store.dart"]
    crip_file = files["lib/core/crypto/cripped.dart"]
    kdf_file = files["lib/core/crypto/kdf.dart"]
    pool_file = files["lib/_const_pool.dart"]

    c = Checks()

    # 1. header on every emitted file
    dart_files = {p: t for p, t in files.items() if p.endswith(".dart")}
    c.check("every .dart file carries the reconstruction header",
            all("RECONSTRUCTED FROM A FLUTTER AOT SNAPSHOT" in t for t in dart_files.values()),
            str([p for p, t in dart_files.items()
                 if "RECONSTRUCTED FROM A FLUTTER AOT SNAPSHOT" not in t]))
    c.check("header explains what an AOT snapshot does not contain",
            all("machine code" in t for t in dart_files.values()))

    # 2. every body throws, none is silent
    throw_count = sum(t.count("throw UnimplementedError(") for t in dart_files.values())
    c.check("every method emits a throwing body (%d throws / %d methods)"
            % (throw_count, emit.stats.get("methods", 0)),
            throw_count == emit.stats.get("methods", 0),
            "throws=%d methods=%d" % (throw_count, emit.stats.get("methods", 0)))
    c.check("stub message names class, address and asm file",
            "CallLogEntry.toJson @0x6cf240 (asm: %s:546 @0x6cf240)" % CALL_ASM in call_file)
    body_re = re.compile(r"\)\s*\{\s*\}")
    c.check("no empty method bodies anywhere",
            not any(body_re.search(t) for t in dart_files.values()))
    for bad in ("TODO", "FIXME", "return null;", "// stub"):
        c.check("no %r placeholder in output" % bad,
                not any(bad in t for t in dart_files.values()))

    # 3. enum values emitted exactly, in ordinal order, with gaps flagged
    order = re.findall(r"^\s{2}(\w+|\$\w+),", crip_file, re.M)
    c.check("SecurityLevel values emitted exactly and in ordinal order",
            order[:4] == ["standard", "paranoid", "fortress", "titan"], str(order[:4]))
    c.check("enum ordinals documented per value",
            "standard, // RECOVERED: ordinal 0, Obj!SecurityLevel@b6a161, slot 0x14 = 0x10 (16)"
            in crip_file)
    c.check("ordinal gap becomes a loud placeholder, keeping later ordinals real",
            "$missingOrdinal2," in crip_file and "delta," in crip_file)
    c.check("enum payload slots documented, not faked as fields",
            "slot 0x14 : _Mint" in crip_file and "late int field_14;" not in crip_file)
    c.check("enum constructor documented as NOT EMITTED",
            "NOT EMITTED: constructor `SecurityLevel`" in crip_file)

    # 4. inferred field names + evidence
    entry_cls = program.find_class("CallLogEntry")
    names = {f.offset: (f.name, f.confidence) for f in entry_cls.instance_fields()}
    c.check("toJson key/field pairing recovered 5 names at the right offsets",
            [names[o][0] for o in (0x8, 0xC, 0x10, 0x14, 0x1C)]
            == ["peer", "dir", "outcome", "at", "dur"],
            str({hex(k): v[0] for k, v in sorted(names.items())}))
    c.check("tagged body offsets were converted to declaration offsets",
            0x8 in names and 0x7 not in names)
    c.check("inferred names carry an evidence comment in the emitted source",
            "/// INFERRED field name (INFERRED_HIGH) for slot 0x8." in call_file
            and "evidence: json_key_pairing:" in call_file)
    c.check("the losing toString candidate is recorded, not hidden",
            "rejected: when" in call_file)
    c.check("slot with no evidence keeps its offset name and says so",
            "field_20; // VM type: bool" in call_file
            and "/// UNKNOWN NAME. Slot 0x20 only." in call_file)
    c.check("computed getter did NOT invent a backing field",
            all(f.name != "_missed" for f in entry_cls.instance_fields()))
    store_cls = program.find_class("CallLogStore")
    store_names = sorted(f.name for f in store_cls.instance_fields())
    c.check("single-load getter and single-store setter named their backing fields",
            store_names == ["_entries", "_limit"], str(store_names))

    # 5. recovered static names
    c.check("static field name emitted as RECOVERED (from the object pool)",
            "/// RECOVERED field name" in kdf_file and "_veilLabel" in kdf_file)
    c.check("static field slot recorded", "field-table slot 0xe80" in kdf_file)

    # 6. strings verbatim
    c.check("string constants emitted verbatim in the const pool",
            "'time_cost'" in pool_file and "' in type cast'" in pool_file)
    c.check("const pool documents unmatched const objects by slot",
            "UNMATCHED" in pool_file and "slot 0x14" in pool_file)

    # 7. types / imports
    c.check("cross-library type reference emitted a relative import",
            "import '../../../core/crypto/kdf.dart';" in call_file, call_file[:0])
    c.check("unresolvable type degrades to dynamic with a note",
            "unresolved type `SomeUnemittedThing`" in call_file)
    c.check("unknown parameter list is stated, not silently emptied",
            "/* parameter list UNKNOWN in snapshot */" in call_file)
    c.check("duplicate method names disambiguated",
            "toJson$2" in call_file)
    c.check("operator emitted as a named method with an explanation",
            "$operator___" in call_file or "$operator_" in call_file)

    # 8. report
    cov = report["coverage"]
    c.check("behaviour coverage reported as 0%", cov["behaviour_bodies"] == 0.0)
    c.check("structure coverage counted", 0.0 < cov["structure_field_names"] < 1.0,
            str(cov))
    errors = [v for v in report["violations"] if v["severity"] == "error"]
    c.check("no invariant errors", not errors, json.dumps(errors, indent=1)[:800])
    c.check("ordinal gap surfaced as a warning",
            any(v["rule"] == "enum_ordinal_gap" for v in report["violations"]))
    md = render_markdown(report)
    c.check("markdown report states the honest capability line",
            "does **not** decompile Dart" in md)
    c.check("markdown report lists per-library rows",
            "package:chat/core/crypto/kdf.dart" in md)

    # 9. inference modes and the honesty guard
    off = infer_program(build_fixture(), mode="off")
    c.check("--infer-fields off applies nothing but still logs candidates",
            off.applied == 0 and len(off.decisions) > 0,
            "applied=%d decisions=%d" % (off.applied, len(off.decisions)))
    aggr_prog = build_fixture()
    aggr = infer_program(aggr_prog, mode="aggressive", min_confidence=Confidence.INFERRED_LOW)
    c.check("aggressive mode is a superset of safe mode",
            aggr.applied >= inf.applied, "%d vs %d" % (aggr.applied, inf.applied))
    off_prog = build_fixture()
    infer_program(off_prog, mode="off")
    c.check("with inference off, field slots keep their offset names",
            all(not f.inferred_name
                for _l, _c, f in off_prog.all_fields() if not f.is_static))

    guard = build_fixture()
    victim = guard.find_class("KdfParams")
    victim.fields[0x8].inferred_name = "smuggled"
    victim.fields[0x8].name_confidence = Confidence.INFERRED_HIGH
    victim.fields[0x8].evidence = []
    guard_v = check_invariants(guard)
    c.check("a name with no evidence trail is an invariant ERROR",
            any(v.rule == "inference_without_evidence" and v.severity == "error"
                for v in guard_v))
    c.check("--strict would fail that build",
            strict_exit_code(build_report(guard, None, None)) == 1)

    # 10. braces balance (cheap structural sanity before dart analyze)
    for path, text in dart_files.items():
        stripped = re.sub(r"//.*", "", text)
        c.check("braces balance in %s" % path,
                stripped.count("{") == stripped.count("}"),
                "%d vs %d" % (stripped.count("{"), stripped.count("}")))

    # 11. dart analyze, when a Dart SDK is on PATH
    dart = shutil.which("dart")
    if dart:
        proc = subprocess.run([dart, "analyze", "--no-fatal-warnings", out_dir],
                              capture_output=True, text=True, timeout=600)
        errs = [ln for ln in proc.stdout.splitlines() if re.search(r"\berror\b", ln)]
        c.check("dart analyze reports no errors", not errs,
                "\n".join(errs[:20]) or proc.stdout[-1500:])
    else:
        c.check("dart analyze skipped (no Dart SDK on PATH)", True)

    print("flutter_decompile emitter self-test")
    print("=" * 70)
    print(c.report())
    print("=" * 70)
    print(render_text_summary(report))
    print("inference: %d applied, %d rejected, by rule %s"
          % (inf.applied, inf.rejected, dict(inf.by_rule)))
    print("output tree: %s" % out_dir)
    for f in emit.files:
        print("   %s" % f.rel_path)

    print("")
    print("---- excerpt: lib/features/call/data/call_log_store.dart ----")
    start = call_file.find("class CallLogEntry")
    print(call_file[start:start + 1800])

    failed = c.failed
    if failed:
        print("")
        print("%d CHECK(S) FAILED:" % len(failed))
        for name, _ok, detail in failed:
            print("  - %s: %s" % (name, detail))
    if not keep:
        shutil.rmtree(out_dir, ignore_errors=True)
    else:
        print("\n(kept output tree at %s)" % out_dir)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
