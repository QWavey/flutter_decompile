"""flutter_decompile.frontend - one command, from an APK to a readable skeleton.

    flutter-decompile --decompile app.apk      once pip-installed
    python main.py --decompile app.apk        from a checkout

That is the whole interface. It checks the toolchain, fetches Blutter if you do
not have it, builds a Dart VM matching the APK's snapshot, disassembles it, and
writes a skeleton of every Dart library it found, plus a report.

    flutter-decompile --decompile app.apk --out mydir
    flutter-decompile --decompile app.apk --only "**/auth/**"
    flutter-decompile --decompile app.apk --quick     skeleton only, much faster
    flutter-decompile --check                         just check the toolchain
    flutter-decompile --decompile app.apk -v          show full tracebacks

READ THIS FIRST
---------------
This does NOT give you back the original .dart files, and nothing can. Flutter
release builds compile Dart to native code; the snapshot keeps only what the
runtime needs. Statement bodies, comments, formatting, imports, local names and
parameter names were never written to the file - they are not obfuscated, they
do not exist.

What you get is real and useful: every library, class, method and enum by name,
all string literals and const objects, the full disassembly, and field names
where they can be reconstructed from evidence - each labelled with that
evidence and a confidence level. See README.md.

HOW LONG IT TAKES
-----------------
The first run on a given Dart version builds a Dart VM from source. That is the
long pole and it is unavoidable - Blutter needs a VM matching the snapshot to
make sense of it. Blutter keeps the result in its own checkout, so the second
APK on the same Dart version is minutes rather than an hour.

WHEN SOMETHING GOES WRONG
-------------------------
Every failure this knows about is reported as a sentence saying what failed and
what to do about it, and nothing else. A Python traceback reaching the terminal
is a bug in this file. `-v` prints the traceback anyway, which is what you want
when reporting one.
"""

from __future__ import annotations

import argparse
import errno
import glob
import os
import sys
import time
import traceback
import zipfile

from . import CAPABILITY, __version__
from . import apk as apk_mod
from . import blutter_driver as bd
from . import bootstrap

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130          # the shell convention for "killed by SIGINT"

# What acquire() can be handed. Checked here, before anything is unpacked, so
# that "that is a PDF" is said in one line instead of surfacing as a zipfile
# exception three frames down.
ARCHIVE_SUFFIXES = (".apk", ".apks", ".xapk", ".aab", ".zip")
INPUT_SUFFIXES = ARCHIVE_SUFFIXES + (".so",)

# Files that mean "this output directory already holds a previous run".
RESULT_MARKERS = ("skeletons", "report.md", "report.json", "blutter_out")


# --------------------------------------------------------------------------- #
# Time estimates
#
# Measured on the machine this was developed on (8-core laptop, NVMe) against a
# 124 MB release APK: 266 libraries, 910 classes, 553k lines of disassembly.
# They are ranges because the dominant cost is a C++ build whose speed depends
# almost entirely on your core count.
#
# "build Blutter + disassemble" is one entry because they are one Blutter
# invocation: blutter.py compiles its own executable against the Dart VM it
# just built, then runs it. On a second APK with the same Dart version both the
# VM and that executable already exist and only the disassembly runs, which is
# the fast end of the range.
# --------------------------------------------------------------------------- #

ESTIMATES = [
    ("check",    "check the toolchain",                (2, 10)),
    ("fetch",    "fetch Blutter (first run only)",     (10, 60)),
    ("acquire",  "unpack the APK, find libapp.so",     (3, 30)),
    ("identify", "read the Dart version + snapshot",   (1, 5)),
    ("vm",       "build a matching Dart VM",           (1200, 3600)),
    ("blutter",  "build Blutter + disassemble",        (60, 600)),
    ("parse",    "parse the disassembly",              (3, 30)),
    ("emit",     "write skeletons + report",           (2, 20)),
]


class Abort(Exception):
    """A failure we understand, phrased for the person who hit it.

    Anything raised as an Abort has already been turned into "here is what
    broke and here is what to do"; main() prints it and exits. Everything else
    reaching the top is, by definition, a bug we did not anticipate, and is
    reported as one.
    """

    def __init__(self, message: str, *hints: str, code: int = EXIT_FAILED):
        super().__init__(message)
        self.message = message
        self.hints = [h for h in hints if h]
        self.code = code


