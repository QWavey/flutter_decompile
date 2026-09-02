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
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

BLUTTER_URL = "https://github.com/worawit/blutter"

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


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
    if tool == "compiler":
        return _compiler_hint()
    return ""


def check_prerequisites() -> Tuple[bool, List[Dict[str, object]]]:
    """(everything_present, per-tool detail).

    Checked before any long-running work, because the alternative is finding
    out about the third missing prerequisite forty minutes into a Dart VM
    build.
    """
    checks: List[Dict[str, object]] = []

    for tool, args in (("git", ("--version",)), ("cmake", ("--version",))):
        path = _which(tool)
        checks.append({
            "tool": tool,
            "found": bool(path),
            "path": path or "",
            "version": _version_of(path, *args) if path else "",
            "hint": "" if path else _install_hint(tool),
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
        })
    else:
        cc = _which("clang++") or _which("g++")
        checks.append({
            "tool": "compiler (clang++/g++)",
            "found": bool(cc),
            "path": cc or "",
            "version": _version_of(cc, "--version") if cc else "",
            "hint": "" if cc else _install_hint("compiler"),
        })

    checks.append({
        "tool": "python",
        "found": True,
        "path": sys.executable,
        "version": platform.python_version(),
        "hint": "",
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
    if found:
        log(f"[blutter] using {found}")
        return found

    target = default_blutter_dir()
    candidate = os.path.join(target, "blutter.py")
    if os.path.exists(candidate):
        log(f"[blutter] using cached clone at {target}")
        return candidate

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
    os.makedirs(os.path.dirname(target), exist_ok=True)
    result = subprocess.run(["git", "clone", "--depth", "1", BLUTTER_URL, target])
    if result.returncode != 0 or not os.path.exists(candidate):
        raise RuntimeError(
            f"git clone of Blutter failed (exit {result.returncode}).\n"
            f"Try manually:  git clone {BLUTTER_URL} \"{target}\"")

    log("[blutter] cloned")
    return candidate


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
    patches = bd.patch_cmake_compat(root)
    if patches:
        log(f"[blutter] applied {len(patches)} build fix(es) for a modern CMake")
    return len(patches)
