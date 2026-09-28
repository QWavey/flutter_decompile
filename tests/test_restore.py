"""The full-APK restore: extract everything, label it honestly."""

import os
import zipfile

from flutter_decompile import restore


def _make_apk(path: str) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00binaryAXML")
        zf.writestr("resources.arsc", b"\x02\x00\x0c\x00table")
        zf.writestr("res/drawable/icon.png", b"\x89PNG\r\n")
        zf.writestr("assets/flutter_assets/assets/logo.png", b"\x89PNG logo")
        zf.writestr("assets/flutter_assets/NOTICES", b"licences")
        zf.writestr("lib/arm64-v8a/libapp.so", b"\x7fELFsnapshot")
        zf.writestr("classes.dex", b"dex\n035plugin")
        zf.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0")
        zf.writestr("META-INF/CERT.RSA", b"\x30\x82signing")


def test_extract_all_pulls_every_file(tmp_path):
    apk = os.path.join(str(tmp_path), "app.apk")
    _make_apk(apk)
    out = os.path.join(str(tmp_path), "out")
    res = restore.extract_all(apk, out)

    assert len(res.items) == 9
    contents = os.path.join(out, "apk_contents")
    # The real bytes are on disk, laid out as in the archive.
    assert os.path.isfile(os.path.join(contents, "res", "drawable", "icon.png"))
    assert os.path.isfile(os.path.join(contents, "lib", "arm64-v8a", "libapp.so"))


def test_classification_is_honest(tmp_path):
    apk = os.path.join(str(tmp_path), "app.apk")
    _make_apk(apk)
    res = restore.extract_all(apk, os.path.join(str(tmp_path), "out"))
    by = {it.arcname: it for it in res.items}

    assert by["AndroidManifest.xml"].status == "BINARY"
    assert by["AndroidManifest.xml"].category == "manifest"
    assert by["classes.dex"].status == "BINARY"
    assert by["classes.dex"].category == "dex"
    assert by["lib/arm64-v8a/libapp.so"].status == "RECOVERED"
    assert by["lib/arm64-v8a/libapp.so"].category == "native"
    assert by["assets/flutter_assets/assets/logo.png"].category == "dart-assets"
    assert by["res/drawable/icon.png"].status == "RECOVERED"
    assert by["META-INF/CERT.RSA"].category == "signing"


def test_manifest_names_every_status(tmp_path):
    apk = os.path.join(str(tmp_path), "app.apk")
    _make_apk(apk)
    out = os.path.join(str(tmp_path), "out")
    res = restore.extract_all(apk, out)
    md_path = restore.write_manifest(out, res, dart_files=12,
                                     dart_root=os.path.join(out, "dart"))
    md = open(md_path, encoding="utf-8").read()

    assert "RECOVERED-NAME" in md          # the Dart tree
    assert "**RECOVERED**" in md           # the real files
    assert "**BINARY**" in md              # the manifest / dex
    assert "12 files" in md
    assert "binary AXML" in md
    # It must never claim to have decoded what it only extracted.
    assert "NOT your Dart" in md
