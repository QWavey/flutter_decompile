"""cli.py -- argument handling and the pieces of the run that are pure.

The expensive stages are not exercised here. Everything below either tests a
pure function or drives cli.main() against the adopted-Blutter-output path,
which needs no Blutter, no APK and no network.
"""

from __future__ import annotations

import json
import os

import pytest

from flutter_decompile import cli
from flutter_decompile import parse_asm as pa

from conftest import write_asm

URL = "package:chat/features/panic/domain/panic.dart"


# --------------------------------------------------------------------------- #
# _skeleton_match -- glob or substring, and never a third thing
# --------------------------------------------------------------------------- #

def test_an_empty_pattern_matches_everything():
    assert cli._skeleton_match("", URL) is True
    assert cli._skeleton_match("", "anything at all") is True


@pytest.mark.parametrize("pattern", ["panic", "features/panic", "package:chat",
                                     ".dart"])
def test_a_pattern_with_no_wildcard_is_a_substring_test(pattern):
    assert cli._skeleton_match(pattern, URL) is True


@pytest.mark.parametrize("pattern", ["nope", "panic.dartx", "PANIC"])
def test_a_substring_that_is_not_there_does_not_match(pattern):
    assert cli._skeleton_match(pattern, URL) is False


def test_substring_matching_is_case_sensitive():
    """Dart library urls are case sensitive, so matching them must be too."""
    assert cli._skeleton_match("Panic", URL) is False


@pytest.mark.parametrize("pattern", [
    "**/panic.dart",        # the shell-shaped one people type first
    "*/domain/*",
    "*.dart",
    "package:chat/*",
    "chat/*",               # matched against the path tail
    "*panic*",
])
def test_a_pattern_with_wildcards_is_globbed(pattern):
    assert cli._skeleton_match(pattern, URL) is True


@pytest.mark.parametrize("pattern", ["*/nowhere/*", "*.txt", "flutter/*"])
def test_a_glob_that_does_not_fit_does_not_match(pattern):
    assert cli._skeleton_match(pattern, URL) is False


def test_a_leading_star_star_slash_behaves_like_a_shell():
    """`**/panic.dart` must match the file wherever it sits, including at the
    top of the package."""
    assert cli._skeleton_match("**/panic.dart", "package:chat/panic.dart") is True
    assert cli._skeleton_match("**/panic.dart", URL) is True


def test_the_character_class_form_is_treated_as_a_glob():
    assert cli._skeleton_match("[a-z]*", URL) is True
    assert cli._skeleton_match("[0-9]*", URL) is False


def test_a_dart_sdk_url_is_matched_on_its_tail_too():
    assert cli._skeleton_match("core*", "dart:core") is True
    assert cli._skeleton_match("dart:", "dart:core") is True


# --------------------------------------------------------------------------- #
# _url_to_path
# --------------------------------------------------------------------------- #

def test_url_to_path_drops_the_scheme_and_keeps_the_tree():
    assert cli._url_to_path(URL) == os.path.join(
        "chat", "features", "panic", "domain", "panic.dart")


def test_url_to_path_refuses_to_escape_the_output_directory():
    """A library url is untrusted text out of a snapshot; `..` in it must not
    write outside -o."""
    path = cli._url_to_path("package:../../etc/passwd")
    assert ".." not in path.split(os.sep)


def test_url_to_path_of_a_degenerate_url_still_yields_a_name():
    assert cli._url_to_path("package:") == "unnamed.dart"


# --------------------------------------------------------------------------- #
# the parser
# --------------------------------------------------------------------------- #

def test_the_documented_defaults():
    args = cli.build_parser().parse_args(["x.apk"])
    assert args.input == "x.apk"
    assert args.report == "both"
    assert args.infer_fields == "safe"
    assert args.emit is False
    assert args.strict is False
    assert args.no_bodies is False
    assert args.skeleton is None


def test_skeleton_without_a_value_means_everything():
    args = cli.build_parser().parse_args(["x.apk", "--skeleton"])
    assert args.skeleton == "*"
    args = cli.build_parser().parse_args(["x.apk", "--skeleton", "**/auth/*"])
    assert args.skeleton == "**/auth/*"


