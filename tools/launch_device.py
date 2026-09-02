#!/usr/bin/env python3
"""Pick a branch, boot Android emulators, and run the app on them.

Interactive by default: it shows you the branches, you pick one, it checks it
out and launches. Every flag below also lets you skip the questions.

    python tools/launch_device.py                    pick a branch, one device
    python tools/launch_device.py -b version2-ui     skip the branch question
    python tools/launch_device.py -n 2               two devices side by side
    python tools/launch_device.py --release          release build
    python tools/launch_device.py --list             show branches, AVDs, exit
    python tools/launch_device.py --reverse 8080     bridge a host port in
    python tools/launch_device.py --no-checkout      use the tree as it stands

Two phones is the interesting case and the reason -n exists. Most of this app
is one handset talking to another with no server between them - animated QR,
the USB cable, near mode - and none of it can be exercised on a single device.

Runs on Windows, macOS and Linux. Nothing about the host is hardcoded: the SDK
location, the Flutter binary, executable suffixes, the process listing and the
accelerator advice are all resolved at runtime, so this works on someone else's
machine as well as the one it was written on.

No third-party dependencies; standard library only.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from typing import Optional

BOOT_TIMEOUT_S = 360
POLL_S = 3

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

def find_project(start: str | None = None) -> str:
    """The Flutter project to build and run.

    This tool lives in its own repository, not inside the app it launches, so
    the project cannot be inferred from where this file sits. It is the
    directory you run from, or the nearest parent holding a pubspec.yaml, or
    whatever --project says.
    """
    here = os.path.abspath(start or os.getcwd())
    current = here
    while True:
        if os.path.exists(os.path.join(current, "pubspec.yaml")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            sys.exit(
                f"No Flutter project found at {here} or above it "
                "(nothing with a pubspec.yaml).\n"
                "Run this from inside your app, or pass --project <path>.")
        current = parent


# Resolved in main() once --project has been parsed; module-level only as a
# default for the helpers that take it implicitly.
REPO = ""


# --------------------------------------------------------------------------- #
# Locating the toolchain - discovered, never assumed.
# --------------------------------------------------------------------------- #

def _sdk_candidates() -> list[str]:
    """Where an Android SDK plausibly lives, most authoritative first.

    The environment wins when it is set: someone who exported ANDROID_SDK_ROOT
    has already told us the answer. Only then do we fall back to the default
    install location for this platform.
    """
    found = [os.environ.get("ANDROID_SDK_ROOT"), os.environ.get("ANDROID_HOME")]
    home = os.path.expanduser("~")
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        found.append(os.path.join(local, "Android", "Sdk"))
    elif IS_MAC:
        found.append(os.path.join(home, "Library", "Android", "sdk"))
    else:
        found += [os.path.join(home, "Android", "Sdk"),
                  "/usr/lib/android-sdk", "/opt/android-sdk"]
    return [p for p in found if p]


def find_sdk() -> str:
    seen: list[str] = []
    for candidate in _sdk_candidates():
        if candidate in seen:
            continue          # the env vars usually duplicate the default path
        seen.append(candidate)
        # Only counts if it actually holds the tools we need.
        if os.path.isdir(os.path.join(candidate, "platform-tools")):
            return candidate

    # Distinguish "no SDK" from "an SDK that is mid-update". Android Studio's
    # SDK manager moves platform-tools aside while it updates it, so a perfectly
    # good install can fail this check for a minute or two - and "SDK not found"
    # is a badly misleading thing to say about a directory that plainly exists.
    partial = [d for d in seen if os.path.isdir(d)]
    if partial:
        sys.exit(
            f"Found an Android SDK at {partial[0]}, but it has no "
            "platform-tools directory.\n\n"
            "If Android Studio's SDK Manager is updating right now, wait for it "
            "to finish\nand re-run - it moves platform-tools aside while it "
            "works.\n\n"
            "Otherwise install the platform tools:\n"
            "    sdkmanager \"platform-tools\"\n"
            "or in Android Studio: Settings > Languages & Frameworks > Android "
            "SDK >\nSDK Tools > Android SDK Platform-Tools.")

    sys.exit("Android SDK not found. Looked in:\n  "
             + "\n  ".join(seen)
             + "\nSet ANDROID_SDK_ROOT, or install the SDK via Android Studio.")


SDK = find_sdk()


def sdk_tool(*parts: str) -> str:
    """Path to an SDK executable, trying this platform's suffixes."""
    base = os.path.join(SDK, *parts)
    for suffix in ((".exe", ".bat", "") if IS_WINDOWS else ("",)):
        if os.path.exists(base + suffix):
            return base + suffix
    return base + (".exe" if IS_WINDOWS else "")


