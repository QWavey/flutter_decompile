"""bootstrap.py -- prerequisite checks and getting Blutter onto the machine.

Nothing here clones anything, runs git, or looks at the real cache directory.
The one test that exercises check_prerequisites for real only asserts the
shape of the answer, so it passes on a machine with no toolchain at all.

bootstrap decides its platform once, at import, into IS_WINDOWS / IS_MAC.
Patching os.name after that changes nothing, so the tests patch those module
constants instead -- and one test asserts they really are derived from
os.name / sys.platform, so the shortcut stays honest.
"""

from __future__ import annotations

import os
import sys

import pytest

from flutter_decompile import blutter_driver as bd
from flutter_decompile import bootstrap


@pytest.fixture
def platform_is(monkeypatch):
    """Pretend to be one of windows / mac / linux."""
    def set_platform(which: str):
        monkeypatch.setattr(bootstrap, "IS_WINDOWS", which == "windows")
        monkeypatch.setattr(bootstrap, "IS_MAC", which == "mac")
    return set_platform


# --------------------------------------------------------------------------- #
# platform detection
# --------------------------------------------------------------------------- #

def test_platform_flags_come_from_os_name_and_sys_platform():
    assert bootstrap.IS_WINDOWS == (os.name == "nt")
    assert bootstrap.IS_MAC == (sys.platform == "darwin")


# --------------------------------------------------------------------------- #
# default_blutter_dir
# --------------------------------------------------------------------------- #

def test_windows_cache_lives_under_localappdata(platform_is, monkeypatch):
    platform_is("windows")
    monkeypatch.setenv("LOCALAPPDATA", os.path.join("X:", "AppData", "Local"))
    path = bootstrap.default_blutter_dir()
    assert path.startswith(os.path.join("X:", "AppData", "Local"))
    assert path.endswith(os.path.join("flutter_decompile", "blutter"))


def test_windows_falls_back_to_home_without_localappdata(platform_is, monkeypatch):
    platform_is("windows")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", "/home/u"))
    assert bootstrap.default_blutter_dir().startswith("/home/u")


def test_mac_cache_lives_under_library_caches(platform_is, monkeypatch):
    platform_is("mac")
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", "/Users/u"))
    path = bootstrap.default_blutter_dir()
    assert os.path.join("Library", "Caches") in path
    assert path.endswith(os.path.join("flutter_decompile", "blutter"))


def test_linux_honours_xdg_cache_home(platform_is, monkeypatch):
    platform_is("linux")
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg")
    assert bootstrap.default_blutter_dir() == os.path.join(
        "/tmp/xdg", "flutter_decompile", "blutter")


def test_linux_falls_back_to_dot_cache(platform_is, monkeypatch):
    platform_is("linux")
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", "/home/u"))
    assert bootstrap.default_blutter_dir() == os.path.join(
        "/home/u", ".cache", "flutter_decompile", "blutter")


def test_the_cache_is_shared_not_per_working_directory(monkeypatch, tmp_path):
    """Three runs from three folders must not build three Dart VMs."""
    first = bootstrap.default_blutter_dir()
    monkeypatch.chdir(str(tmp_path))
    assert bootstrap.default_blutter_dir() == first


# --------------------------------------------------------------------------- #
# prerequisite checks
# --------------------------------------------------------------------------- #

def test_check_prerequisites_reports_every_tool_it_needs():
    ok, checks = bootstrap.check_prerequisites()
    tools = [c["tool"] for c in checks]
    for needed in ("git", "cmake", "ninja", "python"):
        assert needed in tools, "%s is a real prerequisite of the VM build" % needed
    assert any(t.startswith("compiler") for t in tools)
    assert isinstance(ok, bool)
    assert ok == all(c["found"] for c in checks)
    for c in checks:
        assert {"tool", "found", "path", "version", "hint"} <= set(c)


def test_every_check_says_whether_it_is_only_needed_for_the_build():
    """Adopting an existing Blutter output needs none of the build tools, so
    the caller has to be able to tell which MISS is actually fatal."""
    _ok, checks = bootstrap.check_prerequisites()
    for c in checks:
        assert isinstance(c["build_only"], bool)
    by_tool = {c["tool"]: c for c in checks}
    assert by_tool["git"]["build_only"] is True
    assert by_tool["ninja"]["build_only"] is True
    assert by_tool["python"]["build_only"] is False


def test_python_is_always_present_and_reports_the_running_interpreter():
    _ok, checks = bootstrap.check_prerequisites()
    py = [c for c in checks if c["tool"] == "python"][0]
    assert py["found"] is True
    assert py["path"] == sys.executable


