# flutter_decompile

Reconstructs a Flutter/Dart app from its release APK as readable Dart — **whole
methods, with bodies** — and marks every hole honestly.

## What it does, and what it can't

Point it at a release `.apk` (or `.apks` / `.xapk` / `.aab` / a `libapp.so`) and
it rebuilds the whole app. The code comes back as `.dart` files: class shapes,
method signatures, and a **faithful reconstruction of every method body** — every
call (resolved to `library::Class::method` where the snapshot named it), every
string literal, every field load/store, every allocation, in the order the
machine runs them. Everything else the APK carries — the Flutter asset bundle,
images, fonts, shaders, Android resources, native libraries, the manifest — is
extracted verbatim next to it, so the output is a restored project, not just a
pile of code. That is a real decompilation: the complete app, in a form you can
read.

What it will **not** do is pretend to be the file the author typed. Flutter's
release build compiles Dart to native machine code; what lands in `libapp.so` is
an AOT snapshot that keeps the object graph the runtime needs and drops the rest.
Statement-level source layout, comments, local variable names, parameter names
and **instance-field names** were never written into the file. No tool can bring
back what isn't there — so this one doesn't invent it. It emits what the snapshot
contains and **labels** the rest: a field whose name is gone shows up as
`field_0xNN`, value names in bodies are register names (`v0`, `v3`), and every
reconstructed file opens with a header saying exactly that.

The result reads like decompiled output from any serious RE tool (think jadx for
Android/Java): the logic is all there, the cosmetic surface is reconstructed, and
nothing is disguised as something it isn't.

### What survives

| Recovered | Partial | Reconstructed as a hole |
|---|---|---|
| library URL and file path | method return types | statement-level source layout |
| class and superclass names | positional parameter types | comments and formatting |
| class id and instance size | instance field types (VM types) | **instance field names** (`field_0xNN`) |
| method / getter / setter names | | local variable names (`v0`, `v3`) |
| static field **names** | | positional parameter names |
| every call, in body order | | import and export lists |
| every string literal | | generics erased at runtime |
| field load/store offsets | | |
| `async` / `sync*` markers | | |
| enum names, ordinals, `.name` | | |

`--infer-fields` reconstructs some instance-field names from evidence — a
`toJson()` map literal pairs a key with the very next field load, a `toString()`
label precedes the field it describes — and every inferred name is emitted with
the evidence that produced it and a confidence tag. An inference is a hypothesis
with a citation, not a recovered name, and the output says so on every one.

## Install

Nothing to install for the core tool. Python 3.10+, standard library only.

```bash
python main.py --check
```

That reports what is on your machine and what is missing, with the install
command for your platform. Blutter itself is fetched automatically on first use.

For the command on your `PATH`, `pip install .` gives you `flutter-decompile`
(same program as `python main.py`). For the GUI, add the extra:

```bash
pip install ".[gui]"
```

## Use — the GUI (Windows 11)

```bash
python gui.py
```

A Fluent (Windows 11) window: **select the APK**, optionally pick an output
folder, and press **Start**. The log streams as it runs; **Stop** kills the
pipeline — including a long Dart VM build — cleanly. Installed, it's
`flutter-decompile-gui`.

## Use — the command line

```bash
python main.py --decompile app.apk
```

That's the whole interface. It checks the toolchain, clones Blutter if needed,
builds a Dart VM matching the APK's snapshot, disassembles it, and writes a
reconstructed `.dart` tree for every app library plus a report.

```bash
python main.py --decompile app.apk --out mydir
python main.py --decompile app.apk --only "**/auth/**"
python main.py --decompile app.apk --quick     # skeleton only, much faster
python main.py --decompile blutter_out/        # re-analyse without rebuilding
```

It restores the **whole app**, not just its code:

```
<out>/
  dart/
    lib/...                      reconstructed Dart source tree (bodies included)
    pubspec.recovered.yaml       the app's dependency names, recovered
  apk_contents/                  everything else in the APK, extracted verbatim:
                                   the Flutter asset bundle (images, fonts,
                                   shaders, data), Android resources, native
                                   .so libraries, the manifest
    AndroidManifest.decoded.xml  the binary manifest, decoded back to text
  skeletons/                     signatures-only listing
  RESTORED.md                    per-part honesty manifest + recovered app identity
  report.md                      parse/coverage report
```

Everything under `apk_contents/` is **RECOVERED** — the real bytes from the
archive. The Dart folder and file names are **recovered from the snapshot's
library URLs**; only Dart *bodies* are reconstructed and only field/local names
are holes. `RESTORED.md` labels every part.

