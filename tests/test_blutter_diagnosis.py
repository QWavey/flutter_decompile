"""_diagnose_blutter_failure: name the real cause of a Blutter build failure."""

from flutter_decompile.blutter_driver import _diagnose_blutter_failure as diag


def test_dart_api_drift_is_named_as_unsupported_version():
    out = "DartLoader.cpp(28): error: 'vm_snapshot_data' is not a member of Dart_InitializeParams"
    msg = diag(out, "3.13.2")
    assert msg is not None
    assert "3.13.2" in msg
    assert "port" in msg.lower()
    assert "older, stable" in msg          # actionable guidance
    assert "--check" not in msg            # not the misleading toolchain hint


def test_stub_list_macro_drift_is_recognised():
    msg = diag("DartStub.h(14): OBJECT_STORE_STUB_CODE_LIST(DO) error", "3.14.0")
    assert msg is not None and "Blutter" in msg


def test_localised_compiler_message_still_matches():
    # MSVC emits localised text ("kein Member"); the Dart symbol still anchors it.
    msg = diag("error C2039: kein Member von Dart_InitializeParams", "3.13.2")
    assert msg is not None


def test_unrelated_failure_gets_no_false_diagnosis():
    assert diag("ninja: error: manifest 'build.ninja' not found", "3.5.0") is None
