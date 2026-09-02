# flutter_decompile

Reconstructs a structural skeleton of a Flutter app from its AOT snapshot, and
marks every hole.

## Read this first

**This tool does not decompile Dart, and no tool can.** Flutter's release build
compiles Dart to native machine code. What ends up in `libapp.so` is an AOT
snapshot: it keeps the object graph the runtime needs and throws away
everything the runtime does not. Statement-level source, comments, formatting,
import lists, local variable names and parameter names are simply not in the
file. They are not obfuscated, not compressed, not encrypted — they were never
written.

Anything that claims to give you the original `.dart` files back is either
guessing or lying. This tool refuses to do either: it emits what the snapshot
actually contains and labels the rest.

### What survives

| Recovered | Partial | Destroyed |
|---|---|---|
| library URL and file path | method return types | statement bodies |
| class and superclass names | positional parameter types | comments and formatting |
| class id and instance size | instance field types (lowered to VM types) | **instance field names** |
| method / getter / setter names | | local variable names |
| static field **names** | | positional parameter names |
| `late` instance field names | | import and export lists |
| `async` / `sync*` markers | | generics erased at runtime |
| enum names, ordinals, `.name` | | |
| string literals, const object graphs | | |
| named-argument names (at call sites) | | |

The one that hurts is **instance field names**. AOT stores a field as an
*offset*, so `user.name` compiles to "load the pointer 12 bytes into this
object". The name is gone. `--infer-fields` reconstructs some of them from
evidence — a `toJson()` map literal pairs a key with the very next field load,
a `toString()` label precedes the field it describes — and every inferred name
is emitted with the evidence that produced it and a confidence level. An
inference is a hypothesis with a citation, not a recovered name, and the output
says so on every single one.

## Install

Nothing to install. Python 3.10+, standard library only.

```bash
python main.py --check
```

That reports what is on your machine and what is missing, with the install
command for your platform. Blutter itself is fetched automatically on first
use, so it is fine for it to be absent.

If you would rather have it on your `PATH`, `pip install .` puts a
`flutter-decompile` command there. It is the same program as `python main.py`
— same code, same flags — not a second implementation, so every `python
main.py ...` line below works unchanged as `flutter-decompile ...`.

## Use

```bash
python main.py --decompile app.apk
```

That is the whole interface. It checks the toolchain, clones Blutter if you do
not have it, builds a Dart VM matching the APK's snapshot, disassembles it,
and writes a skeleton of every library plus a report.

```bash
python main.py --decompile app.apk --out mydir
python main.py --decompile app.apk --only "**/auth/**"
python main.py --decompile app.apk --quick     # skeleton only, much faster
python main.py --decompile blutter_out/        # re-analyse without rebuilding
```

### How long it takes

It prints a plan with timings before it starts anything expensive, and asks
before committing you to the long part.

| stage | time |
|---|---|
| check the toolchain | seconds |
| fetch Blutter | 10s - 1m, first run only |
| unpack the APK | seconds |
| **build a matching Dart VM** | **20m - 1h, first run per Dart version** |
| disassemble the snapshot | 1 - 10 min |
| parse + emit | seconds |

The Dart VM build is the long pole and it is unavoidable: Blutter needs a VM
matching the snapshot to interpret it. It is cached, so the second APK on the
same Dart version takes minutes rather than an hour.

### It fixes Blutter's build for you

Two things break a fresh Blutter clone on a current toolchain, and both are
patched automatically:

- CMake 4.x dropped compatibility with `cmake_minimum_required` below 3.5,
  which several vendored builds still declare.
- One `CMakeLists.txt` calls `string(REPLACE ... ${CMAKE_CXX_FLAGS})` unquoted,
  which fails outright when that variable is empty - as it is on a default
  configure.

On Windows it also locates MSVC through `vswhere` and captures the environment
from `vcvars64.bat`, so you do not need a developer prompt.

### The lower-level CLI

`main.py` (and the installed `flutter-decompile`) is a friendly front end over
`flutter_decompile.cli`, which exposes everything individually if you want it,
as `python -m flutter_decompile <input> ...`:

| Flag | Effect |
|---|---|
| `--skeleton GLOB` | Only libraries matching a glob (`**/panic/**`) or substring |
| `--infer-fields safe\|aggressive` | Reconstruct field names, with evidence |
| `--include-deps` | All packages, not just the app's own |
| `--no-bodies` | Skeleton only; much faster |
| `--preflight` | Check the toolchain and exit |
| `--strict` | Non-zero exit if parse coverage is not 100% |
| `--dump-model FILE` | The whole parsed model as JSON |

## Why the emit stage refuses

`--emit` (stage 6, writing compilable `.dart`) is deliberately not implemented.
Everything needed to write a *plausible* file is here — class shapes, method
names, string constants — and that is exactly the problem. A file that compiles
and looks right, with invented field names and empty method bodies, is worse
than no file: it reads as recovered source and it is not.

The parsed model is available through `--dump-model` and `--skeleton`. Build on
that if you want to generate code; just do not let the output pretend to be
something it is not.

## Verifying

```bash
python selftest_emit.py
```

Runs the emitter against known structures and prints inferred field names with
their evidence chains.

## Licence

MIT.