Two things are decoded rather than left binary:

- **`AndroidManifest.xml`** is Android binary XML — it's decoded back to readable
  text (`AndroidManifest.decoded.xml`), and its headline facts (package,
  version, permissions) are pulled into `RESTORED.md` and the run summary.
- **`pubspec.recovered.yaml`** lists the packages compiled into the snapshot.
  The dependency *names* are real; version constraints weren't kept in the build,
  so they're omitted rather than guessed.

`.dex` files (plugin Java/Kotlin, not your Dart) and `resources.arsc` are handed
back verbatim rather than pretended-decoded.

By default the reconstructed bodies drop pure machine bookkeeping (frame setup,
register copies, stack spills) so the real operations aren't buried; `--keep-asm`
keeps the byte-for-byte trace.

### Newer Dart versions

Blutter's disassembler tracks Dart's VM internals (the ObjectStore stub layout,
the embedder API, the snapshot symbol scheme) **release by release**, so an app
built with a Dart newer than Blutter supports cannot be processed — the Dart VM
compiles, but Blutter's own build then fails. flutter_decompile already patches
the parts it can (the combined single-snapshot ELF loader, ICU/Capstone fetch,
the MSVC `__VA_OPT__` build), but the stub-resolution rework is upstream Blutter
work and can't be faked without producing wrong output.

So the tool **detects this up front and stops in seconds** rather than after the
20–60 min VM build:

```
!! LIKELY UNSUPPORTED DART VERSION
Dart 3.13.2 uses the newer VM layout ... build the app with an older, stable
Flutter (Dart <= 3.8) and run this again.
```

To get output today, build the app with an older stable Flutter. To attempt the
build anyway (it will run the full VM build and most likely fail at Blutter's
compile step), pass `--try-unsupported`. The supported ceiling lives in
`apk.BLUTTER_MAX_DART` and moves up when Blutter gains support for a new release.

### How long it takes

It prints a plan with timings before anything expensive, and asks before the long
part.

| stage | time |
|---|---|
| check the toolchain | seconds |
| fetch Blutter | 10s - 1m, first run only |
| unpack the APK | seconds |
| **build a matching Dart VM** | **20m - 1h, first run per Dart version** |
| disassemble the snapshot | 1 - 10 min |
| parse + reconstruct + emit | seconds |

The Dart VM build is the long pole and unavoidable: Blutter needs a VM matching
the snapshot to interpret it. It's cached, so the second APK on the same Dart
version takes minutes rather than an hour.

### It fixes Blutter's build for you

Two things break a fresh Blutter clone on a current toolchain, and both are
patched automatically:

- CMake 4.x dropped compatibility with `cmake_minimum_required` below 3.5, which
  several vendored builds still declare.
- One `CMakeLists.txt` calls `string(REPLACE ... ${CMAKE_CXX_FLAGS})` unquoted,
  which fails when that variable is empty — as it is on a default configure.

On Windows it also locates MSVC through `vswhere` and captures the environment
from `vcvars64.bat`, so you don't need a developer prompt.

### The lower-level CLI

`main.py` (and `flutter-decompile`) is a friendly front end over
`flutter_decompile.cli`, which exposes everything individually as
`python -m flutter_decompile <input> ...`:

| Flag | Effect |
|---|---|
| `--emit` | Write the reconstructed `.dart` tree (bodies included) to `-o/dart/` |
| `--skeleton GLOB` | Signatures-only listing for libraries matching a glob or substring |
| `--infer-fields safe\|aggressive` | Reconstruct field names, with evidence |
| `--include-deps` | All packages, not just the app's own |
| `--no-bodies` | Skeleton only; much faster |
| `--preflight` | Check the toolchain and exit |
| `--strict` | Non-zero exit if parse coverage is not 100% |
| `--dump-model FILE` | The whole parsed model as JSON |

## How the bodies are reconstructed

Each method body is the linearised trace of what the AOT code does, rendered as
Dart-flavoured statements:

- A resolved call becomes `v0 = decode(v2);  // -> [dart:convert] Base64Codec::decode`.
- A field access becomes `v1 = v0.field_0x13;` (or its real name, if recovered or
  inferred).
- A string load becomes `v2 = "-";`.
- Anything the parser didn't lift to a high-level op (branches, compares, frame
  setup) is kept verbatim as a `// comment`, so the trace is complete and nothing
  is silently dropped.

Register names are kept on purpose: a body that reads `v3 = v4.field_0xb` can't be
mistaken for hand-written source, which is the whole point.

## Verifying

```bash
python selftest_emit.py
python -m pytest
```

## Licence

MIT.
