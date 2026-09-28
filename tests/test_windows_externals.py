"""ensure_windows_externals: fetch ICU + Capstone before the Dart VM build."""

import os
import types

import pytest

from flutter_decompile import blutter_driver as bd


def _clone(tmp_path, with_externals=False, with_script=True):
    root = str(tmp_path)
    if with_script:
        os.makedirs(os.path.join(root, "scripts"))
        open(os.path.join(root, "scripts", "init_env_win.py"), "w").close()
    if with_externals:
        os.makedirs(os.path.join(root, "external", "icu-windows"))
        os.makedirs(os.path.join(root, "external", "capstone"))
    return root


def test_present_detects_both_dirs(tmp_path):
    root = _clone(tmp_path, with_externals=True)
    assert bd.windows_externals_present(root) is True
    root2 = _clone(tmp_path / "b")
    assert bd.windows_externals_present(root2) is False


def test_skips_on_non_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(bd, "IS_WINDOWS", False)
    assert bd.ensure_windows_externals(_clone(tmp_path), log=lambda *_: None) is False


def test_skips_when_already_present(tmp_path, monkeypatch):
    monkeypatch.setattr(bd, "IS_WINDOWS", True)
    root = _clone(tmp_path, with_externals=True)
    # Must NOT invoke the downloader when both dirs already exist.
    monkeypatch.setattr(bd.subprocess, "run",
                        lambda *a, **k: pytest.fail("should not download"))
    assert bd.ensure_windows_externals(root, log=lambda *_: None) is False


def test_runs_setup_and_verifies(tmp_path, monkeypatch):
    monkeypatch.setattr(bd, "IS_WINDOWS", True)
    root = _clone(tmp_path)

    def fake_run(cmd, **kw):
        # Simulate init_env_win.py creating the dirs.
        os.makedirs(os.path.join(root, "external", "icu-windows"))
        os.makedirs(os.path.join(root, "external", "capstone"))
        return types.SimpleNamespace(returncode=0, stdout="Done", stderr="")

    monkeypatch.setattr(bd.subprocess, "run", fake_run)
    assert bd.ensure_windows_externals(root, log=lambda *_: None) is True
    assert bd.windows_externals_present(root)


def test_raises_a_clear_error_when_setup_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(bd, "IS_WINDOWS", True)
    root = _clone(tmp_path)
    monkeypatch.setattr(bd.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(
                            returncode=1, stdout="", stderr="No module named requests"))
    with pytest.raises(bd.BlutterError) as e:
        bd.ensure_windows_externals(root, log=lambda *_: None)
    assert "requests" in str(e.value)


def test_missing_script_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(bd, "IS_WINDOWS", True)
    root = _clone(tmp_path, with_script=False)
    with pytest.raises(bd.BlutterError):
        bd.ensure_windows_externals(root, log=lambda *_: None)


def test_msvc_preprocessor_patch_adds_the_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(bd, "IS_WINDOWS", True)
    root = str(tmp_path)
    cml = os.path.join(root, "dartsdk", "v9.9.9")
    os.makedirs(cml)
    p = os.path.join(cml, "CMakeLists.txt")
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write("if (MSVC)\n\tset(cc_opts\n\t\t/Oy /GR- /EHs-c-\n\t)\nendif()\n")
    applied = bd.patch_msvc_preprocessor(root)
    assert len(applied) == 1
    text = open(p, encoding="utf-8").read()
    assert "/Zc:preprocessor" in text
    assert bd.patch_msvc_preprocessor(root) == []          # idempotent


def test_msvc_preprocessor_patch_noop_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(bd, "IS_WINDOWS", False)
    assert bd.patch_msvc_preprocessor(str(tmp_path)) == []