ADB = sdk_tool("platform-tools", "adb")
EMULATOR = sdk_tool("emulator", "emulator")


def find_flutter() -> str:
    """The flutter binary. `which` already handles the .bat on Windows."""
    found = shutil.which("flutter")
    if found:
        return found
    home = os.path.expanduser("~")
    guesses = [os.path.join(home, "flutter", "bin", "flutter"),
               os.path.join(home, "development", "flutter", "bin", "flutter"),
               "/usr/local/flutter/bin/flutter",
               "/opt/flutter/bin/flutter"]
    if IS_WINDOWS:
        guesses = [g + ".bat" for g in guesses] + [r"C:\src\flutter\bin\flutter.bat"]
    if IS_MAC:
        guesses.append("/opt/homebrew/bin/flutter")
    for g in guesses:
        if os.path.exists(g):
            return g
    sys.exit("flutter not found on PATH. Install it or add it to PATH.")


FLUTTER = find_flutter()


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def git(*args: str) -> str:
    return run(["git", "-C", REPO] + list(args)).stdout.strip()


# --------------------------------------------------------------------------- #
# Branch selection
# --------------------------------------------------------------------------- #

def branches() -> tuple[list[str], str]:
    """(selectable branches, current branch). Locals first, then remote-only."""
    current = git("rev-parse", "--abbrev-ref", "HEAD")
    local = [b for b in git("for-each-ref", "--format=%(refname:short)",
                            "refs/heads").splitlines() if b]
    remote = []
    for r in git("for-each-ref", "--format=%(refname:short)",
                 "refs/remotes").splitlines():
        # Skip the bare remote name (the HEAD symref shows up as just
        # "origin") and the explicit HEAD ref - neither is a branch.
        if not r or "/" not in r or r.endswith("/HEAD"):
            continue
        short = r.split("/", 1)[1]
        if short not in local:          # only offer what you cannot already pick
            remote.append(r)
    return local + remote, current


def describe(branch: str) -> str:
    subject = git("log", "-1", "--format=%s", branch)
    return subject[:56] + ("…" if len(subject) > 56 else "")


