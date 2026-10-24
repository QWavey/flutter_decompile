# flutter_decompile

Rebuilds a Flutter/Dart app from its release APK as readable Dart, with whole method bodies. It also marks every gap it can't fill.

## What it does, and what it can't

Give it a release `.apk` (or `.apks`, `.xapk`, `.aab`, or a `libapp.so`) and it rebuilds the app. The code comes back as `.dart` files: class shapes, method signatures, and a reconstruction of every method body. That includes each call (resolved to `library::Class::method` where the snapshot names it), each string literal, each field load and store, and each allocation, in the order the machine runs them. The rest of the APK is extracted unchanged next to the code: the Flutter asset bundle, images, fonts, shaders, Android resources, native libraries, and the manifest. The output is a restored project, not only a pile of code.

Flutter's release build compiles Dart to native machine code. What lands in `libapp.so` is an AOT snapshot that keeps the object graph the runtime needs and drops the rest. Comments, statement layout, local variable names, parameter names, and instance-field names never make it into the file, so the tool doesn't invent them. It emits what the snapshot contains and labels the gaps. A field whose name is gone shows up as `field_0xNN`. Value names in bodies are register names (`v0`, `v3`). Every reconstructed file opens with a header that says so.

The output reads like what jadx produces for Android: the logic is all there, the cosmetic surface is rebuilt, and nothing is presented as more than it is.

### What survives

| Recovered | Partial | Reconstructed as a gap |
|---|---|---|
| library URL and file path | method return types | statement-level source layout |
| class and superclass names | positional parameter types | comments and formatting |
| class id and instance size | instance field types (VM types) | **instance field names** (`field_0xNN`) |
| method, getter, and setter names | | local variable names (`v0`, `v3`) |
| static field **names** | | positional parameter names |
| every call, in body order | | import and export lists |
| every string literal | | generics (erased at runtime) |
| field load and store offsets | | |
| `async` and `sync*` markers | | |
| enum names, ordinals, and `.name` | | |

`--infer-fields` is accepted but has no effect in 0.1: the field-name inference in `infer.py` (for example, a `toJson()` map literal pairing a key with the next field load) is not wired into the command line yet. When it is, every inferred name will carry its evidence and a confidence tag.

## Install

Nothing to install for the core tool. It needs Python 3.10+ and only the standard library.

```bash
python main.py --check
```

That reports what is on your machine and what is missing, with the install command for your platform. Blutter itself is fetched automatically on first use.

For a `flutter-decompile` command on your `PATH`, run `pip install .`. It is the same program as `python main.py`. For the GUI, add the extra:

```bash
pip install ".[gui]"
```

## Use: the GUI (Windows 11)

```bash
python gui.py
```

This opens a Fluent (Windows 11) window. Select the APK, optionally pick an output folder, and press **Start**. The log streams as it runs. **Stop** kills the pipeline cleanly, including a long Dart VM build. Once installed, the command is `flutter-decompile-gui`.

## Use: the command line

```bash
python main.py --decompile app.apk
```

That is the whole interface. It checks the toolchain, clones Blutter if needed, builds a Dart VM that matches the APK's snapshot, disassembles it, and writes a reconstructed `.dart` tree for every app library, plus a report.

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

Everything under `apk_contents/` is **RECOVERED**: these are the real bytes from the archive. The Dart folder and file names come from the snapshot's library URLs. Only Dart bodies are reconstructed, and only field and local names are gaps. `RESTORED.md` labels every part.

Two things are decoded rather than left binary:

- **`AndroidManifest.xml`** is Android binary XML. It is decoded to readable text (`AndroidManifest.decoded.xml`), and its main facts (package, version, permissions) go into `RESTORED.md` and the run summary.
- **`pubspec.recovered.yaml`** lists the packages compiled into the snapshot. The dependency names are real. The version constraints weren't kept in the build, so they are omitted rather than guessed.

`.dex` files (plugin Java and Kotlin, not your Dart) and `resources.arsc` are handed back verbatim, not decoded.

By default, the reconstructed bodies drop pure machine bookkeeping (frame setup, register copies, stack spills) so the real operations aren't buried. `--keep-asm` keeps the byte-for-byte trace.

### Newer Dart versions

Blutter's disassembler tracks Dart's VM internals (the ObjectStore stub layout, the embedder API, the snapshot symbol scheme) release by release. An app built with a Dart version newer than Blutter supports cannot be processed. The Dart VM compiles, but Blutter's own build then fails.

