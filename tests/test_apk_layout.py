"""snapshot_symbol_layout: tell a loadable split snapshot from the combined
layout Blutter cannot read, before paying for a VM build."""

import os

from flutter_decompile import apk


def _write(tmp_path, name, blob):
    p = os.path.join(str(tmp_path), name)
    with open(p, "wb") as fh:
        fh.write(blob)
    return p


def test_split_layout_is_recognised(tmp_path):
    blob = b"\x7fELF" + b"...." + b"_kDartVmSnapshotData\x00_kDartVmSnapshotInstructions\x00"
    assert apk.snapshot_symbol_layout(_write(tmp_path, "libapp.so", blob)) == "split"


def test_combined_layout_is_recognised(tmp_path):
    blob = b"\x7fELF" + b"...." + b"_kDartSnapshotData\x00_kDartSnapshotText\x00"
    assert apk.snapshot_symbol_layout(_write(tmp_path, "libapp.so", blob)) == "combined"


def test_split_wins_when_both_appear(tmp_path):
    # A file that somehow interns both names is treated as loadable.
    blob = b"_kDartSnapshotData\x00_kDartVmSnapshotData\x00"
    assert apk.snapshot_symbol_layout(_write(tmp_path, "libapp.so", blob)) == "split"


def test_unknown_when_no_dart_symbols(tmp_path):
    assert apk.snapshot_symbol_layout(_write(tmp_path, "libapp.so", b"\x7fELFnope")) == "unknown"


def test_missing_file_is_unknown_not_an_error():
    assert apk.snapshot_symbol_layout("/no/such/libapp.so") == "unknown"
