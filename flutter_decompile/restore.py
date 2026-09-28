"""flutter_decompile.restore -- rebuild the whole APK as a project tree.

The Dart reconstruction (``reconstruct.py``) is only half of "restore the app".
An APK is a zip: alongside ``libapp.so`` it carries the Flutter asset bundle,
the app's images/fonts/shaders, the Android resources, the manifest and the
native libraries. Every one of those is *literally present* -- extracting them
is recovery, not guesswork -- and a tool that claims to restore the app should
hand them back too, laid out the way a project is laid out.

This module unpacks the entire archive and sorts what it finds into honest
buckets, then writes a manifest that says, for every path, whether it was:

  RECOVERED      the bytes are in the APK; this is the real file
                 (assets, images, fonts, shaders, resources.arsc, native .so,
                 NOTICES, the manifest -- verbatim).
  RECOVERED-NAME the *name and location* are real (recovered from the Dart
                 snapshot's library URLs), the *body* is reconstructed
                 (the .dart files under lib/).
  BINARY         present verbatim but in a compiled form this tool does not
                 decode (AndroidManifest.xml is binary AXML; classes.dex is
                 Android bytecode from plugins, not the app's Dart).

Nothing is invented and nothing is hidden. Folder and file names the app author
chose come back for the whole asset tree and, for the Dart, for every library
the snapshot names.
"""

from __future__ import annotations

import os
import posixpath
import shutil
import zipfile
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Tuple


# What each top-level area of an APK is, and how honestly we can hand it back.
@dataclass
class Item:
    arcname: str          # path inside the APK
    dest: str             # where we wrote it
    category: str         # assets | dart-assets | resources | native | manifest | dex | meta | signing
    status: str           # RECOVERED | BINARY
    size: int


@dataclass
class RestoreResult:
    apk: str
    root: str             # the restored project root
    items: List[Item] = dc_field(default_factory=list)
    skipped: int = 0

    def by_category(self) -> Dict[str, List[Item]]:
        out: Dict[str, List[Item]] = {}
        for it in self.items:
            out.setdefault(it.category, []).append(it)
        return out


# Extensions/paths that are compiled forms we extract but do not pretend to
# decode. Everything else that is a normal file is RECOVERED as-is.
def _classify(arc: str) -> Tuple[str, str]:
    lower = arc.lower()
    base = posixpath.basename(lower)

    if lower.startswith("meta-inf/"):
        if base.endswith((".rsa", ".dsa", ".ec", ".sf", ".mf")):
            return "signing", "RECOVERED"
        return "meta", "RECOVERED"
    if base == "androidmanifest.xml":
        return "manifest", "BINARY"          # binary AXML
    if base == "resources.arsc":
        return "resources", "BINARY"
    if lower.startswith("res/"):
        return "resources", "RECOVERED"
    if lower.endswith(".dex"):
        return "dex", "BINARY"               # plugin Java/Kotlin, not app Dart
    if lower.startswith("lib/") and lower.endswith(".so"):
        return "native", "RECOVERED"
    if "flutter_assets/" in lower:
        return "dart-assets", "RECOVERED"    # the app's own bundle
    if lower.startswith("assets/"):
        return "assets", "RECOVERED"
    return "assets", "RECOVERED"


