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


# --- Blutter support detection ------------------------------------------- #

def test_dart_version_tuple_parses_common_forms():
    assert apk.dart_version_tuple("3.13.2") == (3, 13, 2)
    assert apk.dart_version_tuple("3.8") == (3, 8, 0)
    assert apk.dart_version_tuple("unknown") is None
    assert apk.dart_version_tuple(None) is None


def test_combined_layout_triggers_the_support_warning():
    msg = apk.blutter_support_warning("combined", "3.13.2")
    assert msg is not None and "3.13.2" in msg


def test_new_dart_version_triggers_even_with_split_layout():
    assert apk.blutter_support_warning("split", "3.99.0") is not None


def test_supported_version_and_split_layout_is_fine():
    assert apk.blutter_support_warning("split", "3.5.0") is None


def test_unknown_version_split_layout_is_not_flagged():
    # No positive signal either way -> do not cry wolf.
    assert apk.blutter_support_warning("split", None) is None
