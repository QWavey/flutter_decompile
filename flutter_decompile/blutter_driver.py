"""
flutter_decompile.blutter_driver -- Stage 2.

Locates Blutter, patches the two build breakages that stop it cold on a modern
toolchain, runs it, and validates its output.  Also supports adopting an
existing ``blutter_out/`` and skipping the build entirely (``--blutter-out``),
which is the normal day-2 workflow because the build is slow.

THE TWO BREAKAGES
-----------------

(1) CMake 4.x + `string(REPLACE ... ${CMAKE_CXX_FLAGS})`

    Blutter's CMake (and the Dart runtime CMake it vendors) contains lines of
    the shape:

        string(REPLACE "/GR" "" CMAKE_CXX_FLAGS ${CMAKE_CXX_FLAGS})

    The final argument is an UNQUOTED variable reference.  CMake expands an
    unquoted `${VAR}` to *zero* arguments when VAR is empty.  CMake 4.x no
    longer pre-seeds CMAKE_CXX_FLAGS the way 3.x did, so on a clean configure
    this expands to a 4-argument call and CMake aborts with:

        string sub-command REPLACE requires at least four arguments.

    The fix is to quote it -- `"${CMAKE_CXX_FLAGS}"` always expands to exactly
    one (possibly empty) argument:

        string(REPLACE "/GR" "" CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS}")

    CMake 4.x additionally REMOVED compatibility with
    `cmake_minimum_required(VERSION <3.5)`, so we bump those too.

(2) MSVC environment on Windows

    Blutter compiles the Dart VM from source.  On Windows that needs the MSVC
    toolchain on PATH plus INCLUDE / LIB / LIBPATH set -- i.e. the process must
    run inside a "Developer Command Prompt".  Launching `blutter.py` from a
    plain PowerShell or Git-Bash prompt fails with missing `cl.exe`, or with
    `cannot open include file 'stddef.h'` if only PATH is set.  We locate
    `vcvars64.bat` via `vswhere.exe`, run it, capture the resulting
    environment, and hand that environment to the Blutter subprocess.  We do
    NOT permanently modify the user's environment.
"""

from __future__ import annotations

import collections
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Tuple

IS_WINDOWS = os.name == "nt"

REQUIRED_OUTPUTS = ("asm", "objs.txt", "pp.txt")
OPTIONAL_OUTPUTS = ("blutter_frida.js", "ida_script")

# `string(REPLACE a b out ${VAR})` / `string(APPEND out ${VAR})` with a BARE
# ${...} as the final argument.
RE_CMAKE_BARE_TAIL = re.compile(
    r"(?P<head>\bstring\s*\(\s*(?:REPLACE|APPEND|PREPEND|CONCAT)\b[^()\n]*?\s)"
    r"(?P<var>\$\{[A-Za-z_][A-Za-z0-9_]*\})\s*\)",
    re.IGNORECASE)

RE_CMAKE_MINREQ = re.compile(
    r"(?P<head>\bcmake_minimum_required\s*\(\s*VERSION\s+)(?P<ver>\d+(?:\.\d+)*)",
    re.IGNORECASE)

CMAKE_MIN_FLOOR = (3, 5)


class BlutterError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Locate
# --------------------------------------------------------------------------- #

def find_blutter(hint: Optional[str] = None) -> Optional[str]:
    """Return a path to blutter.py (or a blutter executable), or None."""
    cands: List[str] = []
    if hint:
        cands.append(hint)
    env = os.environ.get("BLUTTER_PATH")
    if env:
        cands.append(env)
    for name in ("blutter.py", "blutter", "blutter.exe"):
        w = shutil.which(name)
        if w:
            cands.append(w)
    home = os.path.expanduser("~")
    for base in (os.getcwd(), home, os.path.join(home, "Desktop"),
                 os.path.join(home, "tools"), "C:/tools", "/opt", "/usr/local/src"):
        cands.append(os.path.join(base, "blutter", "blutter.py"))
        cands.append(os.path.join(base, "Blutter", "blutter.py"))

    for c in cands:
        if not c:
            continue
        c = os.path.abspath(c)
        if os.path.isfile(c):
            return c
        if os.path.isdir(c):
            p = os.path.join(c, "blutter.py")
            if os.path.isfile(p):
                return p
    return None


def blutter_root(blutter_py: str) -> str:
    return os.path.dirname(os.path.abspath(blutter_py))


