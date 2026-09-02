"""
flutter_decompile.apk -- Stage 0 (acquire) and Stage 1 (identify).

Stage 0 turns whatever the user passed (.apk / .apks / .xapk / .aab / a
lib/<abi> directory / a bare libapp.so / an existing blutter_out) into a
working directory holding ``libapp.so`` and, when available, ``libflutter.so``
plus the Flutter asset manifests.

Stage 1 identifies the Dart SDK version and snapshot hash from three
INDEPENDENT signals and cross-checks them:

  1. the Dart snapshot header inside libapp.so   (best-effort, layout drifts)
  2. a string scan of libflutter.so              (authoritative when present)
  3. the ELF section shape of libapp.so          (coarse, corroborating only)

Per the design doc's rigor note, signal (1) is NEVER treated as authoritative
on its own: when (1) and (2) disagree, (2) wins and the disagreement is
recorded in the report rather than hidden.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import zipfile
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_ABI = "arm64-v8a"
KNOWN_ABIS = ("arm64-v8a", "armeabi-v7a", "x86_64")

# Dart snapshot magic, little-endian on every Android target we care about.
DART_SNAPSHOT_MAGIC = 0xF5F5DCDC
DART_SNAPSHOT_MAGIC_LE = struct.pack("<I", DART_SNAPSHOT_MAGIC)

# A Dart snapshot version hash is exactly 32 lowercase hex chars.
RE_SNAPSHOT_HASH = re.compile(rb"\b([0-9a-f]{32})\b")
# SEEN in libflutter.so: "3.5.4 (stable) (Wed Oct 16 ...) on \"android_arm64\""
RE_DART_BANNER = re.compile(
    rb"(\d+\.\d+\.\d+(?:[-.\w]*))\s+\((stable|beta|dev|edge)\)"
    rb"(?:\s+\([^)]{0,80}\))?\s+on\s+\"(android_[a-z0-9_]+)\""
)
RE_LOOSE_VERSION = re.compile(rb"Dart(?:VM)?\s+version:\s*(\d+\.\d+\.\d+[-.\w]*)")

ASSET_KEEP = ("AssetManifest.json", "AssetManifest.bin", "FontManifest.json", "NOTICES", "NOTICES.Z")


class AcquireError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Stage 0 -- acquire
# --------------------------------------------------------------------------- #

@dataclass
class Acquired:
    source: str
    source_kind: str                 # apk | apks | xapk | aab | dir | so | blutter_out
    workdir: str
    abi: str
    libapp: Optional[str] = None
    libflutter: Optional[str] = None
    assets: Dict[str, str] = dc_field(default_factory=dict)
    blutter_out: Optional[str] = None
    notes: List[str] = dc_field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source, "source_kind": self.source_kind,
            "workdir": self.workdir, "abi": self.abi,
            "libapp": self.libapp, "libflutter": self.libflutter,
            "assets": sorted(self.assets), "blutter_out": self.blutter_out,
            "notes": self.notes,
        }


def _looks_like_blutter_out(path: str) -> bool:
    return (os.path.isdir(os.path.join(path, "asm"))
            and os.path.isfile(os.path.join(path, "pp.txt")))


def acquire(source: str, workdir: str, abi: str = DEFAULT_ABI) -> Acquired:
    """Stage 0.  Never mutates ``source``; everything lands under ``workdir``."""
    source = os.path.abspath(source)
    if not os.path.exists(source):
        raise AcquireError("input does not exist: %s" % source)
    os.makedirs(workdir, exist_ok=True)

    if os.path.isdir(source):
        if _looks_like_blutter_out(source):
            return Acquired(source, "blutter_out", workdir, abi, blutter_out=source,
                            notes=["adopted an existing Blutter output; "
                                   "stages 0-1 are skipped, version is unverified"])
        return _acquire_dir(source, workdir, abi)

    ext = os.path.splitext(source)[1].lower()
    if ext == ".so":
        a = Acquired(source, "so", workdir, abi, libapp=source)
        sib = os.path.join(os.path.dirname(source), "libflutter.so")
        if os.path.isfile(sib):
            a.libflutter = sib
        else:
            a.notes.append("libflutter.so not found next to libapp.so; "
                           "version detection degrades to snapshot-hash only")
        return a
    if ext in (".apk", ".apks", ".xapk", ".aab", ".zip"):
        return _acquire_archive(source, workdir, abi, ext)
    raise AcquireError("unsupported input: %s" % source)


def _acquire_dir(source: str, workdir: str, abi: str) -> Acquired:
    """A `lib/<abi>` directory, or any directory containing libapp.so."""
    cands = [source, os.path.join(source, abi), os.path.join(source, "lib", abi)]
    for d in cands:
        libapp = os.path.join(d, "libapp.so")
        if os.path.isfile(libapp):
            a = Acquired(source, "dir", workdir, abi, libapp=libapp)
            lf = os.path.join(d, "libflutter.so")
            if os.path.isfile(lf):
                a.libflutter = lf
            else:
                a.notes.append("libflutter.so missing; version detection degraded")
            return a
    raise AcquireError("no libapp.so under %s (tried %s)" % (source, ", ".join(cands)))


def _acquire_archive(source: str, workdir: str, abi: str, ext: str) -> Acquired:
    kind = {".apk": "apk", ".apks": "apks", ".xapk": "xapk",
            ".aab": "aab", ".zip": "apk"}[ext]
    a = Acquired(source, kind, workdir, abi)
    outdir = os.path.join(workdir, "extracted")
    os.makedirs(outdir, exist_ok=True)

    with zipfile.ZipFile(source) as zf:
        names = zf.namelist()

        if kind in ("apks", "xapk") and not _has_native_lib(names, abi):
            # A split bundle: recurse into the split that carries lib/<abi>/.
            inner = _pick_split(names, abi)
            if inner is None:
                raise AcquireError(
                    "no split in %s contains lib/%s/libapp.so" % (source, abi))
            inner_path = os.path.join(outdir, os.path.basename(inner))
            with zf.open(inner) as src, open(inner_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            a.notes.append("descended into split %s" % inner)
            sub = _acquire_archive(inner_path, workdir, abi, ".apk")
            sub.source, sub.source_kind = source, kind
            sub.notes = a.notes + sub.notes
            return sub

        prefixes = ["lib/%s/" % abi, "base/lib/%s/" % abi]
        for want in ("libapp.so", "libflutter.so"):
            member = _first(names, [p + want for p in prefixes])
            if member is None:
                if want == "libapp.so":
                    raise AcquireError(
                        "lib/%s/libapp.so not found in %s. Present ABIs: %s"
                        % (abi, source, ", ".join(sorted(_abis_in(names))) or "none"))
                a.notes.append("libflutter.so missing from the archive; "
                               "version detection degrades to snapshot-hash only")
                continue
            dest = os.path.join(outdir, want)
            with zf.open(member) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            setattr(a, "libapp" if want == "libapp.so" else "libflutter", dest)

        # Flutter assets.  NOTICES is the ground-truth dependency list.
        for n in names:
            base = os.path.basename(n)
            if base in ASSET_KEEP and "flutter_assets/" in n:
                dest = os.path.join(outdir, "flutter_assets", base)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(n) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                a.assets[base] = dest
    return a


def _has_native_lib(names: List[str], abi: str) -> bool:
    return any(n.endswith("lib/%s/libapp.so" % abi) or n == "lib/%s/libapp.so" % abi
               for n in names)


def _pick_split(names: List[str], abi: str) -> Optional[str]:
    tag = abi.replace("-", "_")
    cands = [n for n in names if n.lower().endswith(".apk")]
    for n in cands:
        if tag in os.path.basename(n).lower():
            return n
    for n in cands:
        if os.path.basename(n).startswith("base"):
            return n
    return cands[0] if cands else None


def _first(names: List[str], wanted: List[str]) -> Optional[str]:
    s = set(names)
    for w in wanted:
        if w in s:
            return w
    for w in wanted:
        for n in names:
            if n.endswith(w):
                return n
    return None


def _abis_in(names: List[str]) -> List[str]:
    out = set()
    for n in names:
        m = re.match(r"^(?:base/)?lib/([^/]+)/", n)
        if m:
            out.add(m.group(1))
    return sorted(out)


# --------------------------------------------------------------------------- #
# Minimal ELF reader (signal 3)
# --------------------------------------------------------------------------- #

@dataclass
class ElfSection:
    name: str
    addr: int
    offset: int
    size: int


def read_elf_sections(path: str) -> Tuple[List[ElfSection], Dict[str, Any]]:
    """A deliberately small ELF32/64 section reader.  Returns ([], meta) on any
    malformed input rather than raising -- a stripped/packed .so must not crash
    the pipeline, it must be reported."""
    meta: Dict[str, Any] = {"class": None, "endian": None, "machine": None, "error": None}
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        meta["error"] = str(e)
        return [], meta

    if len(data) < 64 or data[:4] != b"\x7fELF":
        meta["error"] = "not an ELF file"
        return [], meta

    is64 = data[4] == 2
    little = data[5] == 1
    end = "<" if little else ">"
    meta["class"] = 64 if is64 else 32
    meta["endian"] = "little" if little else "big"
    try:
        meta["machine"] = struct.unpack_from(end + "H", data, 18)[0]
        if is64:
            e_shoff = struct.unpack_from(end + "Q", data, 0x28)[0]
            e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(end + "HHH", data, 0x3A)
        else:
            e_shoff = struct.unpack_from(end + "I", data, 0x20)[0]
            e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(end + "HHH", data, 0x2E)
        if not e_shoff or not e_shnum:
            meta["error"] = "no section headers (stripped)"
            return [], meta

        raw: List[Tuple[int, int, int, int]] = []
        for i in range(e_shnum):
            base = e_shoff + i * e_shentsize
            if is64:
                sh_name, _t, _f, sh_addr, sh_off, sh_size = struct.unpack_from(
                    end + "IIQQQQ", data, base)
            else:
                sh_name, _t, _f, sh_addr, sh_off, sh_size = struct.unpack_from(
                    end + "IIIIII", data, base)
            raw.append((sh_name, sh_addr, sh_off, sh_size))

        strtab_off, strtab_size = raw[e_shstrndx][2], raw[e_shstrndx][3]
        strtab = data[strtab_off:strtab_off + strtab_size]

        out: List[ElfSection] = []
        for sh_name, sh_addr, sh_off, sh_size in raw:
            e = strtab.find(b"\0", sh_name)
            nm = strtab[sh_name:e].decode("utf-8", "replace") if e >= 0 else ""
            out.append(ElfSection(nm, sh_addr, sh_off, sh_size))
        return out, meta
    except (struct.error, IndexError) as e:
        meta["error"] = "malformed section headers: %s" % e
        return [], meta


# --------------------------------------------------------------------------- #
# Stage 1 -- identify
# --------------------------------------------------------------------------- #

@dataclass
class VersionInfo:
    dart_version: Optional[str] = None
    snapshot_hash: Optional[str] = None
    channel: Optional[str] = None
    target: Optional[str] = None
    winner: Optional[str] = None            # which signal supplied the answer
    signals: Dict[str, Any] = dc_field(default_factory=dict)
    warnings: List[str] = dc_field(default_factory=list)
    elf: Dict[str, Any] = dc_field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return bool(self.dart_version and self.snapshot_hash)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dart_version": self.dart_version, "snapshot_hash": self.snapshot_hash,
            "channel": self.channel, "target": self.target, "winner": self.winner,
            "signals": self.signals, "warnings": self.warnings, "elf": self.elf,
            "complete": self.complete,
        }


def snapshot_hashes_from_libapp(libapp: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Signal 1 -- BEST EFFORT.

    Find the snapshot magic, then look for a 32-hex-char version string in the
    bytes that follow.  The exact header layout (magic / length / kind /
    version[32] / features) has moved between Dart releases, so we deliberately
    do NOT hardcode an offset: we scan a short window and report what we find.
    """
    try:
        with open(libapp, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    pos = 0
    while len(out) < limit:
        i = data.find(DART_SNAPSHOT_MAGIC_LE, pos)
        if i < 0:
            break
        pos = i + 4
        window = data[i + 4:i + 4 + 96]
        m = RE_SNAPSHOT_HASH.search(window)
        if m:
            out.append({
                "magic_offset": hex(i),
                "hash": m.group(1).decode("ascii"),
                "hash_offset_from_magic": hex(4 + m.start(1)),
            })
        else:
            out.append({"magic_offset": hex(i), "hash": None,
                        "hash_offset_from_magic": None})
    return out


def scan_libflutter(libflutter: str) -> Dict[str, Any]:
    """Signal 2 -- the Dart VM banner and version hash inside libflutter.so."""
    res: Dict[str, Any] = {"version": None, "channel": None, "target": None,
                           "hashes": [], "banner": None}
    try:
        with open(libflutter, "rb") as fh:
            data = fh.read()
    except OSError as e:
        res["error"] = str(e)
        return res

    m = RE_DART_BANNER.search(data)
    if m:
        res["version"] = m.group(1).decode("ascii")
        res["channel"] = m.group(2).decode("ascii")
        res["target"] = m.group(3).decode("ascii")
        res["banner"] = m.group(0)[:160].decode("utf-8", "replace")
    else:
        m2 = RE_LOOSE_VERSION.search(data)
        if m2:
            res["version"] = m2.group(1).decode("ascii")

    # Count 32-hex candidates; the snapshot hash is typically the one that
    # appears next to the banner, so rank by proximity when we have a banner.
    seen: Dict[str, int] = {}
    for hm in RE_SNAPSHOT_HASH.finditer(data):
        h = hm.group(1).decode("ascii")
        if h not in seen:
            seen[h] = hm.start()
    if m:
        anchor = m.start()
        ranked = sorted(seen.items(), key=lambda kv: abs(kv[1] - anchor))
    else:
        ranked = sorted(seen.items(), key=lambda kv: kv[1])
    res["hashes"] = [h for h, _ in ranked[:8]]
    return res


def identify(acq: Acquired,
             dart_version: Optional[str] = None,
             snapshot_hash: Optional[str] = None) -> VersionInfo:
    """Stage 1.  Cross-checks three signals and records which one won."""
    vi = VersionInfo()

    if acq.libapp:
        sections, elfmeta = read_elf_sections(acq.libapp)
        vi.elf = dict(elfmeta)
        names = [s.name for s in sections]
        dart_syms = [n for n in names if "Dart" in n or "kDart" in n]
        vi.elf["sections"] = len(sections)
        vi.elf["dart_sections"] = dart_syms
        # Signal 3: shape only. Corroborating, never decisive.
        vi.signals["elf_shape"] = {
            "has_section_headers": bool(sections),
            "dart_snapshot_sections": dart_syms,
        }
        s1 = snapshot_hashes_from_libapp(acq.libapp)
        vi.signals["libapp_header"] = {"occurrences": s1}
    else:
        vi.warnings.append("no libapp.so available; signals 1 and 3 unavailable")
        s1 = []

    s2: Dict[str, Any] = {}
    if acq.libflutter:
        s2 = scan_libflutter(acq.libflutter)
        vi.signals["libflutter_strings"] = s2
    else:
        vi.warnings.append("no libflutter.so; signal 2 unavailable, "
                           "the Dart version cannot be established from the binary")

    h1 = next((o["hash"] for o in s1 if o.get("hash")), None)
    h2list = s2.get("hashes") or []
    h2 = h2list[0] if h2list else None

    # --- resolve the snapshot hash ---------------------------------------- #
    if snapshot_hash:
        vi.snapshot_hash, vi.winner = snapshot_hash, "user override"
    elif h1 and h2:
        if h1 == h2 or h1 in h2list:
            vi.snapshot_hash = h1
            vi.winner = "libapp header + libflutter agree"
        else:
            # The rigor note: signal 2 wins, and we say so out loud.
            vi.snapshot_hash = h2
            vi.winner = "libflutter (disagreed with libapp header)"
            vi.warnings.append(
                "snapshot hash disagreement: libapp header says %s, "
                "libflutter says %s. Deferring to libflutter. Pass "
                "--snapshot-hash to override." % (h1, h2))
    elif h2:
        vi.snapshot_hash, vi.winner = h2, "libflutter only"
    elif h1:
        vi.snapshot_hash, vi.winner = h1, "libapp header only (BEST EFFORT)"
        vi.warnings.append(
            "the snapshot hash came only from the libapp.so header, whose byte "
            "layout is not stable across Dart releases; treat it as a guess.")
    else:
        vi.warnings.append("no snapshot hash found by any signal")

    # --- resolve the Dart version ----------------------------------------- #
    if dart_version:
        vi.dart_version = dart_version
        vi.winner = (vi.winner or "") + " / version from user override"
    elif s2.get("version"):
        vi.dart_version = s2["version"]
        vi.channel = s2.get("channel")
        vi.target = s2.get("target")
    else:
        vi.warnings.append(
            "the Dart version could not be determined from the binaries. "
            "Blutter needs it to build its dartvm; pass --dart-version.")

    if vi.target and acq.abi == "arm64-v8a" and "arm64" not in vi.target:
        vi.warnings.append("libflutter targets %s but --abi is %s"
                           % (vi.target, acq.abi))
    return vi


def check_abi_support(abi: str) -> List[str]:
    if abi == "arm64-v8a":
        return []
    if abi == "armeabi-v7a":
        return ["armeabi-v7a support is DEGRADED: Blutter's 32-bit backend "
                "recovers fewer types and no unboxed-field widths. Field "
                "offsets differ from arm64 and any offset-based inference "
                "carried over from an arm64 run is invalid."]
    return ["%s is not a supported Flutter AOT target for this tool" % abi]
