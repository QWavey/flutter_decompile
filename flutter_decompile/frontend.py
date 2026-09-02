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
make sense of it. It is cached afterwards, so the second APK on the same Dart
version is minutes rather than an hour.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import CAPABILITY, __version__
from . import apk as apk_mod
from . import blutter_driver as bd
from . import bootstrap


# --------------------------------------------------------------------------- #
# Time estimates
#
# Measured on the machine this was developed on (8-core laptop, NVMe) against a
# 124 MB release APK: 266 libraries, 910 classes, 553k lines of disassembly.
# They are ranges because the dominant cost is a C++ build whose speed depends
# almost entirely on your core count.
# --------------------------------------------------------------------------- #

ESTIMATES = [
    ("check",    "check the toolchain",              (2, 10)),
    ("fetch",    "fetch Blutter (first run only)",   (10, 60)),
    ("acquire",  "unpack the APK, find libapp.so",   (3, 30)),
    ("identify", "read the Dart version + snapshot", (1, 5)),
    ("vm",       "build a matching Dart VM",         (1200, 3600)),
    ("blutter",  "disassemble the snapshot",         (60, 600)),
    ("parse",    "parse the disassembly",            (3, 30)),
    ("emit",     "write skeletons + report",         (2, 20)),
]

CACHED_STAGES = {"fetch", "vm"}