def extract_all(apk_path: str, dest_root: str,
                log=lambda s: None) -> RestoreResult:
    """Unpack the entire APK into ``dest_root/apk_contents`` and inventory it."""
    result = RestoreResult(apk=os.path.abspath(apk_path), root=os.path.abspath(dest_root))
    contents = os.path.join(dest_root, "apk_contents")
    os.makedirs(contents, exist_ok=True)

    with zipfile.ZipFile(apk_path) as zf:
        for info in zf.infolist():
            arc = info.filename
            if arc.endswith("/"):
                continue
            # zipfile already refuses absolute paths; guard traversal anyway.
            safe = arc.replace("\\", "/").lstrip("/")
            if ".." in safe.split("/"):
                result.skipped += 1
                continue
            dest = os.path.join(contents, *safe.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            try:
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
            except (OSError, zipfile.BadZipFile) as e:
                log("[restore] could not extract %s: %s" % (arc, e))
                result.skipped += 1
                continue
            category, status = _classify(arc)
            result.items.append(Item(arc, dest, category, status, info.file_size))

    log("[restore] extracted %d file(s) from the APK -> %s"
        % (len(result.items), contents))
    return result


_CATEGORY_TITLE = {
    "dart-assets": "Flutter asset bundle (the app's own images/fonts/shaders/data)",
    "assets": "Other bundled assets",
    "resources": "Android resources (res/, resources.arsc)",
    "native": "Native libraries (.so)",
    "manifest": "AndroidManifest.xml",
    "dex": "Android bytecode (plugin Java/Kotlin, not the app's Dart)",
    "meta": "Archive metadata (META-INF)",
    "signing": "Signing block (META-INF signatures)",
}


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.0f %s" % (size, unit) if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%d B" % n


def write_manifest(out_dir: str, restore: RestoreResult,
                   dart_files: int, dart_root: str) -> str:
    """Write RESTORED.md describing the whole tree and its honesty status."""
    L: List[str] = []
    L.append("# Restored project")
    L.append("")
    L.append("Rebuilt from `%s`." % os.path.basename(restore.apk))
    L.append("")
    L.append("| Part | Where | Status |")
    L.append("|---|---|---|")
    dart_rel = os.path.relpath(dart_root, out_dir) if dart_files else "-"
    L.append("| Dart libraries (%d files) | `%s/` | **RECOVERED-NAME** "
             "(folder + file names recovered from the snapshot's library URLs; "
             "bodies reconstructed, field/local names marked as holes) |"
             % (dart_files, dart_rel))

    buckets = restore.by_category()
    order = ["dart-assets", "assets", "resources", "native", "manifest",
             "dex", "meta", "signing"]
    for cat in order:
        items = buckets.get(cat)
        if not items:
            continue
        total = sum(i.size for i in items)
        status = "RECOVERED" if all(i.status == "RECOVERED" for i in items) else \
                 ("BINARY" if all(i.status == "BINARY" for i in items) else "mixed")
        title = _CATEGORY_TITLE.get(cat, cat)
        note = ""
        if cat == "manifest":
            note = " — extracted verbatim as **binary AXML**; not decoded to text"
        elif cat == "resources":
            note = " — `res/` files verbatim; `resources.arsc` is a binary table"
        elif cat == "dex":
            note = " — these are plugin Java/Kotlin classes, NOT your Dart"
        L.append("| %s (%d files, %s) | `apk_contents/` | **%s**%s |"
                 % (title, len(items), _human(total), status, note))

    if restore.skipped:
        L.append("| (skipped) | - | %d entr%s could not be extracted |"
                 % (restore.skipped, "y" if restore.skipped == 1 else "ies"))

    L.append("")
    L.append("## What each status means")
    L.append("")
    L.append("- **RECOVERED** — the bytes are physically in the APK. This is the "
             "real file, not a guess.")
    L.append("- **RECOVERED-NAME** — the name and location are real (from the Dart "
             "snapshot's library URLs); the body is a faithful machine "
             "reconstruction. See `dart/` and the header on each file.")
    L.append("- **BINARY** — present verbatim but in a compiled form this tool "
             "does not decode (binary AXML, `resources.arsc`, Android `.dex`).")
    L.append("")
    L.append("The Dart *source layout* the author typed (exact statement "
             "formatting, comments, local and instance-field names) was never "
             "written into the release build and cannot be recovered. Everything "
             "that was in the file is here; everything that wasn't is labelled.")
    L.append("")

    path = os.path.join(out_dir, "RESTORED.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    return path