# Where we are, so that a Ctrl-C or a crash can say something specific about
# what is on disk rather than a generic apology.
_STATE = {"out": None, "stage": None, "long_build": False, "verbose": 0}


def human(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def show_plan(done: set, skipped: dict) -> None:
    """Print the remaining work.

    ``done`` is stages already finished by the time we get here - the plan is
    printed after the toolchain check and the unpack, so calling those
    "cached" would be a lie. ``skipped`` maps a stage key to the reason it
    will not run, which is not always caching: adopting an existing Blutter
    output skips the build because it is not needed, not because it is cached.
    """
    print("\nPlan and rough timings:\n")
    low = high = 0
    for key, label, (lo, hi) in ESTIMATES:
        if key in done:
            print(f"  - {label:<36} done")
        elif key in skipped:
            print(f"  - {label:<36} ({skipped[key]})")
        else:
            low += lo
            high += hi
            print(f"  - {label:<36} {human(lo)} - {human(hi)}")
    if not low and not high:
        print("\n  Nothing expensive left to do.")
        print()
        return
    print(f"\n  Still to do: roughly {human(low)} to {human(high)}.")
    if "vm" not in done and "vm" not in skipped:
        print("  Most of that is the one-off Dart VM build. Blutter keeps it,")
        print("  so the next APK on this Dart version takes minutes, not hours.")
    print()


class Progress:
    """Stage timing with a running total, printed as it goes."""

    def __init__(self) -> None:
        self.started = time.time()
        self.stage_started = self.started
        self.current: str | None = None

    def stage(self, label: str) -> None:
        self.done()
        self.current = label
        _STATE["stage"] = label
        self.stage_started = time.time()
        # Plain ASCII on purpose: this goes to a Windows console that may be
        # on a legacy code page, and a UnicodeEncodeError here would kill a
        # run that was otherwise fine.
        print(f"\n[{human(time.time() - self.started):>8}] {label}...", flush=True)

    def done(self) -> None:
        if self.current:
            print(f"[{human(time.time() - self.started):>8}] "
                  f"{self.current} - done in {human(time.time() - self.stage_started)}",
                  flush=True)
            self.current = None
            _STATE["stage"] = None

    def total(self) -> str:
        return human(time.time() - self.started)


# --------------------------------------------------------------------------- #
# Failure reporting
# --------------------------------------------------------------------------- #

def _os_error_sentence(e: OSError, doing: str) -> str:
    """One sentence for an OSError, in terms of what the user was doing.

    errno is the only reliable way to tell "the disk is full" from "you do not
    own that folder", and those need completely different advice.
    """
    known = {
        errno.ENOSPC: "the disk is full",
        errno.EACCES: "permission was denied",
        errno.EPERM: "permission was denied",
        errno.EROFS: "the filesystem is read-only",
        errno.ENOENT: "part of that path does not exist",
        errno.ENOTDIR: "part of that path is a file, not a directory",
        errno.ENAMETOOLONG: "the path is too long",
        errno.EINVAL: "that path is not a legal name on this filesystem",
        errno.EMFILE: "this process ran out of file handles",
        errno.ENFILE: "the system ran out of file handles",
    }
    why = known.get(e.errno) or (e.strerror or str(e))
    where = getattr(e, "filename", None)
    return f"{doing} failed: {why}" + (f" ({where})" if where else "")


def _hints_for_os_error(e: OSError) -> list:
    if e.errno == errno.ENOSPC:
        return ["Free some space, or send the output somewhere else with "
                "--out <dir>.",
                "A full run of a large APK writes a few hundred MB of "
                "disassembly."]
    if e.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return ["Choose a directory you can write to with --out <dir>."]
    return []


def _say(message: str) -> None:
    """Print, and flush, for anything printed alongside a subprocess.

    git and Blutter write straight to the console; our own stdout is buffered
    when it is a pipe. Without the flush our line about what we are running
    turns up after the output of the thing we ran.
    """
    print(message, flush=True)


def _stderr_after_stdout() -> None:
    """Keep the failure below the output it followed.

    stdout is block-buffered when it is a pipe or a log file, so without this
    the error lands above the progress it came after and reads as nonsense.
    """
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def _report_abort(e: Abort) -> int:
    _stderr_after_stdout()
    print(f"\n{e.message}", file=sys.stderr)
    for hint in e.hints:
        print(hint, file=sys.stderr)
    if _STATE["verbose"]:
        traceback.print_exc()
    return e.code


def _report_interrupt() -> int:
    _stderr_after_stdout()
    print("\n\nStopped (Ctrl-C).", file=sys.stderr)
    if _STATE["long_build"]:
        # ninja is incremental and Blutter re-uses whatever it already
        # compiled, so this is genuinely resumable - worth saying, because
        # otherwise nobody dares interrupt an hour-long build.
        print("The Dart VM build stops where it is. It is an incremental "
              "build, so\nre-running the same command continues it rather "
              "than starting over.", file=sys.stderr)
    if _STATE["out"]:
        print(f"Partial output is under {_STATE['out']} and is safe to delete.",
              file=sys.stderr)
    return EXIT_INTERRUPTED


def _report_unexpected(e: BaseException) -> int:
    _stderr_after_stdout()
    print(f"\nUnexpected failure: {type(e).__name__}: {e}", file=sys.stderr)
    if _STATE["stage"]:
        print(f"It happened while: {_STATE['stage']}.", file=sys.stderr)
    if _STATE["verbose"]:
        traceback.print_exc()
    else:
        print("This one is not handled, which makes it a bug in "
              "flutter_decompile.\nRe-run with -v for the full traceback and "
              "please report it with that.", file=sys.stderr)
    return EXIT_FAILED


# --------------------------------------------------------------------------- #
# Input and output checks - both done before any expensive work
# --------------------------------------------------------------------------- #

def check_input(path: str) -> None:
    """Refuse an input we already know cannot work, with the reason.

    Cheap, and it runs before the toolchain check, so pointing this at a PDF
    costs a fraction of a second rather than a minute of vswhere and unzip.
    """
    if not os.path.exists(path):
        raise Abort(
            f"No such file or directory: {path}",
            "Give it an .apk (or .apks/.xapk/.aab), a lib/<abi> directory, a "
            "libapp.so,\nor a directory Blutter has already written.",
            code=EXIT_USAGE)

    if os.path.isdir(path):
        return                      # acquire() reports precisely what is wrong

    if not os.path.isfile(path):
        raise Abort(f"{path} is not a regular file.", code=EXIT_USAGE)

    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise Abort(_os_error_sentence(e, f"Reading {path}"),
                    code=EXIT_USAGE) from e
    if size == 0:
        raise Abort(f"{path} is empty (0 bytes).",
                    "The download or copy that produced it did not finish.",
                    code=EXIT_USAGE)

    ext = os.path.splitext(path)[1].lower()
    if ext not in INPUT_SUFFIXES:
        raise Abort(
            f"{path} is not something this can read (suffix "
            f"'{ext or 'none'}').",
            "Accepted: " + ", ".join(INPUT_SUFFIXES) + ", a lib/<abi> "
            "directory, or a Blutter output directory.",
            code=EXIT_USAGE)

    if ext in ARCHIVE_SUFFIXES:
        # An APK is a zip. If the central directory is unreadable the file is
        # truncated or is not an APK at all, and that is worth saying now
        # rather than as a BadZipFile from inside stage 0.
        if not zipfile.is_zipfile(path):
            raise Abort(
                f"{path} is not a readable APK - it has no zip central "
                "directory.",
                "It is either truncated (an interrupted download) or not an "
                "APK.\nCheck it opens: python -c \"import zipfile,sys; "
                "zipfile.ZipFile(sys.argv[1]).testzip()\" " + path,
                code=EXIT_USAGE)
    elif ext == ".so":
        try:
            with open(path, "rb") as fh:
                magic = fh.read(4)
        except OSError as e:
            raise Abort(_os_error_sentence(e, f"Reading {path}"),
                        code=EXIT_USAGE) from e
        if magic != b"\x7fELF":
            raise Abort(
                f"{path} is not an ELF shared object (bad magic bytes).",
                "libapp.so out of an Android APK is what is expected here.",
                code=EXIT_USAGE)


def prepare_out_dir(out: str, force: bool) -> str:
    """Create the output directory, or explain why we cannot use it.

    Also refuses to write over a previous run unless told to. A second run
    overwrites report.md and every skeleton it regenerates, and silently
    mixing two APKs' results in one directory is the kind of thing you only
    notice much later.
    """
    out = os.path.abspath(out)

    if os.path.exists(out) and not os.path.isdir(out):
        raise Abort(f"--out {out} exists and is not a directory.",
                    "Pick another path with --out <dir>.", code=EXIT_USAGE)

    if os.path.isdir(out):
        try:
            present = [n for n in RESULT_MARKERS
                       if os.path.exists(os.path.join(out, n))]
        except OSError as e:
            raise Abort(_os_error_sentence(e, f"Reading {out}"),
                        *_hints_for_os_error(e)) from e
        if present and not force:
            raise Abort(
                f"{out} already holds results from an earlier run "
                f"({', '.join(present)}).",
                "Re-running overwrites the files it regenerates and leaves "
                "everything else\nin place, which quietly mixes two APKs "
                "together.",
                "Use --out <dir> for a fresh directory, or --force to write "
                "into this one\n(nothing is deleted either way).",
                code=EXIT_USAGE)

    try:
        os.makedirs(out, exist_ok=True)
    except OSError as e:
        raise Abort(_os_error_sentence(e, f"Creating the output directory {out}"),
                    *_hints_for_os_error(e)) from e

    # Creating a directory can succeed where writing a file into it does not:
    # a read-only mount, a full disk, a folder owned by someone else. Find out
    # now, not after an hour of building.
    probe = os.path.join(out, ".fd-write-test")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("flutter_decompile write test\n")
        os.unlink(probe)
    except OSError as e:
        raise Abort(_os_error_sentence(e, f"Writing into the output directory {out}"),
                    *_hints_for_os_error(e)) from e
    return out


def dartvm_is_built(blutter_py: str | None, dart_version: str) -> bool:
    """Has Blutter already built a VM and an executable for this Dart version?

    This is the same file blutter.py itself tests before deciding whether to
    spend an hour building: bin/blutter_dartvm<version>_<os>_<arch>[.exe]. If
    it is there, the long stage does not run.

    Note that ~/.cache/flutter_decompile/dartvm is NOT that cache - nothing in
    this path populates it - so it is not consulted here. Getting this wrong
    in the optimistic direction would promise a five-minute run and deliver an
    hour, so when in doubt this says False and the plan over-estimates.
    """
    if not blutter_py or not dart_version or dart_version == "unknown":
        return False
    root = bd.blutter_root(blutter_py)
    pattern = os.path.join(root, "bin", f"blutter_dartvm{dart_version}_*")
    return bool(glob.glob(pattern))


# --------------------------------------------------------------------------- #

def cmd_check() -> int:
    ok, checks = bootstrap.check_prerequisites()
    print("\nToolchain:\n")
    print(bootstrap.render_prerequisites(checks))
    found = bd.find_blutter(None)
    cache = bootstrap.default_blutter_dir()
    cached = os.path.join(cache, "blutter.py")
    if found:
        print(f"\n  [ok  ] blutter                {found}")
    elif os.path.exists(cached):
        gaps = bootstrap.missing_clone_parts(cache)
        if gaps:
            print(f"\n  [MISS] blutter                {cache} is an unfinished "
                  f"clone (missing {', '.join(gaps)})")
            print("         it will be removed and fetched again on the next run")
        else:
            print(f"\n  [ok  ] blutter                {cached}")
    else:
        print("\n  [    ] blutter                not present - will be cloned on first run")

    if ok:
        print("\nReady.")
        return 0
    print("\nSomething is missing. Install the items marked MISS above.")
    if all(c.get("build_only") for c in checks if not c["found"]):
        print("All of them are only needed to build the Dart VM and Blutter. "
              "Adopting an\nexisting Blutter output directory works without "
              "them.")
    return 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--decompile", metavar="APK",
                    help="the APK (or .aab / libapp.so / an existing blutter_out)")
    ap.add_argument("--out", metavar="DIR", default="decompiled",
                    help="output directory (default: decompiled/)")
    ap.add_argument("--only", metavar="GLOB", default="*",
                    help="restrict skeletons to libraries matching a glob")
    ap.add_argument("--quick", action="store_true",
                    help="skeleton only - skips body-fact extraction")
    ap.add_argument("--abi", default="arm64-v8a",
                    choices=["arm64-v8a", "armeabi-v7a", "x86_64"])
    ap.add_argument("--blutter", metavar="PATH", help="path to an existing blutter.py")
    ap.add_argument("--no-download", action="store_true",
                    help="never fetch anything; fail instead")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="answer every prompt yes: do not pause before the "
                         "long part, and write into an output directory that "
                         "already holds results")
    ap.add_argument("--force", action="store_true",
                    help="write into an output directory that already holds "
                         "results (nothing is deleted)")
    ap.add_argument("--verbose", "-v", action="count", default=0,
                    help="more detail, and a full traceback when something fails")
    ap.add_argument("--check", action="store_true", help="check the toolchain and exit")
    ap.add_argument("--capability", action="store_true",
                    help="print exactly what can and cannot be recovered")
    ap.add_argument("--version", action="version",
                    version=f"flutter_decompile {__version__}")
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    _STATE["verbose"] = args.verbose
    try:
        return _run(args, ap)
    except KeyboardInterrupt:
        return _report_interrupt()
    except Abort as e:
        return _report_abort(e)
    except OSError as e:
        # Disk full, permission denied, a vanished path - anywhere in the
        # pipeline, including inside the parse and emit stages.
        _stderr_after_stdout()
        print("\n" + _os_error_sentence(e, _STATE["stage"] or "The run"),
              file=sys.stderr)
        for hint in _hints_for_os_error(e):
            print(hint, file=sys.stderr)
        if _STATE["verbose"]:
            traceback.print_exc()
        return EXIT_FAILED
    except Exception as e:                    # noqa: BLE001 - last resort
        return _report_unexpected(e)