def test_a_missing_tool_is_not_found_and_carries_an_install_hint(monkeypatch,
                                                                platform_is):
    platform_is("linux")
    monkeypatch.setattr(bootstrap, "_which", lambda name: None)
    ok, checks = bootstrap.check_prerequisites()
    assert ok is False
    git = [c for c in checks if c["tool"] == "git"][0]
    assert git["found"] is False
    assert "apt install git" in git["hint"]
    compiler = [c for c in checks if c["tool"].startswith("compiler")][0]
    assert compiler["found"] is False
    assert "build-essential" in compiler["hint"]


def test_install_hints_are_platform_specific(platform_is):
    platform_is("windows")
    assert "winget" in bootstrap._install_hint("git")
    assert "Visual Studio" in bootstrap._install_hint("compiler")
    platform_is("mac")
    assert "brew" in bootstrap._install_hint("cmake")
    assert "xcode-select" in bootstrap._install_hint("compiler")
    platform_is("linux")
    assert "apt install cmake" in bootstrap._install_hint("cmake")
    assert "ninja-build" in bootstrap._install_hint("ninja")


def test_an_unknown_tool_has_no_hint_rather_than_a_made_up_one():
    assert bootstrap._install_hint("frobnicator") == ""


def test_render_prerequisites_marks_misses_and_shows_the_fix():
    checks = [
        {"tool": "git", "found": True, "path": "/usr/bin/git",
         "version": "git version 2.44", "hint": ""},
        {"tool": "cmake", "found": False, "path": "", "version": "",
         "hint": "sudo apt install cmake"},
    ]
    text = bootstrap.render_prerequisites(checks)
    assert "[ok  ] git" in text
    assert "[MISS] cmake" in text
    assert "install with: sudo apt install cmake" in text
    assert "git version 2.44" in text


def test_render_prerequisites_of_nothing_is_empty_not_a_crash():
    assert bootstrap.render_prerequisites([]) == ""


def test_version_of_a_missing_binary_is_empty_not_an_exception():
    assert bootstrap._version_of("definitely-not-a-real-binary-xyz", "--version") == ""


# --------------------------------------------------------------------------- #
# ensure_blutter
# --------------------------------------------------------------------------- #

@pytest.fixture
def no_blutter_anywhere(monkeypatch, tmp_path):
    """Nothing on this machine, and an empty cache directory."""
    cache = os.path.join(str(tmp_path), "cache", "blutter")
    monkeypatch.setattr(bd, "find_blutter", lambda hint=None: None)
    monkeypatch.setattr(bootstrap, "default_blutter_dir", lambda: cache)
    return cache


def test_no_download_raises_instead_of_silently_doing_nothing(no_blutter_anywhere):
    """Returning a path that is not there, or None, would push the failure
    into the middle of a Blutter run. It fails here, with the fix in hand."""
    with pytest.raises(RuntimeError) as exc:
        bootstrap.ensure_blutter(auto_download=False, log=lambda *_: None)
    message = str(exc.value)
    assert "--no-download" in message
    assert bootstrap.BLUTTER_URL in message
    assert "--blutter" in message, "the error must name the manual way out"


def test_no_download_never_shells_out(no_blutter_anywhere, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("ensure_blutter ran a subprocess with downloads off")
    monkeypatch.setattr(bootstrap.subprocess, "run", explode)
    with pytest.raises(RuntimeError):
        bootstrap.ensure_blutter(auto_download=False, log=lambda *_: None)


def test_a_missing_git_is_reported_before_any_clone_is_attempted(
        no_blutter_anywhere, monkeypatch):
    monkeypatch.setattr(bootstrap, "_which", lambda name: None)

    def explode(*a, **k):
        raise AssertionError("attempted to clone without git")
    monkeypatch.setattr(bootstrap.subprocess, "run", explode)

    with pytest.raises(RuntimeError) as exc:
        bootstrap.ensure_blutter(auto_download=True, log=lambda *_: None)
    assert "git is not installed" in str(exc.value)


def fake_checkout(root: str) -> str:
    """A directory shaped like a finished Blutter clone. Returns blutter.py."""
    os.makedirs(root, exist_ok=True)
    for entry in bootstrap.BLUTTER_REQUIRED_ENTRIES:
        path = os.path.join(root, entry)
        if entry.endswith(".py"):
            open(path, "w").close()
        else:
            os.makedirs(path, exist_ok=True)
    return os.path.join(root, "blutter.py")


def test_an_existing_checkout_is_used_and_nothing_is_fetched(monkeypatch, tmp_path):
    existing = fake_checkout(os.path.join(str(tmp_path), "blutter"))
    monkeypatch.setattr(bd, "find_blutter", lambda hint=None: existing)
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: pytest.fail(
        "cloned despite an existing checkout"))
    logged = []
    assert bootstrap.ensure_blutter(hint=existing, log=logged.append) == existing
    assert any("using" in line for line in logged)


def test_the_cached_clone_is_preferred_over_a_fresh_one(no_blutter_anywhere,
                                                        monkeypatch):
    cached = fake_checkout(no_blutter_anywhere)
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: pytest.fail(
        "re-cloned over a cached checkout"))
    assert bootstrap.ensure_blutter(log=lambda *_: None) == cached