def pick_branch(current: str) -> str | None:
    # No terminal means no one to answer, so stay put rather than hang.
    if not sys.stdin.isatty():
        print(f"(not a terminal - staying on {current}; use -b to choose)")
        return None
    options, _ = branches()
    print("\nBranches:\n")
    for i, b in enumerate(options, 1):
        mark = "*" if b == current else " "
        print(f"  {mark} {i:>2}. {b:<38} {describe(b)}")
    print(f"\n     0. stay on {current} (no checkout)\n")

    while True:
        try:
            raw = input(f"Which branch? [Enter = stay on {current}] ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not raw or raw == "0":
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        if raw in options:
            return raw
        print("  Not one of those - enter a number or a branch name.")


def checkout(branch: str, current: str) -> bool:
    """Switch branches, refusing rather than discarding uncommitted work."""
    if branch == current:
        print(f"Already on {branch}.")
        return True

    if git("status", "--porcelain"):
        print("\nRefusing to switch: you have uncommitted changes.")
        print("Commit or stash them first, then re-run.\n")
        return False

    local = git("for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    if branch not in local and "/" in branch:
        # A remote-only branch needs a local tracking branch first.
        short = branch.split("/", 1)[1]
        print(f"Creating local branch {short} tracking {branch}…")
        if run(["git", "-C", REPO, "checkout", "-b", short,
                "--track", branch]).returncode != 0:
            print(f"Could not create a tracking branch for {branch}.")
            return False
        branch = short
    else:
        if run(["git", "-C", REPO, "checkout", branch]).returncode != 0:
            print(f"Could not check out {branch}.")
            return False

    print(f"On {branch}: {describe(branch)}")

    # Branches can differ in pubspec, and a stale .dart_tool produces failures
    # that read as code errors rather than a missed dependency fetch.
    print("flutter pub get…")
    subprocess.run([FLUTTER, "pub", "get"], cwd=REPO)
    return True


# --------------------------------------------------------------------------- #
# Devices
# --------------------------------------------------------------------------- #

def avds() -> list[str]:
    return [l.strip() for l in run([EMULATOR, "-list-avds"]).stdout.splitlines()
            if l.strip()]


def serials() -> list[str]:
    out = run([ADB, "devices"]).stdout
    return [p[0] for p in (l.split() for l in out.splitlines()[1:])
            if len(p) == 2 and p[1] == "device"]


def booted(serial: str) -> bool:
    """Android has finished booting - not merely that adb can see it.

    `adb devices` lists a device long before it can install anything, and
    installing too early fails in ways that look like build errors. This is the
    property that actually means ready.
    """
    return run([ADB, "-s", serial, "shell", "getprop",
                "sys.boot_completed"]).stdout.strip() == "1"


LOCK_NAMES = ("hardware-qemu.ini.lock", "multiinstance.lock",
              "userdata-qemu.img.lock", "cache.img.lock", "sdcard.img.lock")


def emulator_running() -> bool:
    out = (run(["tasklist", "/fo", "csv", "/nh"]) if IS_WINDOWS
           else run(["ps", "-A"])).stdout.lower()
    return "qemu-system" in out or "emulator" in out


def clear_stale_locks(avd: str) -> list[str]:
    """Remove locks left by an emulator that died without cleaning up.

    Only when nothing is running: a lock held by a LIVE emulator is doing its
    job, and deleting it corrupts the device image.

    Cheap insurance, not a diagnosis. It is NOT a cure for an accelerator that
    refuses to start - see accel_hint().
    """
    if emulator_running():
        return []
    home = os.environ.get("ANDROID_AVD_HOME") or os.path.join(
        os.path.expanduser("~"), ".android", "avd")
    directory = os.path.join(home, avd + ".avd")
    if not os.path.isdir(directory):
        return []

    removed = []
    for name in LOCK_NAMES:
        path = os.path.join(directory, name)
        if not os.path.exists(path):
            continue
        try:
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
            removed.append(name)
        except OSError:
            pass  # Still held; let the emulator complain rather than half-clear.
    return removed


def accel_hint() -> str:
    """Advice for when the emulator will not start.

    Each OS uses a different hypervisor and fails differently, so one generic
    message would be useless on two platforms out of three.
    """
    if IS_WINDOWS:
        return ("Windows uses WHPX. 'Failed to setup partition, hr=80070005'\n"
                "  means something else holds the hypervisor - usually Core\n"
                "  isolation / memory integrity being on, or WSL2, Docker,\n"
                "  VirtualBox or VMware. Note that `emulator -accel-check`\n"
                "  still reports WHPX as usable while this happens, so it is\n"
                "  not proof the hypervisor is actually free.")
    if IS_MAC:
        return ("macOS uses the Hypervisor framework. On Apple Silicon, check\n"
                "  the AVD uses an arm64-v8a system image - an x86_64 image has\n"
                "  to be fully emulated and rarely boots inside the timeout.")
    return ("Linux uses KVM. Check /dev/kvm exists and is writable by you:\n"
            "    ls -l /dev/kvm\n"
            "    groups | grep -q kvm || sudo usermod -aG kvm $USER\n"
            "  A group change only takes effect after logging back in.")


def host_memory_mb() -> int:
    """Total host RAM in MB, or 0 if it cannot be determined."""
    try:
        if IS_WINDOWS:
            out = run(["powershell", "-NoProfile", "-Command",
                       "(Get-CimInstance Win32_ComputerSystem)"
                       ".TotalPhysicalMemory"]).stdout.strip()
            return int(out) // (1024 * 1024) if out.isdigit() else 0
        if IS_MAC:
            out = run(["sysctl", "-n", "hw.memsize"]).stdout.strip()
            return int(out) // (1024 * 1024) if out.isdigit() else 0
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def suggested_memory_mb() -> int:
    """How much RAM to give the emulator.

    The AVD's own setting is fixed regardless of the machine - often 2 GB,
    which genuinely does starve a media-heavy app - while tens of gigabytes of
    host RAM sit unused.

    This is HEADROOM, not a fix for an observed problem. On the machine this
    was written on the guest had 6 GB with 4 GB available and no low-memory
    kills at all, so nothing here was starving.

    Worth knowing where the real ceiling is: the per-app Dalvik heap growth
    limit measured 192 MB against a 576 MB maximum, and that is what a Flutter
    app decoding camera frames or video actually hits. Raising the emulator's
    total RAM does not move it.

    A quarter of host RAM, clamped: below 2 GB Android itself struggles, and
    above 8 GB there is nothing useful left for the guest to do with it.
    """
    total = host_memory_mb()
    if not total:
        return 0                      # unknown - leave the AVD's own setting
    return max(2048, min(8192, total // 4))


def _emulator_args(avd: str, port: int, headless: bool,
                   memory_mb: int = 0) -> list[str]:
    args = ["-avd", avd, "-port", str(port),
            # Host GPU: software rendering is too slow to judge a UI on, and on
            # an x86_64 image without acceleration the emulator segfaults.
            "-gpu", "host",
            # Fresh boot. A snapshot can restore a device predating the code
            # under test, producing results that make no sense.
            "-no-snapshot-load", "-no-boot-anim",
            # Without this, a crashing emulator pops a modal "send a crash
            # report?" dialog and then sits there. The process stays alive, so
            # nothing looks wrong from outside - it just never boots and never
            # exits, which reads as an unexplained timeout. This is the single
            # most confusing failure mode the emulator has.
            "-no-metrics"]
    if memory_mb:
        args += ["-memory", str(memory_mb)]
    if headless:
        args.append("-no-window")
    return args


def make_responsive(serial: str, width: int, height: int, density: int) -> None:
    """Lower the guest's render resolution.

    The single biggest lever on emulator smoothness once the GPU is already
    hardware-accelerated. A 1080x2400 guest renders 2.6 million pixels per
    frame and copies them to the host window; 720x1600 is 1.15 million - well
    under half the work, for a display you are only using to check that a
    layout is right.

    Fully reversible and does not touch the AVD: `adb shell wm size reset`
    (and `wm density reset`) puts it back, and so does rebooting the device.
    """
    run([ADB, "-s", serial, "shell", "wm", "size", f"{width}x{height}"])
    run([ADB, "-s", serial, "shell", "wm", "density", str(density)])


def reset_display(serial: str) -> None:
    run([ADB, "-s", serial, "shell", "wm", "size", "reset"])
    run([ADB, "-s", serial, "shell", "wm", "density", "reset"])


def defender_advice() -> str:
    """Whether Windows Defender is scanning the AVD, and how to stop it.

    Every disk write the emulator makes to its qcow2 images goes through
    real-time scanning, which is one of the largest and least obvious causes of
    a sluggish emulator. Adding an exclusion is a change to AV coverage, so
    this only ever prints the command - it never applies it.
    """
    if not IS_WINDOWS:
        return ""
    avd_home = os.path.join(os.path.expanduser("~"), ".android")
    result = run(["powershell", "-NoProfile", "-Command",
                  "$p=(Get-MpPreference).ExclusionPath; "
                  "if ($p -contains '" + avd_home + "') {'yes'} else {'no'}"])
    if result.stdout.strip() == "yes":
        return ""
    return (
        "Windows Defender is scanning every disk write the emulator makes.\n"
        "  That is one of the biggest causes of a sluggish emulator. To exclude\n"
        "  the AVD directory, in an ADMIN PowerShell:\n\n"
        f"      Add-MpPreference -ExclusionPath \"{avd_home}\"\n\n"
        "  It reduces antivirus coverage for that folder, so it is your call -\n"
        "  this script will not do it for you.")


def boot(avd: str, port: int, headless: bool,
         elevate: bool = False, memory_mb: int = 0) -> Optional[subprocess.Popen]:
    """Start the emulator, optionally with a UAC prompt.

    Elevation exists because WHPX can fail to create its partition with
    ACCESS_DENIED (hr=80070005) under a UAC-FILTERED admin token. Being in the
    Administrators group is not the same as holding those rights: an
    unelevated process on an admin account carries the group as
    "deny only", and the hypervisor call can refuse on exactly that.

    Elevating is worth trying BEFORE weakening anything - in particular before
    turning off Core isolation, which is a real protection and should not be
    the first thing you reach for.

    Windows only; on macOS and Linux the accelerator does not work this way.
    """
    args = _emulator_args(avd, port, headless, memory_mb)

    if elevate and IS_WINDOWS:
        emu_dir = os.path.dirname(EMULATOR)
        quoted = ",".join(f"'{a}'" for a in args)
        # Start-Process -Verb RunAs is what raises the consent dialog. It needs
        # an interactive desktop, so this only works from a real terminal - it
        # reports a misleading "path not found" when run headless.
        script = (f"Start-Process -FilePath '{EMULATOR}' "
                  f"-WorkingDirectory '{emu_dir}' "
                  f"-ArgumentList {quoted} -Verb RunAs")
        print("  requesting elevation - approve the UAC prompt")
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        if result.returncode != 0:
            print("  elevation failed or was declined:")
            print("    " + (result.stderr or "").strip().splitlines()[0]
                  if result.stderr else "    (no detail)")
        return None  # elevated child is detached; we poll adb for it instead

    # Keep the emulator's output. Throwing it away is why a failed boot used to
    # look like an unexplained 6-minute timeout: the emulator says exactly what
    # went wrong ("WHPX: Failed to setup partition, hr=80070005") and we were
    # discarding it.
    log = open(emulator_log_path(), "wb")
    return subprocess.Popen([EMULATOR] + args, stdout=log, stderr=log)


def emulator_log_path() -> str:
    return os.path.join(REPO, ".emulator.log")


def kill_emulators() -> int:
    """Kill emulator/qemu processes we are giving up on.

    Necessary, not tidiness. A crashed emulator blocked on its consent dialog
    keeps running - well over a gigabyte of it - and keeps the AVD locked, so
    every later attempt inherits a stale lock and a busy device. Leaving one
    behind on each failure is how you end up with several.
    """
    names = ["qemu-system-x86_64", "qemu-system-aarch64", "emulator"]
    killed = 0
    for name in names:
        if IS_WINDOWS:
            result = run(["taskkill", "/F", "/T", "/IM", name + ".exe"])
        else:
            result = run(["pkill", "-f", name])
        if result.returncode == 0:
            killed += 1
    return killed


def emulator_crashed() -> bool:
    """Whether the emulator has crashed and is blocked on its consent dialog.

    Worth detecting separately from "the process died": here the process is
    still alive, so every other check says things are fine while it waits for
    a click that is never coming.
    """
    path = emulator_log_path()
    if not os.path.exists(path):
        return False
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return False
    return "crashdialog" in text or "Storing crashdata" in text


def emulator_complaint() -> str:
    """The most useful lines the emulator printed before giving up."""
    path = emulator_log_path()
    if not os.path.exists(path):
        return ""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    wanted = ("WHPX", "HAXM", "KVM", "hvf", "Failed", "failed", "ERROR",
              "cannot", "Cannot", "not found", "permission", "Permission")
    hits = [l.strip() for l in text.splitlines()
            if any(w in l for w in wanted) and "INFO" not in l[:12]]
    # Keep the last few: the fatal one is at the end, not the start.
    return "\n".join(f"    {l}" for l in hits[-6:])


def wait_for(count: int, started: int = 0) -> tuple[list[str], str]:
    """(devices, failure_reason). An empty list means it did not come up.

    Returns rather than exiting so the caller can decide what to do next -
    which is what makes the automatic elevation retry possible.
    """
    deadline = time.time() + BOOT_TIMEOUT_S
    seen: list[str] = []
    while time.time() < deadline:
        seen = [s for s in serials() if booted(s)]
        if len(seen) >= count:
            print(" " * 72, end="\r")
            return seen[:count], ""

        # The emulator's worst failure mode: it crashes, pops a modal "send a
        # crash report?" dialog, and waits forever. The process stays alive, so
        # nothing looks wrong - it simply never boots. -no-metrics prevents it,
        # but an emulator started some other way can still land here, so detect
        # it rather than sitting through the full timeout.
        if started and emulator_crashed():
            print(" " * 72, end="\r")
            return [], "crashed and is waiting on a crash-report dialog"

        # Fail fast when the emulator has died. Waiting the full six minutes
        # for a process that exited thirty seconds ago tells you nothing.
        if started and not emulator_running() and time.time() - (
                deadline - BOOT_TIMEOUT_S) > 15:
            print(" " * 72, end="\r")
            return [], "started and then exited"

        print(f"  waiting for {count - len(seen)} more device(s)… "
              f"{int(deadline - time.time())}s left", end="\r")
        time.sleep(POLL_S)

    print(" " * 72, end="\r")
    return [], f"did not boot within {BOOT_TIMEOUT_S}s"


def report_boot_failure(reason: str, tried_elevated: bool) -> None:
    print(f"The emulator {reason}.\n")
    complaint = emulator_complaint()
    if complaint:
        print("  It said:\n" + complaint + "\n")
    print("  " + accel_hint())
    if IS_WINDOWS and tried_elevated:
        print("\n  Elevation was tried and did not help, so this is not a "
              "token problem.\n  The remaining likely cause is Core isolation "
              "/ memory integrity holding\n  the hypervisor. Turning that off "
              "weakens the machine, so it is your\n  call - or use a real "
              "phone over USB, which needs no hypervisor at all.")
    print(f"\n  Full log: {emulator_log_path()}")


def app_id() -> str:
    """The applicationId, read out of Gradle rather than assumed."""
    for name in ("build.gradle.kts", "build.gradle"):
        path = os.path.join(REPO, "android", "app", name)
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8", errors="replace"):
            if "applicationId" in line and '"' in line:
                return line.split('"')[1]
    sys.exit("Could not read applicationId from android/app/build.gradle[.kts].")


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-b", "--branch", help="branch to run (skips the prompt)")
    ap.add_argument("--no-checkout", action="store_true",
                    help="use the working tree exactly as it is")
    ap.add_argument("-n", "--count", type=int, default=1,
                    help="how many devices (default 1)")
    ap.add_argument("--avd", help="AVD name (default: the first one)")
    ap.add_argument("--release", action="store_true", help="release build")
    ap.add_argument("--clean", action="store_true", help="flutter clean first")
    ap.add_argument("--headless", action="store_true", help="no emulator window")
    ap.add_argument("--elevate", action="store_true",
                    help="Windows: start elevated from the outset. Not usually "
                         "needed - a failed boot is retried elevated "
                         "automatically")
    ap.add_argument("--reverse", type=int, metavar="PORT",
                    help="adb reverse tcp:PORT on every device, so the app can "
                         "reach a server bound to loopback on this host")
    ap.add_argument("--fast", action="store_true",
                    help="lower the guest render resolution to 720x1600 for a "
                         "much smoother emulator. Reversible: --reset-display, "
                         "or reboot the device")
    ap.add_argument("--reset-display", action="store_true",
                    help="undo --fast and exit")
    ap.add_argument("--memory", type=int, metavar="MB",
                    help="RAM for the emulator (default: a quarter of host "
                         "RAM, clamped to 2-8 GB; 0 keeps the AVD's setting)")
    ap.add_argument("--project", metavar="PATH",
                    help="the Flutter project to run (default: the current "
                         "directory, or the nearest parent with a pubspec.yaml)")
    ap.add_argument("--list", action="store_true",
                    help="show branches and AVDs, then exit")
    args = ap.parse_args()

    global REPO
    REPO = find_project(args.project)

    if args.reset_display:
        for serial in serials():
            reset_display(serial)
            print(f"display reset on {serial}")
        return 0

    print(f"host:    {platform.system()} {platform.machine()}")
    print(f"project: {REPO}")
    print(f"sdk:     {SDK}")
    print(f"flutter: {FLUTTER}")

    available = avds()
    if args.list:
        options, current = branches()
        print("\nBranches:")
        for b in options:
            print(f"  {'*' if b == current else ' '} {b:<40} {describe(b)}")
        print("\nAVDs:")
        print("\n".join("    " + a for a in available) or "    (none)")
        return 0

    if not available:
        sys.exit("No AVDs. Create one in Android Studio's Device Manager.")

    # ---- branch ----
    _, current = branches()
    if args.no_checkout:
        print(f"\nUsing the working tree as-is (on {current}).")
    else:
        target = args.branch or pick_branch(current)
        if target and not checkout(target, current):
            return 1

    # ---- devices ----
    avd = args.avd or available[0]
    if avd not in available:
        sys.exit(f"No such AVD: {avd}. Available: {', '.join(available)}")

    memory_mb = suggested_memory_mb() if args.memory is None else args.memory
    if memory_mb:
        print(f"giving the emulator {memory_mb} MB "
              f"(host has {host_memory_mb()} MB)")

    stale = clear_stale_locks(avd)
    if stale:
        print(f"cleared {len(stale)} stale lock(s): {', '.join(stale)}")

    already = [s for s in serials() if booted(s)]
    if already:
        print(f"{len(already)} device(s) already up: {', '.join(already)}")

    def attempt(elevate: bool) -> tuple[list[str], str]:
        here = [s for s in serials() if booted(s)]
        to_start = max(0, args.count - len(here))
        for i in range(to_start):
            # Emulator console ports must be even, conventionally from 5554.
            port = 5554 + 2 * (len(here) + i)
            print(f"booting {avd} on port {port}…")
            boot(avd, port, args.headless, elevate=elevate,
                 memory_mb=memory_mb)
        return wait_for(args.count, started=to_start)

    devices, reason = attempt(args.elevate)

    # Retry elevated automatically rather than making you read an error and
    # run the same command again with a flag. WHPX can refuse to create its
    # partition under a UAC-FILTERED admin token - being IN the Administrators
    # group is not the same as holding those rights - and elevation is the
    # cheap thing to rule out before anyone starts turning off Core isolation.
    tried_elevated = args.elevate
    if not devices and IS_WINDOWS and not args.elevate:
        print(f"\nThe emulator {reason}. Retrying with elevation - "
              "approve the UAC prompt.\n")
        # Clear the log so the second attempt's complaint is not confused with
        # the first one's.
        # The crashed emulator is still running and still holding the AVD.
        # Clear it out or the retry inherits a locked device.
        kill_emulators()
        try:
            os.remove(emulator_log_path())
        except OSError:
            pass
        clear_stale_locks(avd)
        tried_elevated = True
        devices, reason = attempt(elevate=True)

    if not devices:
        # Do not leave a crashed emulator running: it is over a gigabyte of
        # resident memory and it keeps the AVD locked for the next attempt.
        killed = kill_emulators()
        report_boot_failure(reason, tried_elevated)
        if killed:
            print("\n  (cleaned up the stuck emulator process)")
        return 1

    print(f"{len(devices)} device(s) ready: {', '.join(devices)}")

    if args.fast:
        for serial in devices:
            make_responsive(serial, 720, 1600, 320)
        print("lowered the guest display to 720x1600 @320dpi "
              "(undo: --reset-display)")

    advice = defender_advice()
    if advice:
        print("\n  " + advice + "\n")

    if args.clean:
        print("flutter clean…")
        subprocess.run([FLUTTER, "clean"], cwd=REPO)

    if args.reverse:
        for serial in devices:
            # Inside an emulator 127.0.0.1 is the EMULATOR's loopback, not the
            # host's, so a server bound to localhost here is otherwise
            # unreachable from the app.
            run([ADB, "-s", serial, "reverse",
                 f"tcp:{args.reverse}", f"tcp:{args.reverse}"])
        print(f"adb reverse tcp:{args.reverse} on every device")

    mode = "--release" if args.release else "--debug"

    # One device gets an interactive `flutter run`, because hot reload is the
    # whole point of a single-device session. Several devices cannot share one
    # stdin, so they are installed and launched instead - which leaves both
    # actually usable rather than one hanging on a prompt it cannot answer.
    if len(devices) == 1:
        print(f"\n=== flutter run {mode} on {devices[0]} ===")
        return subprocess.run([FLUTTER, "run", mode, "-d", devices[0]],
                              cwd=REPO).returncode

    package = app_id()
    failures = 0
    for serial in devices:
        print(f"\n=== install + launch on {serial} ({mode}) ===")
        if subprocess.run([FLUTTER, "install", mode, "-d", serial],
                          cwd=REPO).returncode != 0:
            failures += 1
            print(f"!! install failed on {serial}")
            continue
        run([ADB, "-s", serial, "shell", "monkey", "-p", package,
             "-c", "android.intent.category.LAUNCHER", "1"])
        print(f"launched {package}")

    if not failures:
        print(f"\n{len(devices)} devices running {package}. Hot reload is not "
              f"available with more than one - re-run to push a change.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
