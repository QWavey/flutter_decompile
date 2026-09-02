"""
flutter_decompile.cli -- argparse entry point.

    python -m flutter_decompile <input> [options]

<input> is an .apk / .apks / .xapk / .aab, a lib/<abi> directory, a bare
libapp.so, or an existing Blutter output directory.

Honesty is enforced structurally here: the CLI refuses to pretend that the
unimplemented stages ran.  ``--emit`` exits non-zero with a pointer to this
docstring rather than writing plausible-looking Dart.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from . import CAPABILITY, IMPLEMENTED_STAGES, UNIMPLEMENTED_STAGES, __version__
from . import apk as apk_mod
from . import blutter_driver as bd
from . import parse_asm as pa


# --------------------------------------------------------------------------- #

def _skeleton_match(pattern: str, url: str) -> bool:
    """Match a library url against --skeleton.

    Accepts a glob (``**/panic.dart``, ``*/server/*``) or a plain substring,
    because both are things people reasonably type and neither is ambiguous:
    a pattern with no wildcard is a substring test, and one with wildcards is
    matched against the url and against its path tail, so a leading ``**/``
    behaves the way it does in a shell.
    """
    if not pattern:
        return True
    if not any(ch in pattern for ch in "*?["):
        return pattern in url
    tail = url.split(":", 1)[-1]
    return (fnmatch.fnmatch(url, pattern)
            or fnmatch.fnmatch(tail, pattern)
            or fnmatch.fnmatch(tail, pattern.lstrip("*/")))


def _url_to_path(url: str) -> str:
    """`package:chat/features/panic/domain/panic.dart` -> a relative path."""
    tail = url.split(":", 1)[-1]
    parts = [seg for seg in tail.split("/") if seg not in ("", ".", "..")]
    return os.path.join(*parts) if parts else "unnamed.dart"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="flutter_decompile",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=CAPABILITY,
        epilog="Implemented stages: %s.  Not yet implemented: %s."
               % (", ".join(IMPLEMENTED_STAGES), ", ".join(UNIMPLEMENTED_STAGES)))

    ap.add_argument("input", nargs="?",
                    help=".apk/.apks/.xapk/.aab, a lib/<abi> dir, a libapp.so, "
                         "or an existing Blutter output directory")
    ap.add_argument("--version", action="version", version="flutter_decompile " + __version__)
    ap.add_argument("--capability", action="store_true",
                    help="print the capability statement and exit")

    core = ap.add_argument_group("core")
    core.add_argument("-o", "--out", metavar="DIR",
                      help="output root (default ./<name>_reconstructed)")
    core.add_argument("--abi", default=apk_mod.DEFAULT_ABI, choices=list(apk_mod.KNOWN_ABIS))
    core.add_argument("--packages", metavar="PKG[,PKG...]",
                      help="only reconstruct these root packages (e.g. chat)")
    core.add_argument("--include-deps", action="store_true",
                      help="also process pub packages (large)")

    bl = ap.add_argument_group("blutter")
    bl.add_argument("--blutter", metavar="PATH", help="path to blutter.py")
    bl.add_argument("--blutter-out", metavar="DIR",
                    help="reuse an existing Blutter output; skip running it")
    bl.add_argument("--no-blutter", action="store_true",
                    help="fail unless --blutter-out is supplied (CI / air-gapped)")
    bl.add_argument("--dart-version", metavar="X.Y.Z")
    bl.add_argument("--snapshot-hash", metavar="HEX")
    bl.add_argument("--no-cmake-patch", action="store_true",
                    help="do not apply the CMake 4.x compatibility patches")
    bl.add_argument("--no-msvc-env", action="store_true",
                    help="do not wrap the Blutter build in an MSVC environment")
    bl.add_argument("--preflight", action="store_true",
                    help="report Blutter build prerequisites and exit")

    rec = ap.add_argument_group("reconstruction")
    rec.add_argument("--infer-fields", choices=("off", "safe", "aggressive"), default="safe",
                     help="(stage 5 -- NOT IMPLEMENTED in 0.1)")
    rec.add_argument("--no-bodies", action="store_true",
                     help="skeleton only; skip body-fact extraction (much faster)")
    rec.add_argument("--body-events", action="store_true",
                     help="retain the full ordered semantic event list per method "
                          "(high memory)")
    rec.add_argument("--emit", action="store_true",
                     help="(stage 6 -- NOT IMPLEMENTED in 0.1; exits non-zero)")

    rep = ap.add_argument_group("reporting")
    rep.add_argument("--report", choices=("json", "md", "both", "none"), default="both")
    rep.add_argument("--dump-model", metavar="FILE", help="write the full IR as JSON")
    rep.add_argument("--skeleton", metavar="GLOB", nargs="?", const="*",
                     help="skeletons for libraries matching GLOB (fnmatch "
                          "against the library url; a bare substring also "
                          "matches). Printed to stdout, and written under "
                          "-o/skeletons/ mirroring the package tree when -o "
                          "is given.")
    rep.add_argument("--strict", action="store_true",
                     help="exit non-zero if parse coverage is below 100%%")
    rep.add_argument("-v", "--verbose", action="count", default=0)
    return ap


def _log(verbose: int):
    def log(msg: str, level: int = 0) -> None:
        if verbose >= level:
            print(msg, file=sys.stderr)
    return log


# --------------------------------------------------------------------------- #

def main(argv: Optional[List[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])
    log = _log(args.verbose)

    if args.capability:
        print(CAPABILITY)
        return 0
    if args.preflight:
        print(json.dumps(bd.preflight(args.blutter), indent=2))
        return 0
    if not args.input and not args.blutter_out:
        ap.error("an input is required (or --blutter-out)")

    started = time.time()
    report: Dict[str, Any] = {
        "tool": "flutter_decompile", "version": __version__,
        "implemented_stages": list(IMPLEMENTED_STAGES),
        "unimplemented_stages": list(UNIMPLEMENTED_STAGES),
        "blutter_format_fingerprint": pa.BLUTTER_FORMAT_FINGERPRINT,
    }

    src = args.input or args.blutter_out
    name = os.path.splitext(os.path.basename(os.path.abspath(src)))[0]
    out_root = os.path.abspath(args.out or ("./%s_reconstructed" % name))
    os.makedirs(out_root, exist_ok=True)
    report["out_root"] = out_root

    for w in apk_mod.check_abi_support(args.abi):
        log("[warn] " + w)
        report.setdefault("warnings", []).append(w)

    # ---- stage 0 + 1 ------------------------------------------------------ #
    acq: Optional[apk_mod.Acquired] = None
    if args.input:
        try:
            acq = apk_mod.acquire(args.input, os.path.join(out_root, "_work"), args.abi)
        except apk_mod.AcquireError as e:
            print("acquire failed: %s" % e, file=sys.stderr)
            return 2
        report["acquire"] = acq.to_dict()
        for n in acq.notes:
            log("[acquire] " + n)

        if acq.source_kind != "blutter_out":
            vi = apk_mod.identify(acq, args.dart_version, args.snapshot_hash)
            report["identify"] = vi.to_dict()
            log("[identify] dart=%s hash=%s (%s)"
                % (vi.dart_version, vi.snapshot_hash, vi.winner))
            for w in vi.warnings:
                log("[warn] " + w)
        else:
            vi = apk_mod.VersionInfo()
            report["identify"] = {"skipped": "input is an existing Blutter output"}
    else:
        vi = apk_mod.VersionInfo()

    # ---- stage 2 ---------------------------------------------------------- #
    blutter_out = args.blutter_out or (acq.blutter_out if acq else None)
    try:
        if blutter_out:
            run = bd.adopt(blutter_out)
            log("[blutter] adopted %s" % run.out_dir)
        elif args.no_blutter:
            print("--no-blutter was given but --blutter-out was not.", file=sys.stderr)
            return 2
        else:
            assert acq is not None and acq.libapp
            run = bd.run_blutter(
                acq.libapp, os.path.join(out_root, "blutter_out"),
                blutter_py=args.blutter,
                dart_version=vi.dart_version, snapshot_hash=vi.snapshot_hash,
                patch_cmake=not args.no_cmake_patch,
                use_msvc_env=not args.no_msvc_env,
                log=lambda m: log(m, 0))
    except bd.BlutterError as e:
        print("blutter stage failed: %s" % e, file=sys.stderr)
        return 3
    report["blutter"] = run.to_dict()

    # ---- stage 3 + 4 ------------------------------------------------------ #
    asm_root = os.path.join(run.out_dir, "asm")
    packages = args.packages.split(",") if args.packages else None
    if packages is None and not args.include_deps:
        packages = guess_app_packages(asm_root)
        if packages:
            log("[parse] app package(s): %s  (use --include-deps for all %d)"
                % (", ".join(packages), len(os.listdir(asm_root))))

    t0 = time.time()
    prog = pa.parse_tree(asm_root, packages=packages,
                         collect_bodies=not args.no_bodies,
                         body_events=args.body_events,
                         progress=lambda s: log("[parse] " + s, 1))
    parse_secs = time.time() - t0

    cov = pa.coverage(prog)
    obf = pa.probe_obfuscation(prog)
    report["parse"] = dict(cov, seconds=round(parse_secs, 2),
                           packages_selected=packages or "all")
    report["obfuscation"] = obf
    if obf["obfuscated"]:
        msg = ("Names are obfuscated (%.0f%% of sampled method names are 1-3 "
               "characters). Everything below is STRUCTURE ONLY."
               % (obf["short_name_ratio"] * 100))
        report.setdefault("warnings", []).insert(0, msg)
        print("!! " + msg, file=sys.stderr)

    # ---- output ----------------------------------------------------------- #
    if args.skeleton:
        pat = "" if args.skeleton == "*" else args.skeleton
        matched = [lib for lib in prog.libraries
                   if _skeleton_match(pat, lib.url)]

        # A tree on disk is what makes this usable across a whole APK: 266
        # files of skeleton scrolling past a terminal is not readable by
        # anyone.
        #
        # Deliberately named apart from out_root and derived FROM it. This used
        # to rebind out_root itself, which set it to None whenever -o was
        # omitted and then crashed the report writer below on a path join.
        # Deriving it also means --skeleton writes a tree without -o, into the
        # same default directory as everything else, instead of silently
        # printing and saving nothing.
        skeleton_root = os.path.join(out_root, "skeletons")

        for lib in matched:
            text = pa.render_skeleton(lib)
            print()
            print(text)
            if skeleton_root:
                dest = os.path.join(skeleton_root, _url_to_path(lib.url))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w", encoding="utf-8") as fh:
                    fh.write(text + "\n")

        if skeleton_root and matched:
            log("[out] %d skeleton(s) -> %s" % (len(matched), skeleton_root))
        if not matched:
            log("[skeleton] nothing matched %r" % (args.skeleton,))

    if args.dump_model:
        with open(args.dump_model, "w", encoding="utf-8") as fh:
            json.dump({"report": report,
                       "libraries": [l.to_dict() for l in prog.libraries]},
                      fh, indent=1)
        log("[out] model -> %s" % args.dump_model)

    report["elapsed_seconds"] = round(time.time() - started, 2)

    if args.report in ("json", "both"):
        p = os.path.join(out_root, "report.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        log("[out] %s" % p)
    if args.report in ("md", "both"):
        p = os.path.join(out_root, "report.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(render_report_md(report, prog))
        log("[out] %s" % p)

    if args.report != "none":
        print(summary_line(report))

    if args.emit:
        print("\n--emit: stage 6 (Dart source emission) is NOT IMPLEMENTED in "
              "%s.\nThe parsed model is available via --dump-model and "
              "--skeleton.\nThis tool will not write plausible-looking Dart it "
              "cannot justify." % __version__, file=sys.stderr)
        return 4

    if args.strict and cov["unparsed_lines"] > 0:
        print("--strict: %d unparsed lines (parse coverage %.6f)"
              % (cov["unparsed_lines"], cov["parse_coverage"]), file=sys.stderr)
        return 5
    return 0


# --------------------------------------------------------------------------- #

SDK_AND_PUB_HINTS = {
    "dart", "flutter", "flutter_localizations", "sky_engine", "collection",
    "async", "meta", "path", "characters", "vector_math", "material_color_utilities",
}


def guess_app_packages(asm_root: str) -> List[str]:
    """The app's own package(s): a top-level asm dir that is neither an SDK
    library nor a pub dependency.  Heuristic -- INFERRED, and the report says
    which packages were selected so the user can override with --packages."""
    if not os.path.isdir(asm_root):
        return []
    entries = sorted(d for d in os.listdir(asm_root)
                     if os.path.isdir(os.path.join(asm_root, d)))
    cands = [d for d in entries if d not in SDK_AND_PUB_HINTS]
    # The app package almost always owns main.dart.
    owns_main = [d for d in cands
                 if os.path.isfile(os.path.join(asm_root, d, "main.dart"))]
    return owns_main or cands[:1]


def summary_line(report: Dict[str, Any]) -> str:
    p = report.get("parse", {})
    return ("parsed %s files / %s classes / %s methods in %s packages; "
            "parse coverage %.4f%% (%s unparsed lines); "
            "field names RECOVERED %s, DESTROYED %s"
            % (p.get("files"), p.get("classes"), p.get("methods"),
               p.get("packages"), 100.0 * p.get("parse_coverage", 0),
               p.get("unparsed_lines"), p.get("fields_named_RECOVERED"),
               p.get("fields_name_DESTROYED")))


def render_report_md(report: Dict[str, Any], prog: pa.Program) -> str:
    p = report.get("parse", {})
    L: List[str] = []
    L.append("# flutter_decompile report")
    L.append("")
    L.append("> **This is not decompiled Dart.** It is a structural "
             "reconstruction with documented holes. See the capability "
             "statement (`--capability`).")
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append("| tool version | %s |" % report.get("version"))
    L.append("| Blutter format fingerprint | `%s` |" % report.get("blutter_format_fingerprint"))
    ident = report.get("identify") or {}
    L.append("| Dart version | %s |" % (ident.get("dart_version") or "_not determined_"))
    L.append("| snapshot hash | `%s` |" % (ident.get("snapshot_hash") or "_not determined_"))
    L.append("| version signal used | %s |" % (ident.get("winner") or "_n/a_"))
    L.append("| obfuscated | %s |" % report.get("obfuscation", {}).get("obfuscated"))
    L.append("| elapsed | %ss |" % report.get("elapsed_seconds"))
    L.append("")

    for w in report.get("warnings", []):
        L.append("> **Warning.** %s" % w)
    L.append("")

    L.append("## Parse coverage")
    L.append("")
    L.append("| metric | value |")
    L.append("|---|---:|")
    for k in ("files", "packages", "lines", "unparsed_lines", "classes",
              "fields", "methods", "methods_with_body", "call_graph_edges",
              "indexed_addresses"):
        L.append("| %s | %s |" % (k, p.get(k)))
    L.append("| parse_coverage | %.6f |" % p.get("parse_coverage", 0))
    L.append("")

    L.append("## Confidence ledger")
    L.append("")
    L.append("| fact | count | confidence |")
    L.append("|---|---:|---|")
    L.append("| field names read from a pool `Field <...>` entry | %s | RECOVERED |"
             % p.get("fields_named_RECOVERED"))
    L.append("| field names that only exist as an offset | %s | **DESTROYED** |"
             % p.get("fields_name_DESTROYED"))
    L.append("| method return types recorded | %s | RECOVERED |"
             % ((p.get("methods") or 0) - (p.get("methods_return_type_UNKNOWN") or 0)))
    L.append("| method return types absent (`_`) | %s | UNKNOWN |"
             % p.get("methods_return_type_UNKNOWN"))
    L.append("| positional parameter types absent | %s | UNKNOWN |"
             % p.get("methods_param_types_UNKNOWN"))
    L.append("| positional parameter names | %s | **DESTROYED** |" % p.get("methods"))
    L.append("")

    L.append("## Packages")
    L.append("")
    L.append("| package | libraries |")
    L.append("|---|---:|")
    for pkg, n in sorted(prog.packages().items(), key=lambda kv: -kv[1]):
        L.append("| %s | %d |" % (pkg, n))
    L.append("")

    unparsed = [(l.url, u) for l in prog.libraries for u in l.unparsed]
    L.append("## Unparsed lines (%d)" % len(unparsed))
    L.append("")
    if not unparsed:
        L.append("None. Every structure line in the selected packages matched a "
                 "documented rule.")
    else:
        L.append("These are lines the parser did not recognise. They are "
                 "reported rather than guessed at.")
        L.append("")
        L.append("```")
        for url, u in unparsed[:100]:
            L.append("%s:%d  %s" % (url, u.lineno, u.text[:160]))
        if len(unparsed) > 100:
            L.append("... and %d more" % (len(unparsed) - 100))
        L.append("```")
    L.append("")

    L.append("## Not implemented in this version")
    L.append("")
    for s in UNIMPLEMENTED_STAGES:
        L.append("- **%s** -- no output is produced for this stage. The tool "
                 "exits non-zero rather than emitting unjustified code." % s)
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    raise SystemExit(main())