# --------------------------------------------------------------------------- #
# Breakage (1): CMake patches
# --------------------------------------------------------------------------- #

@dataclass
class Patch:
    path: str
    lineno: int
    before: str
    after: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"file": self.path, "line": self.lineno, "before": self.before,
                "after": self.after, "reason": self.reason}


def _iter_cmake_files(root: str):
    skip = {".git", "build", "out", "__pycache__", "node_modules"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            if fn == "CMakeLists.txt" or fn.endswith(".cmake"):
                yield os.path.join(dirpath, fn)


def patch_cmake_compat(root: str, dry_run: bool = False) -> List[Patch]:
    """Apply both CMake 4.x fixes across ``root``.  Idempotent.

    Returns the list of patches applied (or that would be applied).
    Every patched file gets a ``.fd-backup`` copy the first time it is touched.
    """
    patches: List[Patch] = []
    for path in _iter_cmake_files(root):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue

        changed = False
        for i, line in enumerate(lines):
            orig = line

            # Fix A: quote the bare ${...} tail argument of string(REPLACE ...)
            def _quote(m: "re.Match") -> str:
                return '%s"%s")' % (m.group("head"), m.group("var"))

            new = RE_CMAKE_BARE_TAIL.sub(_quote, line)

            # Fix B: bump cmake_minimum_required below the 4.x floor
            def _bump(m: "re.Match") -> str:
                parts = tuple(int(x) for x in m.group("ver").split("."))
                padded = parts + (0,) * (2 - len(parts))
                if padded[:2] < CMAKE_MIN_FLOOR:
                    return "%s%d.%d" % (m.group("head"), *CMAKE_MIN_FLOOR)
                return m.group(0)

            new = RE_CMAKE_MINREQ.sub(_bump, new)

            if new != orig:
                reason = ("unquoted ${...} as the last argument of string() -- "
                          "expands to zero arguments under CMake 4.x when the "
                          "variable is empty"
                          if RE_CMAKE_BARE_TAIL.search(orig)
                          else "cmake_minimum_required below 3.5 is rejected by CMake 4.x")
                patches.append(Patch(path, i + 1, orig.rstrip("\n"),
                                     new.rstrip("\n"), reason))
                lines[i] = new
                changed = True

        if changed and not dry_run:
            backup = path + ".fd-backup"
            if not os.path.exists(backup):
                shutil.copy2(path, backup)
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.writelines(lines)
    return patches


def revert_cmake_patches(root: str) -> int:
    n = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".fd-backup"):
                bak = os.path.join(dirpath, fn)
                shutil.move(bak, bak[:-len(".fd-backup")])
                n += 1
    return n


# --------------------------------------------------------------------------- #
# Breakage (3): the combined / single-snapshot ELF layout
#
# Recent Dart emits an app snapshot with three symbols -- _kDartSnapshotData,
# _kDartSnapshotText, _kDartSnapshotBuildId -- instead of the classic four
# (_kDartVmSnapshotData, _kDartVmSnapshotInstructions, _kDartIsolateSnapshotData,
# _kDartIsolateSnapshotInstructions). Blutter's ElfHelper only knows the four
# and throws on the three, so a current `flutter build apk` cannot be loaded.
#
# The fix maps the single snapshot onto the isolate slots and leaves the VM
# snapshot null: Dart_Initialize with a null VM snapshot falls back to the one
# built into libdart, and the isolate is created from _kDartSnapshotData /
# _kDartSnapshotText -- which is exactly how Dart's own embedder loads a single
# combined snapshot. Purely additive: the four-symbol path is untouched.
# --------------------------------------------------------------------------- #