# --------------------------------------------------------------------------- #
# half-finished checkouts
# --------------------------------------------------------------------------- #

def test_missing_clone_parts_is_empty_for_a_finished_checkout(tmp_path):
    root = os.path.join(str(tmp_path), "blutter")
    fake_checkout(root)
    assert bootstrap.missing_clone_parts(root) == []


def test_missing_clone_parts_names_what_is_absent(tmp_path):
    root = os.path.join(str(tmp_path), "blutter")
    os.makedirs(root)
    open(os.path.join(root, "blutter.py"), "w").close()
    gaps = bootstrap.missing_clone_parts(root)
    assert "blutter/" in gaps and "scripts/" in gaps
    assert "blutter.py" not in gaps


def test_an_interrupted_user_checkout_is_reported_not_used(monkeypatch, tmp_path):
    """A directory with a blutter.py and nothing else would fail deep inside
    the build. It is caught here, where the message can still be useful."""
    root = os.path.join(str(tmp_path), "blutter")
    os.makedirs(root)
    partial = os.path.join(root, "blutter.py")
    open(partial, "w").close()
    monkeypatch.setattr(bd, "find_blutter", lambda hint=None: partial)
    with pytest.raises(RuntimeError) as exc:
        bootstrap.ensure_blutter(log=lambda *_: None)
    assert "incomplete" in str(exc.value)
    assert "git clone" in str(exc.value)


def test_an_incomplete_cache_with_no_download_is_refused_not_deleted(
        no_blutter_anywhere, monkeypatch):
    os.makedirs(no_blutter_anywhere)
    open(os.path.join(no_blutter_anywhere, "blutter.py"), "w").close()
    monkeypatch.setattr(bootstrap, "_remove_tree", lambda p: pytest.fail(
        "deleted a directory with downloads switched off"))
    with pytest.raises(RuntimeError) as exc:
        bootstrap.ensure_blutter(auto_download=False, log=lambda *_: None)
    assert "incomplete" in str(exc.value)
    assert "--no-download" in str(exc.value)


def test_a_blutter_path_the_user_typed_is_never_silently_replaced(monkeypatch,
                                                                  tmp_path):
    """Cloning our own copy behind the user's back would mean they wait out a
    whole build against a Blutter they did not choose."""
    monkeypatch.setattr(bd, "find_blutter", lambda hint=None: None)
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: pytest.fail(
        "cloned instead of reporting a bad --blutter"))
    with pytest.raises(RuntimeError) as exc:
        bootstrap.ensure_blutter(hint=str(tmp_path), auto_download=True,
                                 log=lambda *_: None)
    assert "does not point at a Blutter checkout" in str(exc.value)


def test_a_failed_clone_becomes_an_actionable_error(no_blutter_anywhere,
                                                    monkeypatch):
    class Result:
        returncode = 128

    monkeypatch.setattr(bootstrap, "_which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: Result())
    with pytest.raises(RuntimeError) as exc:
        bootstrap.ensure_blutter(log=lambda *_: None)
    message = str(exc.value)
    assert "exit 128" in message
    assert "git clone" in message
    assert "not a problem with your APK" in message, \
        "a network failure must not be blamed on the input"


def test_a_hung_clone_is_stopped_and_the_partial_copy_removed(
        no_blutter_anywhere, monkeypatch):
    target = no_blutter_anywhere

    def timeout(*_a, **_k):
        os.makedirs(target, exist_ok=True)         # git got part-way
        open(os.path.join(target, "blutter.py"), "w").close()
        raise bootstrap.subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(bootstrap, "_which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(bootstrap.subprocess, "run", timeout)
    with pytest.raises(RuntimeError) as exc:
        bootstrap.ensure_blutter(log=lambda *_: None)
    assert "minutes and was stopped" in str(exc.value)
    assert not os.path.isdir(target), \
        "a partial clone left behind wedges the next run"


def test_git_that_will_not_run_at_all_is_reported_as_such(no_blutter_anywhere,
                                                          monkeypatch):
    monkeypatch.setattr(bootstrap, "_which", lambda name: "/usr/bin/git")

    def boom(*_a, **_k):
        raise OSError(8, "Exec format error")
    monkeypatch.setattr(bootstrap.subprocess, "run", boom)
    with pytest.raises(RuntimeError) as exc:
        bootstrap.ensure_blutter(log=lambda *_: None)
    assert "Could not run git" in str(exc.value)


def test_the_clone_timeout_is_long_enough_to_be_a_hang_not_a_slow_link():
    assert bootstrap.CLONE_TIMEOUT_SECONDS >= 300


def test_blutter_url_is_the_upstream_repository():
    assert bootstrap.BLUTTER_URL == "https://github.com/worawit/blutter"
