"""Get the toolchain onto this machine, or say precisely what is missing.

`flutter_decompile` drives Blutter, and Blutter in turn builds a Dart VM
matching the snapshot under analysis. That is a real C++ build with real
prerequisites, and the usual failure mode is discovering the third missing one
forty minutes in. So everything is checked up front, and what can be fetched
automatically is fetched automatically.

Nothing here is hardcoded to one machine: every tool is looked up on PATH, and
the install advice is chosen from the running platform.
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

BLUTTER_URL = "https://github.com/worawit/blutter"

# A clone that fetched but was killed part-way looks like a directory that
# exists and has a .git in it, which is exactly the shape `git clone` refuses
# to write into. These are the top-level entries a usable Blutter checkout
# has; anything less and the clone is not finished.
BLUTTER_REQUIRED_ENTRIES = ("blutter.py", "blutter", "scripts")

# A clone of Blutter is a few megabytes. Fifteen minutes means the connection
# is hung, not slow, and hanging forever with no output is the worst outcome.
CLONE_TIMEOUT_SECONDS = 900

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _remove_tree(path: str) -> None:
    """rmtree that survives a git checkout on Windows.

    Objects under .git are marked read-only, and rmtree on Windows fails on
    read-only files instead of deleting them - so a broken clone would be
    undeletable and the tool would be wedged for good.
    """
    def _force(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force)
    else:
        shutil.rmtree(path, onerror=_force)


def _version_of(exe: str, *args: str) -> str:
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True,
                             timeout=20, encoding="utf-8",
                             errors="replace").stdout
        return out.strip().splitlines()[0] if out.strip() else ""
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Prerequisites
# --------------------------------------------------------------------------- #

def _compiler_hint() -> str:
    if IS_WINDOWS:
        return ("Visual Studio 2022 Build Tools with the C++ workload:\n"
                "        winget install Microsoft.VisualStudio.2022.BuildTools\n"
                "      then in the installer tick 'Desktop development with C++'.")
    if IS_MAC:
        return "Xcode command line tools:  xcode-select --install"
    return ("A C++ toolchain:\n"
            "        Debian/Ubuntu:  sudo apt install build-essential\n"
            "        Fedora:         sudo dnf groupinstall 'Development Tools'\n"
            "        Arch:           sudo pacman -S base-devel")


def _install_hint(tool: str) -> str:
    if tool == "git":
        if IS_WINDOWS:
            return "winget install Git.Git"
        if IS_MAC:
            return "brew install git   (or: xcode-select --install)"
        return "sudo apt install git   (or your distro's equivalent)"
    if tool == "cmake":
        if IS_WINDOWS:
            return "winget install Kitware.CMake"
        if IS_MAC:
            return "brew install cmake"
        return "sudo apt install cmake"
    if tool == "ninja":
        if IS_WINDOWS:
            return "winget install Ninja-build.Ninja   (or: pip install ninja)"
        if IS_MAC:
            return "brew install ninja   (or: pip install ninja)"
        return "sudo apt install ninja-build   (or: pip install ninja)"
    if tool == "compiler":
        return _compiler_hint()
    return ""


def check_prerequisites() -> Tuple[bool, List[Dict[str, object]]]:
    """(everything_present, per-tool detail).

    Checked before any long-running work, because the alternative is finding
    out about the third missing prerequisite forty minutes into a Dart VM
    build.

    Every entry carries ``build_only``: true means the tool is needed to build
    the Dart VM and Blutter, and not needed at all when an existing Blutter
    output is being adopted. The caller decides whether a MISS is fatal.
    """
    checks: List[Dict[str, object]] = []

    # ninja is not optional: blutter.py configures with `cmake -GNinja` and
    # then shells out to `ninja`, so without it the build dies at configure
    # time - which is exactly the forty-minutes-in surprise this exists to
    # prevent.
    for tool, args in (("git", ("--version",)),
                       ("cmake", ("--version",)),
                       ("ninja", ("--version",))):
        path = _which(tool)
        checks.append({
            "tool": tool,
            "found": bool(path),
            "path": path or "",
            "version": _version_of(path, *args) if path else "",
            "hint": "" if path else _install_hint(tool),
            "build_only": True,
        })

    # A C++ compiler. On Windows the compiler normally is NOT on PATH outside a
    # developer prompt, so absence there is inconclusive - blutter_driver finds
    # MSVC through vswhere instead, and that is the authoritative check.
    if IS_WINDOWS:
        try:
            from . import blutter_driver as bd
            vcvars = bd.find_vcvars()
        except Exception:
            vcvars = None
        checks.append({
            "tool": "compiler (MSVC)",
            "found": bool(vcvars),
            "path": vcvars or "",
            "version": "",
            "hint": "" if vcvars else _install_hint("compiler"),
            "build_only": True,
        })
    else:
        cc = _which("clang++") or _which("g++")
        checks.append({
            "tool": "compiler (clang++/g++)",
            "found": bool(cc),
            "path": cc or "",
            "version": _version_of(cc, "--version") if cc else "",
            "hint": "" if cc else _install_hint("compiler"),
            "build_only": True,
        })

    checks.append({
        "tool": "python",
        "found": True,
        "path": sys.executable,
        "version": platform.python_version(),
        "hint": "",
        "build_only": False,
    })

    return all(c["found"] for c in checks), checks


def render_prerequisites(checks: List[Dict[str, object]]) -> str:
    lines = []
    for c in checks:
        mark = "ok  " if c["found"] else "MISS"
        detail = c["version"] or c["path"] or ""
        lines.append(f"  [{mark}] {str(c['tool']):<22} {detail}")
        if not c["found"] and c["hint"]:
            lines.append(f"         install with: {c['hint']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Blutter
# --------------------------------------------------------------------------- #

def default_blutter_dir() -> str:
    """Where we keep our own clone when the user has not supplied one.

    Under the user's cache rather than the current directory, so running the
    tool from three different folders does not produce three multi-gigabyte
    Dart VM builds.
    """
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif IS_MAC:
        base = os.path.join(os.path.expanduser("~"), "Library", "Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache")
    return os.path.join(base, "flutter_decompile", "blutter")


def missing_clone_parts(root: str) -> List[str]:
    """Which top-level pieces of a Blutter checkout are absent.

    Empty means the clone is usable. A non-empty list means the directory is
    there but the checkout is not finished - an interrupted clone, a disk that
    filled up, or someone's half-copied folder.
    """
    missing: List[str] = []
    for entry in BLUTTER_REQUIRED_ENTRIES:
        path = os.path.join(root, entry)
        if entry.endswith(".py"):
            if not os.path.isfile(path):
                missing.append(entry)
        elif not os.path.isdir(path):
            missing.append(entry + "/")
    return missing


def ensure_blutter(hint: Optional[str] = None,
                   auto_download: bool = True,
                   log=print) -> str:
    """Return a path to blutter.py, cloning Blutter if we have to.

    Order: an explicit hint, then anything already on this machine, then our
    own cache, then a fresh clone. Raises RuntimeError with something
    actionable rather than a stack trace.
    """
    from . import blutter_driver as bd

    found = bd.find_blutter(hint)
    if hint and not found:
        # Silently ignoring a --blutter the user typed and cloning our own
        # copy instead is the worst thing we could do here: they would wait
        # out a whole build against a Blutter they did not choose.
        raise RuntimeError(
            f"--blutter {hint} does not point at a Blutter checkout.\n"
            "Give the path to blutter.py itself, or to the directory that "
            "contains it.\nDrop --blutter to let this fetch its own copy.")

    if found:
        gaps = missing_clone_parts(os.path.dirname(found))
        if gaps:
            raise RuntimeError(
                f"The Blutter checkout at {os.path.dirname(found)} is "
                f"incomplete - missing {', '.join(gaps)}.\n"
                "That is usually a clone that was interrupted. Delete the "
                "directory and re-clone it:\n"
                f"  git clone {BLUTTER_URL} \"{os.path.dirname(found)}\"")
        log(f"[blutter] using {found}")
        return found

    target = default_blutter_dir()
    candidate = os.path.join(target, "blutter.py")

    if os.path.isdir(target):
        gaps = missing_clone_parts(target)
        if not gaps:
            log(f"[blutter] using cached clone at {target}")
            return candidate
        # This directory is ours, under the user's cache, so a half-finished
        # one is ours to throw away - and we must, because `git clone` refuses
        # to write into a non-empty directory and would fail with a message
        # about that instead of about the real problem.
        if not auto_download:
            raise RuntimeError(
                f"The cached Blutter clone at {target} is incomplete "
                f"(missing {', '.join(gaps)}) and --no-download was given.\n"
                f"Delete it and clone it yourself:\n"
                f"  git clone {BLUTTER_URL} \"{target}\"")
        log(f"[blutter] the cached clone at {target} is incomplete "
            f"(missing {', '.join(gaps)}) - removing it and fetching again")
        try:
            _remove_tree(target)
        except OSError as e:
            raise RuntimeError(
                f"Could not remove the broken Blutter clone at {target}: {e}\n"
                "Delete that directory by hand and run this again.") from e

    if not auto_download:
        raise RuntimeError(
            "Blutter not found and --no-download was given.\n"
            f"Clone it yourself:  git clone {BLUTTER_URL} \"{target}\"\n"
            "or point at an existing copy with --blutter <path to blutter.py>")

    if not _which("git"):
        raise RuntimeError(
            "Blutter is not present and git is not installed, so it cannot be "
            f"fetched.\nInstall git ({_install_hint('git')}) or clone Blutter "
            f"manually:\n  git clone {BLUTTER_URL} \"{target}\"")

    log(f"[blutter] not found - cloning {BLUTTER_URL}")
    log(f"[blutter] into {target}")
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
    except OSError as e:
        raise RuntimeError(
            f"Cannot create the Blutter cache directory {target}: {e}\n"
            "Point at an existing checkout with --blutter <path to blutter.py> "
            "instead.") from e

    # git's own progress and its own error text go straight to the terminal:
    # "Could not resolve host" or "407 Proxy Authentication Required" says
    # more about the network than anything we could infer from an exit code.
    try:
        result = subprocess.run(["git", "clone", "--depth", "1",
                                 BLUTTER_URL, target],
                                timeout=CLONE_TIMEOUT_SECONDS)
    except KeyboardInterrupt:
        _cleanup_partial_clone(target, log)
        raise
    except subprocess.TimeoutExpired:
        _cleanup_partial_clone(target, log)
        raise RuntimeError(
            f"The clone of Blutter got no further after "
            f"{CLONE_TIMEOUT_SECONDS // 60} minutes and was stopped.\n"
            f"{_network_advice(target)}")
    except OSError as e:
        raise RuntimeError(
            f"Could not run git: {e}\n"
            f"Install git ({_install_hint('git')}), or clone Blutter manually:"
            f"\n  git clone {BLUTTER_URL} \"{target}\"") from e

    if result.returncode != 0 or not os.path.exists(candidate):
        _cleanup_partial_clone(target, log)
        raise RuntimeError(
            f"git could not clone Blutter (exit {result.returncode}). git's "
            "own error is printed just above.\n"
            f"{_network_advice(target)}")

    log("[blutter] cloned")
    return candidate


def _cleanup_partial_clone(target: str, log=print) -> None:
    """Leave nothing half-cloned behind for the next run to trip over."""
    if not os.path.isdir(target):
        return
    if not missing_clone_parts(target):
        return
    try:
        _remove_tree(target)
        log(f"[blutter] removed the partial clone at {target}")
    except OSError:
        log(f"[blutter] could not remove the partial clone at {target} - "
            "delete it by hand before running this again")


def _network_advice(target: str) -> str:
    return ("This is a network failure, not a problem with your APK. The "
            "usual causes are\nno connection, a proxy that needs "
            "http_proxy/https_proxy set (git also reads\n`git config --global "
            "http.proxy`), or a firewall that blocks github.com.\n"
            f"You can also clone it yourself:\n"
            f"  git clone {BLUTTER_URL} \"{target}\"\n"
            "and then pass --blutter with the path to that blutter.py.")


def prepare_blutter(blutter_py: str, log=print) -> int:
    """Apply the build fixes Blutter needs on a current toolchain.

    Both of these are real breakages hit on a clean machine, not theoretical:

      * CMake 4.x removed compatibility with `cmake_minimum_required` below
        3.5, which several of Blutter's vendored builds still declare.
      * One CMakeLists does `string(REPLACE ... ${CMAKE_CXX_FLAGS})` unquoted,
        which fails outright when that variable is empty - as it is on a
        default configure.

    Idempotent: every patched file keeps a .fd-backup, and re-running is a
    no-op once the fixes are in.
    """
    from . import blutter_driver as bd

    root = bd.blutter_root(blutter_py)
    try:
        patches = bd.patch_cmake_compat(root)
    except OSError as e:
        # The patcher reads defensively but has to write; a checkout on a
        # read-only mount or one owned by another user fails here.
        raise RuntimeError(
            f"Could not apply the CMake compatibility fixes to {root}: {e}\n"
            "Blutter's build needs those files edited, so the checkout has to "
            "be writable.\nUse a copy you own, or pass --blutter pointing at "
            "one.") from e
    if patches:
        log(f"[blutter] applied {len(patches)} build fix(es) for a modern CMake")
    return len(patches)