def test_an_unknown_abi_is_refused_rather_than_attempted():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["x.apk", "--abi", "mips"])


def test_an_unknown_infer_mode_is_refused():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["x.apk", "--infer-fields", "psychic"])


def test_the_help_leads_with_the_capability_statement():
    help_text = cli.build_parser().format_help()
    assert "does NOT decompile Dart" in help_text
    assert "DESTROYED" in help_text


def test_capability_prints_and_exits_zero(capsys):
    assert cli.main(["--capability"]) == 0
    out = capsys.readouterr().out
    assert "This tool does NOT decompile Dart" in out
    assert "instance field NAMES" in out


def test_running_with_no_input_at_all_is_an_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
    assert "an input is required" in capsys.readouterr().err


def test_no_blutter_without_an_output_to_adopt_fails_loudly(tmp_path, monkeypatch,
                                                            capsys):
    """--no-blutter is the CI / air-gapped switch: it must stop before it can
    build or fetch anything, not after."""
    from flutter_decompile import apk as apk_mod
    from flutter_decompile import blutter_driver as bd

    fake = apk_mod.Acquired(source="x.apk", source_kind="apk",
                            workdir=str(tmp_path), abi="arm64-v8a",
                            libapp=os.path.join(str(tmp_path), "libapp.so"))
    monkeypatch.setattr(apk_mod, "acquire", lambda *a, **k: fake)
    monkeypatch.setattr(apk_mod, "identify",
                        lambda *a, **k: apk_mod.VersionInfo())
    monkeypatch.setattr(bd, "run_blutter", lambda *a, **k: pytest.fail(
        "ran Blutter despite --no-blutter"))

    rc = cli.main(["x.apk", "--no-blutter", "-o", os.path.join(str(tmp_path), "o")])
    assert rc == 2
    assert "--no-blutter was given" in capsys.readouterr().err


def test_version_flag_reports_the_package_version(capsys):
    from flutter_decompile import __version__
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# guess_app_packages
# --------------------------------------------------------------------------- #

def test_the_app_package_is_the_one_that_owns_main_dart(tmp_path):
    root = os.path.join(str(tmp_path), "asm")
    write_asm(root, "chat/main.dart", "")
    write_asm(root, "flutter/src/widgets.dart", "")
    write_asm(root, "collection/src/x.dart", "")
    assert cli.guess_app_packages(root) == ["chat"]


def test_sdk_and_pub_packages_are_never_guessed_as_the_app(tmp_path):
    root = os.path.join(str(tmp_path), "asm")
    for pkg in ("dart", "flutter", "collection", "meta", "path"):
        write_asm(root, "%s/x.dart" % pkg, "")
    assert cli.guess_app_packages(root) == []


def test_without_a_main_dart_the_first_non_sdk_package_is_taken(tmp_path):
    root = os.path.join(str(tmp_path), "asm")
    write_asm(root, "zeta/x.dart", "")
    write_asm(root, "alpha/x.dart", "")
    write_asm(root, "flutter/x.dart", "")
    guessed = cli.guess_app_packages(root)
    assert guessed == ["alpha"], "the guess must be deterministic"


def test_a_missing_asm_directory_guesses_nothing(tmp_path):
    assert cli.guess_app_packages(os.path.join(str(tmp_path), "nope")) == []


# --------------------------------------------------------------------------- #
# reporting helpers
# --------------------------------------------------------------------------- #

def test_summary_line_names_recovered_and_destroyed_separately():
    report = {"parse": {"files": 2, "classes": 3, "methods": 4, "packages": 1,
                        "parse_coverage": 1.0, "unparsed_lines": 0,
                        "fields_named_RECOVERED": 1, "fields_name_DESTROYED": 5}}
    line = cli.summary_line(report)
    assert "field names RECOVERED 1, DESTROYED 5" in line
    assert "parse coverage 100.0000%" in line


def test_the_markdown_report_leads_with_the_disclaimer(asm_tree):
    prog = pa.parse_tree(asm_tree, packages=["chat"])
    report = {"version": "0.1.0", "parse": pa.coverage(prog),
              "obfuscation": pa.probe_obfuscation(prog),
              "blutter_format_fingerprint": pa.BLUTTER_FORMAT_FINGERPRINT}
    md = cli.render_report_md(report, prog)
    assert "**This is not decompiled Dart.**" in md.split("## ")[0]
    assert "| field names that only exist as an offset |" in md
    assert "**DESTROYED**" in md
    assert "| positional parameter names |" in md