def human(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def show_plan(vm_cached: bool, blutter_present: bool,
              adopting: bool = False) -> None:
    print("\nPlan and rough timings:\n")
    low = high = 0
    for key, label, (lo, hi) in ESTIMATES:
        skipped = ((key == "vm" and vm_cached)
                   or (key == "fetch" and blutter_present)
                   or (adopting and key in ("fetch", "acquire", "vm", "blutter")))
        if skipped:
            print(f"  - {label:<36} (cached, skipped)")
            continue
        low += lo
        high += hi
        print(f"  - {label:<36} {human(lo)} - {human(hi)}")
    print(f"\n  Total: roughly {human(low)} to {human(high)}.")
    if not vm_cached:
        print("  Most of that is the one-off Dart VM build. It is cached, so the")
        print("  next APK on this Dart version takes minutes, not hours.")
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
        self.stage_started = time.time()
        print(f"\n[{human(time.time() - self.started):>8}] {label}…", flush=True)

    def done(self) -> None:
        if self.current:
            print(f"[{human(time.time() - self.started):>8}] "
                  f"{self.current} - done in {human(time.time() - self.stage_started)}",
                  flush=True)
            self.current = None

    def total(self) -> str:
        return human(time.time() - self.started)


def cmd_check() -> int:
    ok, checks = bootstrap.check_prerequisites()
    print("\nToolchain:\n")
    print(bootstrap.render_prerequisites(checks))
    found = bd.find_blutter(None)
    cached = os.path.join(bootstrap.default_blutter_dir(), "blutter.py")
    if found:
        print(f"\n  [ok  ] blutter                {found}")
    elif os.path.exists(cached):
        print(f"\n  [ok  ] blutter                {cached}")
    else:
        print("\n  [    ] blutter                not present - will be cloned on first run")
    print("\nReady." if ok else
          "\nSomething is missing. Install the items marked MISS above.")
    return 0 if ok else 1


def main() -> int:
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
                    help="do not pause for confirmation before the long part")
    ap.add_argument("--check", action="store_true", help="check the toolchain and exit")
    ap.add_argument("--capability", action="store_true",
                    help="print exactly what can and cannot be recovered")
    ap.add_argument("--version", action="version",
                    version=f"flutter_decompile {__version__}")
    args = ap.parse_args()

    if args.capability:
        print(CAPABILITY)
        return 0
    if args.check:
        return cmd_check()
    if not args.decompile:
        ap.print_help()
        return 2
    if not os.path.exists(args.decompile):
        print(f"No such file: {args.decompile}", file=sys.stderr)
        return 2

    print(f"flutter_decompile {__version__}")
    print("Reconstructs a structural skeleton. It does not, and cannot, give "
          "you\nthe original Dart source - run --capability for exactly what "
          "survives.")

    progress = Progress()

    # ---- toolchain -------------------------------------------------------
    progress.stage("Checking the toolchain")
    ok, checks = bootstrap.check_prerequisites()
    print(bootstrap.render_prerequisites(checks))
    if not ok:
        print("\nCannot continue until the items marked MISS are installed.")
        return 1

    blutter_present = bool(bd.find_blutter(args.blutter)) or os.path.exists(
        os.path.join(bootstrap.default_blutter_dir(), "blutter.py"))
    progress.done()

    # ---- what are we looking at ------------------------------------------
    progress.stage("Unpacking and identifying the input")
    workdir = os.path.join(args.out, "_work")
    os.makedirs(workdir, exist_ok=True)
    acq = apk_mod.acquire(args.decompile, workdir, abi=args.abi)

    # An already-disassembled directory skips everything expensive. Worth
    # supporting explicitly: it is how you re-run the analysis with different
    # flags without paying for the VM build twice.
    adopting = acq.source_kind == "blutter_out"
    if adopting:
        print("  input is an existing Blutter output - the VM build and "
              "disassembly are skipped")

    info = apk_mod.identify(acq)
    dart_version = getattr(info, "dart_version", None) or "unknown"
    snapshot = (getattr(info, "snapshot_hash", None) or "")[:16]
    print(f"  Dart:     {dart_version}")
    print(f"  snapshot: {snapshot or 'unknown'}")
    print(f"  ABI:      {args.abi}")
    progress.done()

    vm_cached = bool(dart_version != "unknown" and snapshot and os.path.isdir(
        bd.dartvm_cache_dir(dart_version, snapshot)))

    show_plan(vm_cached=vm_cached or adopting,
              blutter_present=blutter_present or adopting,
              adopting=adopting)

    if not args.yes and not vm_cached and not adopting and sys.stdin.isatty():
        try:
            if input("Start? [Y/n] ").strip().lower() in ("n", "no"):
                print("Stopped. Nothing was built.")
                return 0
        except (EOFError, KeyboardInterrupt):
            print("\nStopped.")
            return 0

    # ---- blutter ---------------------------------------------------------
    if adopting:
        run = bd.adopt(acq.blutter_out)
    else:
        progress.stage("Preparing Blutter")
        try:
            blutter_py = bootstrap.ensure_blutter(
                args.blutter, auto_download=not args.no_download)
            bootstrap.prepare_blutter(blutter_py)
        except RuntimeError as e:
            print(f"\n{e}")
            return 1
        progress.done()

        progress.stage("Building the Dart VM and disassembling "
                       "(the long one - output follows)")
        try:
            run = bd.run_blutter(acq.libapp,
                                 os.path.join(args.out, "blutter_out"),
                                 blutter_py=blutter_py)
        except Exception as e:
            print(f"\nBlutter failed: {e}")
            print("\nIf this is a compiler or CMake error, run --check. The "
                  "fixes for\nthe two known Blutter build breakages are "
                  "applied automatically,\nbut a missing toolchain is not "
                  "something this can work around.")
            return 1
        progress.done()

    # ---- parse and emit --------------------------------------------------
    progress.stage("Parsing the disassembly")
    from . import cli as fd_cli
    argv = [run.out_dir, "--no-blutter", "-o", args.out,
            "--skeleton", args.only, "--report", "both"]
    if args.quick:
        argv.append("--no-bodies")
    rc = fd_cli.main(argv)
    progress.done()

    print(f"\nFinished in {progress.total()}.")
    print(f"\n  skeletons:  {os.path.join(args.out, 'skeletons')}")
    print(f"  report:     {os.path.join(args.out, 'report.md')}")
    asm = (os.path.join(run.out_dir, "asm") if adopting
           else os.path.join(args.out, "blutter_out", "asm"))
    print(f"  disassembly: {asm}")
    print("\nField names marked INFERRED are reconstructions with the evidence")
    print("attached, not recovered names. Everything else came out of the "
          "snapshot.")
    return rc