flutter_decompile already patches the parts it can: the combined single-snapshot ELF loader, ICU and Capstone fetch, and the MSVC `__VA_OPT__` build. The stub-resolution rework is upstream Blutter work, and it can't be faked without producing wrong output.

So the tool detects this up front and stops in seconds, instead of after a 20 to 60 minute VM build:

```
!! LIKELY UNSUPPORTED DART VERSION
Dart 3.13.2 uses the newer VM layout ... build the app with an older, stable
Flutter (Dart <= 3.8) and run this again.
```

It doesn't dead-end, though. When Blutter can't be used, it runs a **Blutter-free fallback** that recovers what it can straight from the ELF, with no VM build:

- the app's **library tree**: every `package:app/...` and `dart:...` URL is stored as plain text in the snapshot, so the folder and file structure comes back (names only, no bodies) in `snapshot/app_libraries.txt`
- **strings**: class, function, and field names, plus every string literal (URLs, keys, messages), in `snapshot/strings.txt`
- a **raw ARM64/x64 disassembly** of the snapshot code via Capstone, if it is installed, in `snapshot/snapshot_disasm.asm`
- the full **APK restore** (assets, resources, native libs, decoded manifest, recovered pubspec), exactly as above

That is the floor: real names and structure, no invented bodies, and it runs in seconds. `--try-unsupported` attempts the full Blutter build anyway. It runs the VM build and will most likely fail at Blutter's compile step. The supported ceiling is `apk.BLUTTER_MAX_DART`, and it moves up when Blutter adds support for a new release.

### How long it takes

The tool prints a plan with timings before anything expensive, and it asks before the long part.

| Stage | Time |
|---|---|
| Check the toolchain | seconds |
| Fetch Blutter | 10 s to 1 min, first run only |
| Unpack the APK | seconds |
| **Build a matching Dart VM** | **20 min to 1 h, first run per Dart version** |
| Disassemble the snapshot | 1 to 10 min |
| Parse, reconstruct, and emit | seconds |

The VM build is the slow part, and it can't be skipped, because Blutter needs a VM that matches the snapshot in order to interpret it. The result is cached, so the second APK built with the same Dart version takes minutes instead of an hour.

### Build fixes for Blutter

Two things break a fresh Blutter clone on a current toolchain. The tool patches both automatically:

- CMake 4.x dropped compatibility with `cmake_minimum_required` below 3.5, which several vendored builds still declare.
- One `CMakeLists.txt` calls `string(REPLACE ... ${CMAKE_CXX_FLAGS})` unquoted, which fails when that variable is empty, as it is on a default configure.

On Windows, the tool also finds MSVC through `vswhere` and loads the environment from `vcvars64.bat`, so you don't need a developer prompt.

### The lower-level CLI

`main.py` (and `flutter-decompile`) is a friendly front end over `flutter_decompile.cli`. Every step is also available individually as `python -m flutter_decompile <input> ...`:

| Flag | Effect |
|---|---|
| `--emit` | Write the reconstructed `.dart` tree (bodies included) to `-o/dart/` |
| `--skeleton GLOB` | Signatures-only listing for libraries matching a glob or substring |
| `--infer-fields safe\|aggressive` | Accepted, no effect in 0.1 (field-name inference is not wired in yet) |
| `--include-deps` | All packages, not just the app's own |
| `--no-bodies` | Skeleton only, much faster |
| `--preflight` | Check the toolchain and exit |
| `--strict` | Non-zero exit if parse coverage is not 100% |
| `--dump-model FILE` | The whole parsed model as JSON |

## How the bodies are reconstructed

Each method body is a linear trace of what the AOT code does, written as Dart-style statements:

- A resolved call becomes `v0 = decode(v2);  // -> [dart:convert] Base64Codec::decode`.
- A field access becomes `v1 = v0.field_0x13;`, or its real name if it was recovered or inferred.
- A string load becomes `v2 = "-";`.
- Anything the parser doesn't lift to a high-level operation (branches, compares, frame setup) is kept verbatim as a `// comment`. The trace stays complete, and nothing is silently dropped.

Register names are kept on purpose. A body that reads `v3 = v4.field_0xb` can't be mistaken for hand-written source, and that is the point.

## Verifying

```bash
python selftest_emit.py
python -m pytest
```

## License

MIT.