def test_the_markdown_report_lists_unparsed_lines_rather_than_hiding_them(asm_file):
    lib = pa.parse_file(asm_file(
        "junk.dart", "// lib: , url: package:chat/j.dart\n%%% junk %%%\n"))
    prog = pa.Program(libraries=[lib])
    md = cli.render_report_md({"parse": pa.coverage(prog)}, prog)
    assert "## Unparsed lines (1)" in md
    assert "%%% junk %%%" in md


def test_the_markdown_report_says_when_nothing_was_missed(asm_tree):
    prog = pa.parse_tree(asm_tree, packages=["chat"])
    md = cli.render_report_md({"parse": pa.coverage(prog)}, prog)
    assert "## Unparsed lines (0)" in md
    assert "Every structure line in the selected packages matched" in md


def test_the_markdown_report_names_the_unimplemented_stages(asm_tree):
    from flutter_decompile import UNIMPLEMENTED_STAGES
    prog = pa.parse_tree(asm_tree, packages=["chat"])
    md = cli.render_report_md({"parse": pa.coverage(prog)}, prog)
    for stage in UNIMPLEMENTED_STAGES:
        assert "**%s**" % stage in md
    assert "exits non-zero rather than emitting unjustified code" in md


# --------------------------------------------------------------------------- #
# end to end over an adopted Blutter output
# --------------------------------------------------------------------------- #

def test_a_plain_run_writes_both_reports_and_returns_zero(tmp_path, blutter_out,
                                                          capsys):
    out = os.path.join(str(tmp_path), "out")
    assert cli.main(["--blutter-out", blutter_out, "-o", out]) == 0
    assert os.path.isfile(os.path.join(out, "report.json"))
    assert os.path.isfile(os.path.join(out, "report.md"))
    assert "parse coverage" in capsys.readouterr().out


def test_report_none_writes_no_report(tmp_path, blutter_out):
    out = os.path.join(str(tmp_path), "out")
    assert cli.main(["--blutter-out", blutter_out, "-o", out,
                     "--report", "none"]) == 0
    assert not os.path.exists(os.path.join(out, "report.json"))
    assert not os.path.exists(os.path.join(out, "report.md"))


def test_the_json_report_records_the_blutter_format_fingerprint(tmp_path,
                                                                blutter_out):
    out = os.path.join(str(tmp_path), "out")
    cli.main(["--blutter-out", blutter_out, "-o", out, "--report", "json"])
    with open(os.path.join(out, "report.json"), encoding="utf-8") as fh:
        report = json.load(fh)
    assert report["blutter_format_fingerprint"] == pa.BLUTTER_FORMAT_FINGERPRINT
    assert report["unimplemented_stages"], "the report must admit what did not run"
    assert report["parse"]["files"] >= 1


def test_dump_model_writes_the_whole_parsed_model(tmp_path, blutter_out):
    out = os.path.join(str(tmp_path), "out")
    model = os.path.join(str(tmp_path), "model.json")
    cli.main(["--blutter-out", blutter_out, "-o", out, "--report", "none",
              "--dump-model", model])
    with open(model, encoding="utf-8") as fh:
        blob = json.load(fh)
    urls = [l["url"] for l in blob["libraries"]]
    assert "package:chat/features/call/data/call_log_store.dart" in urls
    assert '"DESTROYED"' in json.dumps(blob), \
        "the dumped model must keep the confidence labels"


def test_skeleton_filters_libraries_and_writes_them_under_out(tmp_path,
                                                              blutter_out, capsys):
    out = os.path.join(str(tmp_path), "out")
    cli.main(["--blutter-out", blutter_out, "-o", out, "--skeleton",
              "**/call_log_store.dart", "--report", "none"])
    written = [f for _d, _s, files in os.walk(out) for f in files]
    assert written == ["call_log_store.dart"]
    assert "CallLogEntry" in capsys.readouterr().out


