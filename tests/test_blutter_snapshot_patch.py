"""The combined-snapshot Blutter patch: additive, idempotent, and only where
the upstream code it targets is present."""

import os

from flutter_decompile import blutter_driver as bd

# Minimal slices of the upstream files carrying the exact lines the patch keys
# on. If upstream reformats these, the patch should skip (not corrupt), which
# these fixtures also check.
ELF_SRC = """\
LibAppInfo ElfHelper::findSnapshots(const uint8_t* elf)
{
\t\t\tconst char* s_first = kVmSnapshotDataAsmSymbol;
\t\t\tconst char* s_last = s_first + strlen(kVmSnapshotDataAsmSymbol) + 1;
\t\t\tif (std::search(strtab, last, s_first, s_last) != last) {
\t\t\t\t// found it
\t\t\t\tdynstr = strtab;
\t\t\t}
\t\telse if (strcmp(name, kIsolateSnapshotInstructionsAsmSymbol) == 0) {
\t\t\tisolate_snapshot_instructions = elf + dynsym->value;
\t\t}
\t}
\tif (vm_snapshot_data == nullptr)
\t\tthrow std::invalid_argument("ELF: Cannot find Dart VM Snapshot Data");
\tif (vm_snapshot_instructions == nullptr)
\t\tthrow std::invalid_argument("ELF: Cannot find Dart VM Snapshot Instructions");
\tif (isolate_snapshot_data == nullptr)
\t\tthrow std::invalid_argument("ELF: Cannot find Dart Isolate Snapshot Data");
\tif (isolate_snapshot_instructions == nullptr)
\t\tthrow std::invalid_argument("ELF: Cannot find Dart Isolate Snapshot Instructions");
}
"""

EXTRACT_SRC = (
    "def extract_snapshot_hash_flags(libapp_file):\n"
    "        dynsym = elf.get_section_by_name('.dynsym')\n"
    "        sym = dynsym.get_symbol_by_name('_kDartVmSnapshotData')[0]\n"
    "        assert sym['st_size'] > 128\n"
)


def _clone(tmp_path):
    root = str(tmp_path)
    src = os.path.join(root, "blutter", "src")
    os.makedirs(src)
    with open(os.path.join(src, "ElfHelper.cpp"), "w", encoding="utf-8", newline="") as fh:
        fh.write(ELF_SRC)
    with open(os.path.join(root, "extract_dart_info.py"), "w", encoding="utf-8", newline="") as fh:
        fh.write(EXTRACT_SRC)
    return root


def test_patch_applies_all_edits(tmp_path):
    root = _clone(tmp_path)
    patches = bd.patch_combined_snapshot(root)
    assert len(patches) == 4

    elf = open(os.path.join(root, "blutter", "src", "ElfHelper.cpp"), encoding="utf-8").read()
    # combined symbols now recognised, mapped to the isolate slots
    assert '"_kDartSnapshotData"' in elf and '"_kDartSnapshotText"' in elf
    assert "isolate_snapshot_data = elf + dynsym->value;" in elf
    # the four-symbol path is untouched
    assert "kIsolateSnapshotInstructionsAsmSymbol" in elf
    # VM-snapshot-null is now tolerated (no VM throw), isolate still required
    assert "Cannot find Dart VM Snapshot Data" not in elf
    assert "Cannot find Dart Isolate/App Snapshot Data" in elf

    extract = open(os.path.join(root, "extract_dart_info.py"), encoding="utf-8").read()
    assert "_kDartSnapshotData" in extract


def test_patch_is_idempotent(tmp_path):
    root = _clone(tmp_path)
    assert len(bd.patch_combined_snapshot(root)) == 4
    assert len(bd.patch_combined_snapshot(root)) == 0     # marker present -> skip


def test_patch_skips_when_upstream_lines_absent(tmp_path):
    root = str(tmp_path)
    src = os.path.join(root, "blutter", "src")
    os.makedirs(src)
    # A refactored upstream that no longer has the target lines: no crash, no edit.
    with open(os.path.join(src, "ElfHelper.cpp"), "w", encoding="utf-8") as fh:
        fh.write("// completely different content\n")
    assert bd.patch_combined_snapshot(root) == []


def test_backup_is_written(tmp_path):
    root = _clone(tmp_path)
    bd.patch_combined_snapshot(root)
    assert os.path.exists(os.path.join(root, "blutter", "src", "ElfHelper.cpp.fd-backup"))
