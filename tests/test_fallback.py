"""The Blutter-free fallback: real structure/strings/disasm, no VM."""

import os

from flutter_decompile import fallback as fb


def test_extract_library_urls_and_split():
    data = (b"\x00package:app/ui/home.dart\x00dart:core/list.dart\x00"
            b"package:app/main.dart\x00package:http/http.dart\x00")
    urls = fb.extract_library_urls(data)
    assert "package:app/ui/home.dart" in urls
    assert "dart:core/list.dart" in urls
    pkgs = fb.split_packages(urls)
    assert "app" in pkgs and "http" in pkgs and "dart" in pkgs
    assert "ui/home.dart" in pkgs["app"]


def test_guess_app_packages_prefers_the_one_with_main():
    pkgs = {"app": ["main.dart", "ui/home.dart"], "http": ["http.dart"],
            "dart": ["core/list.dart"], "flutter": ["src/widgets.dart"]}
    assert fb.guess_app_packages(pkgs) == ["app"]


def test_extract_strings_filters_short_runs():
    data = b"\x00hi\x00a_real_string_here\x00\x01\x02longenough\x00"
    out = fb.extract_strings(data, min_len=5)
    assert "a_real_string_here" in out
    assert "longenough" in out
    assert "hi" not in out


def test_run_writes_the_artifacts(tmp_path):
    # A minimal fake libapp: no ELF sections (disasm skipped), but URLs+strings.
    libapp = os.path.join(str(tmp_path), "libapp.so")
    with open(libapp, "wb") as fh:
        fh.write(b"\x7fELF" + b"\x00" * 60
                 + b"package:demo/main.dart\x00package:demo/a/b.dart\x00"
                 + b"a_visible_literal_string\x00")
    summary = fb.run(libapp, str(tmp_path), dart_version="9.9.9")
    snap = os.path.join(str(tmp_path), "snapshot")
    assert os.path.isfile(os.path.join(snap, "library_urls.txt"))
    assert os.path.isfile(os.path.join(snap, "app_libraries.txt"))
    assert os.path.isfile(os.path.join(snap, "strings.txt"))
    assert "demo" in summary["app_packages"]
    assert summary["library_urls"] >= 2

    report = fb.write_report(str(tmp_path), summary, "9.9.9")
    text = open(report, encoding="utf-8").read()
    assert "Blutter-free" in text and "9.9.9" in text
    assert "RECOVERED" in text