# (before, after) pairs applied to ElfHelper.cpp, in order. Each `before` is a
# verbatim slice of the upstream file; if it is absent the patch is skipped, so
# an upstream that already handles this (or a refactor that moves the code) is
# a no-op rather than a corruption.
_ELF_PATCHES = [
    (
        "\t\t\tif (std::search(strtab, last, s_first, s_last) != last) {\n"
        "\t\t\t\t// found it\n"
        "\t\t\t\tdynstr = strtab;\n",
        "\t\t\tstatic const char fd_combined[] = \"_kDartSnapshotData\";\n"
        "\t\t\tif (std::search(strtab, last, s_first, s_last) != last ||\n"
        "\t\t\t    std::search(strtab, last, fd_combined, fd_combined + sizeof(fd_combined) - 1) != last) {\n"
        "\t\t\t\t// found it (split or combined snapshot layout)\n"
        "\t\t\t\tdynstr = strtab;\n",
    ),
    (
        "\t\telse if (strcmp(name, kIsolateSnapshotInstructionsAsmSymbol) == 0) {\n"
        "\t\t\tisolate_snapshot_instructions = elf + dynsym->value;\n"
        "\t\t}\n\t}\n",
        "\t\telse if (strcmp(name, kIsolateSnapshotInstructionsAsmSymbol) == 0) {\n"
        "\t\t\tisolate_snapshot_instructions = elf + dynsym->value;\n"
        "\t\t}\n"
        "\t\telse if (strcmp(name, \"_kDartSnapshotData\") == 0) {\n"
        "\t\t\tisolate_snapshot_data = elf + dynsym->value;\n"
        "\t\t}\n"
        "\t\telse if (strcmp(name, \"_kDartSnapshotText\") == 0) {\n"
        "\t\t\tisolate_snapshot_instructions = elf + dynsym->value;\n"
        "\t\t}\n\t}\n",
    ),
    (
        "\tif (vm_snapshot_data == nullptr)\n"
        "\t\tthrow std::invalid_argument(\"ELF: Cannot find Dart VM Snapshot Data\");\n"
        "\tif (vm_snapshot_instructions == nullptr)\n"
        "\t\tthrow std::invalid_argument(\"ELF: Cannot find Dart VM Snapshot Instructions\");\n"
        "\tif (isolate_snapshot_data == nullptr)\n"
        "\t\tthrow std::invalid_argument(\"ELF: Cannot find Dart Isolate Snapshot Data\");\n"
        "\tif (isolate_snapshot_instructions == nullptr)\n"
        "\t\tthrow std::invalid_argument(\"ELF: Cannot find Dart Isolate Snapshot Instructions\");\n",
        "\t// flutter_decompile: a single combined snapshot has no separate VM\n"
        "\t// snapshot; the VM then uses libdart's built-in one, so vm_* may be null.\n"
        "\tif (isolate_snapshot_data == nullptr)\n"
        "\t\tthrow std::invalid_argument(\"ELF: Cannot find Dart Isolate/App Snapshot Data\");\n"
        "\tif (isolate_snapshot_instructions == nullptr)\n"
        "\t\tthrow std::invalid_argument(\"ELF: Cannot find Dart Isolate/App Snapshot Instructions\");\n",
    ),
]

# extract_dart_info.py reads the snapshot hash from _kDartVmSnapshotData; fall
# back to the combined _kDartSnapshotData when the split symbol is absent.
_EXTRACT_BEFORE = "        sym = dynsym.get_symbol_by_name('_kDartVmSnapshotData')[0]\n"
_EXTRACT_AFTER = (
    "        _syms = (dynsym.get_symbol_by_name('_kDartVmSnapshotData')\n"
    "                 or dynsym.get_symbol_by_name('_kDartSnapshotData'))\n"
    "        sym = _syms[0]\n"
)