def _run(args, ap) -> int:
    if args.capability:
        print(CAPABILITY)
        return EXIT_OK
    if args.check:
        return cmd_check()
    if not args.decompile:
        ap.print_help()
        return EXIT_USAGE

    check_input(args.decompile)

    print(f"flutter_decompile {__version__}")
    print("Reconstructs a structural skeleton. It does not, and cannot, give "
          "you\nthe original Dart source - run --capability for exactly what "
          "survives.")

    out = prepare_out_dir(args.out, force=args.force or args.yes)
    _STATE["out"] = out
    for warning in apk_mod.check_abi_support(args.abi):
        print(f"\n!! {warning}")

    progress = Progress()
    done: set = set()
    skipped: dict = {}

    # ---- toolchain -------------------------------------------------------
    # Checked first because it is cheap and because everything it looks for is
    # needed by the expensive stage. Whether a MISS is fatal depends on what
    # the input turns out to be, so the verdict waits until after the unpack.
    progress.stage("Checking the toolchain")
    toolchain_ok, checks = bootstrap.check_prerequisites()
    print(bootstrap.render_prerequisites(checks))
    # "present" means usable: a directory holding half a clone is not.
    cache_gaps = bootstrap.missing_clone_parts(bootstrap.default_blutter_dir())
    blutter_present = bool(bd.find_blutter(args.blutter)) or not cache_gaps
    progress.done()
    done.add("check")

    # ---- what are we looking at ------------------------------------------
    progress.stage("Unpacking and identifying the input")
    workdir = os.path.join(out, "_work")
    try:
        os.makedirs(workdir, exist_ok=True)
        acq = apk_mod.acquire(args.decompile, workdir, abi=args.abi)
    except apk_mod.AcquireError as e:
        raise Abort(f"Cannot use {args.decompile}: {e}",
                    _abi_hint(str(e), args.abi), code=EXIT_USAGE) from e
    except zipfile.BadZipFile as e:
        raise Abort(f"{args.decompile} is not a readable APK: {e}",
                    "The file is truncated or corrupt - fetch it again.",
                    code=EXIT_USAGE) from e
    except OSError as e:
        raise Abort(_os_error_sentence(e, "Unpacking the input"),
                    *_hints_for_os_error(e)) from e

    for note in acq.notes:
        print(f"  note: {note}")

    # An already-disassembled directory skips everything expensive. Worth
    # supporting explicitly: it is how you re-run the analysis with different
    # flags without paying for the VM build twice.
    adopting = acq.source_kind == "blutter_out"
    if adopting:
        # Identifying is pointless here - there is no libapp.so or
        # libflutter.so to read - and running it anyway prints four warnings
        # about signals that were never going to be available, which reads
        # like something went wrong when nothing did.
        print("  input is an existing Blutter output - the VM build and "
              "disassembly are skipped")
        print("  Dart:     not checked (no libapp.so/libflutter.so to read; "
              "the disassembly is\n            taken as-is and its Dart "
              "version is unverified)")
        dart_version = "unknown"
    else:
        info = apk_mod.identify(acq)
        dart_version = getattr(info, "dart_version", None) or "unknown"
        snapshot = (getattr(info, "snapshot_hash", None) or "")[:16]
        print(f"  Dart:     {dart_version}")
        print(f"  snapshot: {snapshot or 'unknown'}")
        print(f"  ABI:      {args.abi}")
        for warning in info.warnings:
            print(f"  warning: {warning}")
    progress.done()
    done.add("acquire")
    if not adopting:
        done.add("identify")

    # ---- is the toolchain good enough for what we are about to do? -------
    if adopting:
        # "identify" is skipped, not done: it never ran, and printing "done"
        # against a stage whose whole output we just said was unverified is
        # the same lie the rest of this function exists to avoid.
        skipped.update({"fetch": "not needed", "vm": "not needed",
                        "blutter": "not needed",
                        "identify": "nothing to read it from"})
        if not toolchain_ok:
            print("\n  The items marked MISS above are only needed to build "
                  "the Dart VM and\n  Blutter. This input is already "
                  "disassembled, so they are not needed.")
    elif not toolchain_ok:
        missing = [str(c["tool"]) for c in checks if not c["found"]]
        raise Abort(
            "Cannot continue: " + ", ".join(missing) + " "
            + ("is" if len(missing) == 1 else "are") + " missing.",
            "Install the items marked MISS above and run this again.",
            "If you already have a Blutter output directory, pass that "
            "instead of the\nAPK and none of this is needed.")

    if not adopting:
        # Checked here rather than at the point of use: Blutter cannot start
        # without it, so there is no honest plan to show and no point cloning
        # anything first.
        if not acq.libflutter:
            raise Abort(
                "libflutter.so is not next to libapp.so, and Blutter needs "
                "both.",
                "It reads the Dart version out of libflutter.so; without it "
                "the build has\nnothing to match. Pass the APK itself, or a "
                "directory holding both files.",
                code=EXIT_USAGE)
        if blutter_present:
            skipped["fetch"] = "already present"
        blutter_py = bd.find_blutter(args.blutter) or os.path.join(
            bootstrap.default_blutter_dir(), "blutter.py")
        if dartvm_is_built(blutter_py if blutter_present else None, dart_version):
            skipped["vm"] = f"already built for Dart {dart_version}"

    show_plan(done=done, skipped=skipped)

    vm_needed = "vm" not in skipped and not adopting
    if not args.yes and vm_needed and sys.stdin.isatty():
        try:
            if input("Start? [Y/n] ").strip().lower() in ("n", "no"):
                print("Stopped. Nothing was built.")
                return EXIT_OK
        except (EOFError, KeyboardInterrupt):
            print("\nStopped.")
            return EXIT_OK

    # ---- blutter ---------------------------------------------------------
    if adopting:
        try:
            run = bd.adopt(acq.blutter_out)
        except bd.BlutterError as e:
            raise Abort(f"That is not a usable Blutter output: {e}",
                        "A Blutter output directory holds asm/, objs.txt and "
                        "pp.txt.\nRe-run Blutter on the APK, or pass the APK "
                        "here instead.", code=EXIT_USAGE) from e
    else:
        progress.stage("Preparing Blutter")
        try:
            blutter_py = bootstrap.ensure_blutter(
                args.blutter, auto_download=not args.no_download, log=_say)
            bootstrap.prepare_blutter(blutter_py, log=_say)
        except RuntimeError as e:
            raise Abort(str(e)) from e
        except OSError as e:
            raise Abort(_os_error_sentence(e, "Preparing Blutter"),
                        *_hints_for_os_error(e)) from e
        progress.done()

        # blutter.py wants a DIRECTORY holding libapp.so and libflutter.so (or
        # an .apk); handed a path to libapp.so it looks for libapp.so inside
        # it and exits with "Cannot find libapp file". acquire() always puts
        # the two side by side, so the directory is what we pass.
        lib_dir = os.path.dirname(os.path.abspath(acq.libapp))

        progress.stage("Building the Dart VM and disassembling "
                       "(the long one - output follows)")
        _STATE["long_build"] = True
        try:
            run = bd.run_blutter(lib_dir,
                                 os.path.join(out, "blutter_out"),
                                 blutter_py=blutter_py, log=_say)
        except bd.BlutterError as e:
            raise Abort(
                f"Blutter did not finish: {e}",
                "Its own output is above and says more than this line can.",
                "If that is a compiler or CMake error, run --check: the two "
                "known Blutter\nbuild breakages are patched automatically, but "
                "a missing toolchain is not\nsomething this can work around.",
                "If it ran out of memory - the Dart VM build is the hungry "
                "part - close other\nprograms and run this again: the build "
                "resumes rather than restarting.") from e
        except OSError as e:
            raise Abort(_os_error_sentence(e, "Running Blutter"),
                        *_hints_for_os_error(e)) from e
        # Deliberately not a finally: a Ctrl-C must leave this set, so the
        # interrupt message can say the half-finished build will be resumed.
        _STATE["long_build"] = False
        progress.done()

    # ---- parse and emit --------------------------------------------------
    progress.stage("Parsing the disassembly")
    from . import cli as fd_cli
    argv = [run.out_dir, "--no-blutter", "-o", out,
            "--skeleton", args.only, "--report", "both"]
    if args.quick:
        argv.append("--no-bodies")
    argv += ["-v"] * min(args.verbose, 2)
    rc = fd_cli.main(argv)
    progress.done()

    print(f"\nFinished in {progress.total()}.")
    # Printed by looking, not by assuming. A path in this list that does not
    # exist is worse than no line at all: it sends people hunting for a file
    # that was never written.
    skeletons = os.path.join(out, "skeletons")
    print("\n  skeletons:  " + (skeletons if os.path.isdir(skeletons)
                                else f"none written - nothing matched --only "
                                     f"{args.only!r}"))
    report = _first_existing(os.path.join(out, "report.md"),
                             os.path.join(out, "skeletons", "report.md"))
    print(f"  report:     {report or 'not written'}")
    print(f"  disassembly: {os.path.join(run.out_dir, 'asm')}")
    print("\nField names marked INFERRED are reconstructions with the evidence")
    print("attached, not recovered names. Everything else came out of the "
          "snapshot.")
    return rc


def _first_existing(*paths: str) -> str:
    for path in paths:
        if os.path.exists(path):
            return path
    return ""


def _abi_hint(message: str, abi: str) -> str:
    """Turn "Present ABIs: armeabi-v7a" into advice, when that is what it was."""
    if "ABIs:" not in message:
        return ""
    if message.rstrip().endswith("none"):
        return ("That archive carries no native libraries at all, so it is "
                "not a Flutter APK.\nIf it is one split of a split install, "
                "pass the whole .apks/.xapk bundle, or\nthe split that "
                "carries lib/.")
    return (f"This APK does not ship {abi}. Re-run with --abi set to one of "
            "the ABIs listed\nabove (arm64-v8a is the only one this tool "
            "supports fully).")