def test_a_skeleton_pattern_that_matches_nothing_writes_nothing(tmp_path,
                                                                blutter_out):
    out = os.path.join(str(tmp_path), "out")
    rc = cli.main(["--blutter-out", blutter_out, "-o", out, "--skeleton",
                   "**/nothing_like_this.dart", "--report", "none"])
    assert rc == 0
    assert not os.path.isdir(os.path.join(out, "skeletons"))


def test_include_deps_widens_the_selection(tmp_path, blutter_out):
    out = os.path.join(str(tmp_path), "out")
    model = os.path.join(str(tmp_path), "model.json")
    cli.main(["--blutter-out", blutter_out, "-o", out, "--report", "none",
              "--include-deps", "--dump-model", model])
    with open(model, encoding="utf-8") as fh:
        urls = [l["url"] for l in json.load(fh)["libraries"]]
    assert any(u.startswith("package:flutter/") for u in urls)


def test_packages_selects_exactly_what_was_asked_for(tmp_path, blutter_out):
    out = os.path.join(str(tmp_path), "out")
    model = os.path.join(str(tmp_path), "model.json")
    cli.main(["--blutter-out", blutter_out, "-o", out, "--report", "none",
              "--packages", "flutter", "--dump-model", model])
    with open(model, encoding="utf-8") as fh:
        urls = [l["url"] for l in json.load(fh)["libraries"]]
    assert urls and all(u.startswith("package:flutter/") for u in urls)


def test_strict_fails_when_a_line_was_not_understood(tmp_path, blutter_out,
                                                     capsys):
    write_asm(os.path.join(blutter_out, "asm"), "chat/junk.dart",
              "// lib: , url: package:chat/junk.dart\n%%% not a Blutter line %%%\n")
    out = os.path.join(str(tmp_path), "out")
    rc = cli.main(["--blutter-out", blutter_out, "-o", out, "--strict",
                   "--report", "none"])
    assert rc == 5
    assert "--strict: 1 unparsed lines" in capsys.readouterr().err


def test_strict_passes_on_a_clean_parse(tmp_path, blutter_out):
    out = os.path.join(str(tmp_path), "out")
    assert cli.main(["--blutter-out", blutter_out, "-o", out, "--strict",
                     "--report", "none"]) == 0


def test_an_unusable_blutter_output_is_refused_with_a_reason(tmp_path, capsys):
    empty = os.path.join(str(tmp_path), "not_blutter")
    os.makedirs(empty)
    rc = cli.main(["--blutter-out", empty, "-o", os.path.join(str(tmp_path), "out")])
    assert rc == 3
    err = capsys.readouterr().err
    assert "blutter stage failed" in err
    assert "objs.txt" in err


# Regression guard. --skeleton used to rebind `out_root` to the skeleton
# directory, which moved the reports into <out>/skeletons/.
def test_skeleton_does_not_move_the_reports(tmp_path, blutter_out):
    out = os.path.join(str(tmp_path), "out")
    cli.main(["--blutter-out", blutter_out, "-o", out, "--skeleton", "*"])
    assert os.path.isfile(os.path.join(out, "report.json"))
    assert os.path.isfile(os.path.join(out, "report.md"))


# Regression guard. The same shadowing set `out_root` to None whenever -o was
# omitted, and the report writer then died on a path join.
def test_skeleton_without_an_out_dir_still_finishes(tmp_path, monkeypatch,
                                                    blutter_out):
    monkeypatch.chdir(str(tmp_path))
    assert cli.main(["--blutter-out", blutter_out, "--skeleton", "*"]) == 0


# --------------------------------------------------------------------------- #
# the top-level command surface (main.py / the console script)
# --------------------------------------------------------------------------- #

def test_the_documented_top_level_flags_still_exist():
    """README and main.py's docstring promise these three. Removing one
    breaks every set of instructions anyone has written down."""
    from flutter_decompile import frontend
    flags = frontend.build_parser().format_help()
    for flag in ("--decompile", "--check", "--capability"):
        assert flag in flags


def test_the_top_level_capability_flag_prints_the_statement(capsys):
    from flutter_decompile import frontend
    assert frontend.main(["--capability"]) == 0
    assert "does NOT decompile Dart" in capsys.readouterr().out