def _apply_text_patch(path: str, edits, marker: str, dry_run: bool) -> List[Patch]:
    """Apply (before, after) text edits to one file. Idempotent via ``marker``:
    if it is already present the file is left alone. Missing ``before`` slices
    are skipped, never forced."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []
    if marker in text:
        return []                     # already patched
    applied: List[Patch] = []
    new = text
    for before, after in edits:
        if before in new:
            new = new.replace(before, after, 1)
            applied.append(Patch(path, 0, before.strip().split("\n")[0],
                                 after.strip().split("\n")[0],
                                 "support Dart's combined single-snapshot layout"))
    if applied and not dry_run:
        backup = path + ".fd-backup"
        if not os.path.exists(backup):
            shutil.copy2(path, backup)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(new)
    return applied


def windows_externals_present(root: str) -> bool:
    """Blutter's Windows build reads ICU and Capstone from external/. Both dirs
    must exist for the Dart VM configure (find_package(ICU REQUIRED)) to pass."""
    ext = os.path.join(root, "external")
    return (os.path.isdir(os.path.join(ext, "icu-windows"))
            and os.path.isdir(os.path.join(ext, "capstone")))


def ensure_windows_externals(root: str, log=print) -> bool:
    """Run Blutter's own scripts/init_env_win.py to fetch ICU + Capstone.

    This is the step Blutter's README makes you run by hand on Windows before
    the first build; without it the Dart VM configure fails with
    "Failed to find all ICU components". Idempotent: skipped once external/
    holds both. Returns True if it downloaded them this call.
    """
    if not IS_WINDOWS:
        return False
    if windows_externals_present(root):
        return False
    script = os.path.join(root, "scripts", "init_env_win.py")
    if not os.path.isfile(script):
        raise BlutterError(
            "Blutter's Windows setup script (scripts/init_env_win.py) is "
            "missing from the clone,\nso ICU and Capstone cannot be fetched. "
            "The checkout looks incomplete; delete\n" + root + " and let it "
            "re-clone.")
    log("[blutter] fetching ICU + Capstone (Blutter's Windows build deps, "
        "one-time)...")
    proc = subprocess.run([sys.executable, script],
                          cwd=os.path.join(root, "scripts"),
                          capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-1500:]
        hint = ""
        if "requests" in tail:
            hint = ("\nIt needs the 'requests' package: "
                    "pip install requests")
        raise BlutterError(
            "Blutter's Windows dependency setup failed:\n" + tail + hint)
    if not windows_externals_present(root):
        raise BlutterError(
            "Blutter's setup ran but external/icu-windows or external/capstone "
            "is still missing.\nOutput:\n" + (proc.stdout or "")[-800:])
    log("[blutter] ICU + Capstone ready")
    return True


def patch_combined_snapshot(root: str, dry_run: bool = False) -> List[Patch]:
    """Teach Blutter to load Dart's combined (_kDartSnapshotData / ...Text)
    snapshot layout. Idempotent; safe to run on an already-patched or
    already-compatible checkout."""
    patches: List[Patch] = []
    elf = os.path.join(root, "blutter", "src", "ElfHelper.cpp")
    if os.path.isfile(elf):
        patches += _apply_text_patch(elf, _ELF_PATCHES, "_kDartSnapshotText", dry_run)
    extract = os.path.join(root, "extract_dart_info.py")
    if os.path.isfile(extract):
        patches += _apply_text_patch(extract, [(_EXTRACT_BEFORE, _EXTRACT_AFTER)],
                                     "_kDartSnapshotData", dry_run)
    return patches


# The Dart VM source for recent versions uses __VA_OPT__ (a C++20 preprocessor
# feature). MSVC only accepts it under its conforming preprocessor, which the
# Dart VM CMakeLists does not enable, so the build dies with C3861 "__VA_OPT__
# identifier not found". Add /Zc:preprocessor to the MSVC compile options.
_MSVC_CCOPTS_BEFORE = "\t\t/Oy /GR- /EHs-c-\n"
_MSVC_CCOPTS_AFTER = "\t\t/Oy /GR- /EHs-c- /Zc:preprocessor\n"


def patch_msvc_preprocessor(root: str, dry_run: bool = False) -> List[Patch]:
    """Add /Zc:preprocessor to the Dart VM's MSVC flags so __VA_OPT__ compiles.

    Windows/MSVC only in effect (the flag lives in an ``if (MSVC)`` block), but
    applied unconditionally: it is inert on a checkout built with clang, and the
    per-version dartsdk CMakeLists path is not known ahead of time, so every
    CMakeLists carrying the exact MSVC cc_opts line is patched. Idempotent."""
    if not IS_WINDOWS:
        return []
    patches: List[Patch] = []
    for path in _iter_cmake_files(root):
        patches += _apply_text_patch(
            path, [(_MSVC_CCOPTS_BEFORE, _MSVC_CCOPTS_AFTER)],
            "/Zc:preprocessor", dry_run)
    return patches


# --------------------------------------------------------------------------- #
# Breakage (2): MSVC environment
# --------------------------------------------------------------------------- #

VSWHERE_DEFAULT = os.path.join(
    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    "Microsoft Visual Studio", "Installer", "vswhere.exe")


def find_vcvars(arch: str = "x64") -> Optional[str]:
    """Locate vcvars<arch>.bat via vswhere, then via well-known paths."""
    if not IS_WINDOWS:
        return None
    bat = "vcvars64.bat" if arch == "x64" else "vcvarsall.bat"

    vswhere = VSWHERE_DEFAULT if os.path.isfile(VSWHERE_DEFAULT) else shutil.which("vswhere")
    if vswhere:
        try:
            out = subprocess.run(
                [vswhere, "-latest", "-products", "*",
                 "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                 "-property", "installationPath"],
                capture_output=True, text=True, timeout=60)
            root = out.stdout.strip().splitlines()
            if root:
                p = os.path.join(root[0], "VC", "Auxiliary", "Build", bat)
                if os.path.isfile(p):
                    return p
        except (OSError, subprocess.SubprocessError):
            pass

    for pf in (os.environ.get("ProgramFiles", r"C:\Program Files"),
               os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        for year in ("2022", "2019", "2017"):
            for ed in ("Enterprise", "Professional", "Community", "BuildTools"):
                p = os.path.join(pf, "Microsoft Visual Studio", year, ed,
                                 "VC", "Auxiliary", "Build", bat)
                if os.path.isfile(p):
                    return p
    return None


def msvc_env(arch: str = "x64") -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Return (environment, note).

    Runs vcvars in a throwaway cmd.exe and captures the resulting environment.
    Returns (None, reason) when MSVC cannot be found.
    """
    if not IS_WINDOWS:
        return None, "not Windows; no MSVC environment needed"
    if os.environ.get("VSINSTALLDIR") and os.environ.get("INCLUDE"):
        return dict(os.environ), "already inside an MSVC developer environment"

    vcvars = find_vcvars(arch)
    if not vcvars:
        return None, ("MSVC not found. Blutter compiles the Dart VM from source "
                      "and needs the C++ build tools. Install 'Desktop development "
                      "with C++' (VS 2022 or Build Tools), or run flutter_decompile "
                      "from a 'x64 Native Tools Command Prompt for VS'.")

    # Two Windows-specific traps, both hit on real machines:
    #
    #  a) `subprocess.run(["cmd.exe", "/c", 'call "C:\\...\\vcvars64.bat" && set'])`
    #     does NOT work: Python's list2cmdline backslash-escapes the inner
    #     quotes (\"C:\...\"), which cmd.exe does not understand, and you get
    #     "The system cannot find the path specified".  We therefore write a
    #     throwaway .bat wrapper and run that; quoting inside a .bat is sane.
    #
    #  b) Do NOT redirect vcvars' stderr (`2>&1 >NUL`).  vcvars64.bat shells out
    #     to vswhere.exe and, on installs where vswhere is not on PATH, writes a
    #     harmless warning to stderr; swallowing that stream makes the script
    #     abort before exporting INCLUDE/LIB.  Redirect stdout only.
    #
    # vcvars64.bat takes no architecture argument (it *is* the x64 one); only
    # vcvarsall.bat does.
    marker = "__FD_ENV_MARKER__"
    arch_arg = "" if os.path.basename(vcvars).lower() == "vcvars64.bat" else " " + arch
    fd, bat = tempfile.mkstemp(suffix=".bat", prefix="fd_vcvars_")
    os.close(fd)
    try:
        with open(bat, "w", newline="\r\n") as fh:
            fh.write("@echo off\n")
            fh.write('call "%s"%s >NUL\n' % (vcvars, arch_arg))
            fh.write("echo %s\n" % marker)
            fh.write("set\n")
        try:
            res = subprocess.run(["cmd.exe", "/c", bat],
                                 capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as e:
            return None, "failed to run %s: %s" % (vcvars, e)
    finally:
        try:
            os.unlink(bat)
        except OSError:
            pass

    if marker not in res.stdout:
        return None, "vcvars failed: %s" % (res.stderr.strip()[:400] or "no output")

    env: Dict[str, str] = {}
    seen_marker = False
    for line in res.stdout.splitlines():
        if not seen_marker:
            seen_marker = marker in line
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    if "INCLUDE" not in env:
        return None, "vcvars ran but INCLUDE was not set; the C++ toolchain is incomplete"
    return env, "MSVC environment captured from %s" % vcvars


# --------------------------------------------------------------------------- #
# Run / adopt
# --------------------------------------------------------------------------- #

@dataclass
class BlutterRun:
    out_dir: str
    adopted: bool
    cmd: Optional[List[str]] = None
    returncode: Optional[int] = None
    patches: List[Patch] = dc_field(default_factory=list)
    env_note: Optional[str] = None
    notes: List[str] = dc_field(default_factory=list)
    missing: List[str] = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and (self.adopted or self.returncode == 0)

    def to_dict(self) -> Dict[str, Any]:
        return {"out_dir": self.out_dir, "adopted": self.adopted, "cmd": self.cmd,
                "returncode": self.returncode, "env_note": self.env_note,
                "patches": [p.to_dict() for p in self.patches],
                "notes": self.notes, "missing": self.missing, "ok": self.ok}


def validate_output(out_dir: str) -> List[str]:
    """Return the list of REQUIRED outputs that are missing."""
    missing = []
    for name in REQUIRED_OUTPUTS:
        p = os.path.join(out_dir, name)
        if name == "asm":
            if not os.path.isdir(p):
                missing.append(name + "/")
            else:
                has = any(fn.endswith(".dart")
                          for _d, _dn, fns in os.walk(p) for fn in fns)
                if not has:
                    missing.append("asm/**/*.dart (directory is empty)")
        elif not os.path.isfile(p):
            missing.append(name)
    return missing


def adopt(out_dir: str) -> BlutterRun:
    """--blutter-out: reuse an existing Blutter output, run nothing."""
    run = BlutterRun(out_dir=os.path.abspath(out_dir), adopted=True)
    run.missing = validate_output(run.out_dir)
    if run.missing:
        raise BlutterError(
            "the Blutter output at %s is missing: %s"
            % (run.out_dir, ", ".join(run.missing)))
    for name in OPTIONAL_OUTPUTS:
        if not os.path.exists(os.path.join(run.out_dir, name)):
            run.notes.append("optional output %s absent (harmless)" % name)
    return run


def dartvm_cache_dir(dart_version: str, snapshot_hash: str) -> str:
    base = os.path.join(os.path.expanduser("~"), ".cache", "flutter_decompile", "dartvm")
    return os.path.join(base, "%s_%s" % (dart_version, snapshot_hash))


def _diagnose_blutter_failure(output: str, dart_version: Optional[str]) -> Optional[str]:
    """Turn a wall of build errors into one accurate sentence about the cause.

    The important case: Blutter's C++ is written against a specific Dart
    embedder / VM-internal API and is updated per Dart release. A Dart version
    newer than Blutter supports fails to COMPILE Blutter -- distinct from a
    missing toolchain -- and we should say so instead of pointing at --check.
    """
    ver = dart_version or "this Dart version"
    # Dart embedder API drift: fields/renames in Blutter's own source.
    if ("Dart_InitializeParams" in output
            or "stub_code_list" in output
            or "OBJECT_STORE_STUB_CODE_LIST" in output
            or ("is not a member" in output and "Dart" in output)
            or ("kein Member" in output and "Dart" in output)):
        return (
            "This is Blutter failing to COMPILE against Dart %s, not a problem "
            "with your\nmachine. Blutter's C++ tracks Dart's embedder / VM "
            "internal API, which changed\nin this version (e.g. "
            "Dart_InitializeParams lost its snapshot fields, and the\nstub-code "
            "list macros changed shape). Blutter has to be ported to Dart %s "
            "for\nit to build -- that is upstream work, version by version.\n\n"
            "What works today: build the app with an older, stable Flutter/Dart "
            "whose\nversion Blutter already supports, and run this again. The "
            "full build log is at\nblutter_build.log in the output directory."
            % (ver, ver))
    if "__VA_OPT__" in output:
        return ("The Dart VM build hit a __VA_OPT__ error. That is normally "
                "patched with\n/Zc:preprocessor; if you see this, the patch did "
                "not apply -- please report it.")
    if "ICU" in output and "find" in output.lower():
        return ("The Dart VM build could not find ICU. It is normally fetched "
                "automatically;\nif you see this, delete the blutter clone and "
                "let it re-set-up.")
    return None


def run_blutter(libapp: str,
                out_dir: str,
                blutter_py: Optional[str] = None,
                dart_version: Optional[str] = None,
                snapshot_hash: Optional[str] = None,
                patch_cmake: bool = True,
                use_msvc_env: bool = True,
                extra_args: Optional[List[str]] = None,
                timeout: Optional[int] = None,
                log=print) -> BlutterRun:
    """Stage 2.  Runs Blutter over ``libapp`` into ``out_dir``."""
    blutter_py = find_blutter(blutter_py)
    if not blutter_py:
        raise BlutterError(
            "Blutter not found. Pass --blutter /path/to/blutter.py, set "
            "BLUTTER_PATH, or use --blutter-out to reuse an existing output.")
    root = blutter_root(blutter_py)
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    run = BlutterRun(out_dir=out_dir, adopted=False)

    if patch_cmake:
        run.patches = patch_cmake_compat(root)
        if run.patches:
            log("[blutter] applied %d CMake 4.x compatibility patch(es) under %s"
                % (len(run.patches), root))
            for p in run.patches[:5]:
                log("           %s:%d  %s" % (os.path.relpath(p.path, root),
                                              p.lineno, p.reason))

    env: Optional[Dict[str, str]] = None
    if use_msvc_env and IS_WINDOWS:
        env, note = msvc_env()
        run.env_note = note
        if env is None:
            raise BlutterError(
                "Blutter cannot build on Windows outside an MSVC environment.\n  %s" % note)
        log("[blutter] %s" % note)
    else:
        run.env_note = "using the inherited environment"

    if blutter_py.endswith(".py"):
        cmd = [sys.executable, blutter_py, libapp, out_dir]
    else:
        cmd = [blutter_py, libapp, out_dir]
    if dart_version:
        cmd += ["--dart-version", dart_version]
    if snapshot_hash:
        cmd += ["--vm-hash", snapshot_hash]
    if extra_args:
        cmd += list(extra_args)
    run.cmd = cmd

    if dart_version and snapshot_hash:
        cache = dartvm_cache_dir(dart_version, snapshot_hash)
        os.makedirs(cache, exist_ok=True)
        run.notes.append("dartvm build cache: %s" % cache)

    log("[blutter] %s" % " ".join(cmd))
    # Stream Blutter's output live (so a long build is visible) AND capture it,
    # so a failure can be diagnosed instead of reported as a bare exit code.
    build_log = os.path.join(out_dir, "blutter_build.log")
    tail: "collections.deque[str]" = collections.deque(maxlen=400)
    try:
        with open(build_log, "w", encoding="utf-8", errors="replace") as lf:
            proc = subprocess.Popen(cmd, cwd=root, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace", bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                lf.write(line)
                tail.append(line)
            run.returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise BlutterError("Blutter timed out after %ss" % timeout)
    except OSError as e:
        raise BlutterError("failed to launch Blutter: %s" % e)

    run.missing = validate_output(out_dir)
    if run.returncode != 0:
        diag = _diagnose_blutter_failure("".join(tail), dart_version)
        raise BlutterError(
            "Blutter exited with code %s. Missing outputs: %s%s"
            % (run.returncode, ", ".join(run.missing) or "none",
               ("\n\n" + diag) if diag else ""))
    if run.missing:
        raise BlutterError(
            "Blutter reported success but did not produce: %s"
            % ", ".join(run.missing))
    return run


def preflight(blutter_py: Optional[str] = None) -> Dict[str, Any]:
    """Report on the build prerequisites without changing anything."""
    found = find_blutter(blutter_py)
    info: Dict[str, Any] = {
        "blutter": found,
        "python": sys.executable,
        "cmake": shutil.which("cmake"),
        "ninja": shutil.which("ninja"),
        "git": shutil.which("git"),
        "windows": IS_WINDOWS,
    }
    if info["cmake"]:
        try:
            out = subprocess.run([info["cmake"], "--version"],
                                 capture_output=True, text=True, timeout=30)
            first = out.stdout.strip().splitlines()
            info["cmake_version"] = first[0] if first else None
            m = re.search(r"(\d+)\.(\d+)", info.get("cmake_version") or "")
            if m and int(m.group(1)) >= 4:
                info["cmake_4x"] = True
                info["cmake_note"] = (
                    "CMake 4.x detected: the string(REPLACE ... ${CMAKE_CXX_FLAGS}) "
                    "and cmake_minimum_required(<3.5) patches WILL be needed.")
        except (OSError, subprocess.SubprocessError):
            pass
    if IS_WINDOWS:
        info["vcvars"] = find_vcvars()
        info["in_msvc_shell"] = bool(os.environ.get("VSINSTALLDIR") and os.environ.get("INCLUDE"))
    if found:
        info["cmake_patches_needed"] = [p.to_dict()
                                        for p in patch_cmake_compat(blutter_root(found),
                                                                    dry_run=True)]
    return info
