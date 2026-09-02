"""
flutter_decompile -- reconstruct a compilable Dart skeleton from a Flutter
Android AOT snapshot, with loud, documented holes.

THIS TOOL DOES NOT DECOMPILE DART.

An AOT snapshot contains machine code and metadata, not source.  There is no
AST to recover.  What this tool does is reconstruct the *structure* that did
survive, and mark every hole where something did not.  Read CAPABILITY below
before you trust a single line of its output.

Implemented in this version (0.1):
    stage 0  acquire   apk.py            .apk/.apks/.xapk/.aab/dir/.so -> libapp.so
    stage 1  identify  apk.py            Dart version + snapshot hash, 3 signals
    stage 2  blutter   blutter_driver.py locate / patch / run / adopt Blutter
    stage 3a parse     parse_asm.py      asm/**/*.dart -> IR
    stage 4  link      parse_asm.py      address index + call graph
    --                 ir.py             the shared program model
    stage 5  infer     infer.py          field-name inference + evidence trail
    stage 6  emit      emit.py           IR -> Dart source tree
    stage 7  report    report.py         coverage + invariant checks (--strict)

Not implemented in this version (the CLI says so rather than faking it):
    stage 3b/3c objs.txt and pp.txt parsers
    stage 7     `dart analyze` is run by the self-test, not yet by the CLI

NOTE for whoever wires the halves together: parse_asm.py currently carries its
own local dataclasses, while infer/emit/report are written against ir.py.  One
adapter (parse_asm facts -> ir.ProgramIR) is the remaining seam; both models
agree on the facts, only on the container do they differ.
"""

__version__ = "0.1.0"
__all__ = [
    "CAPABILITY",
    "apk",
    "blutter_driver",
    "parse_asm",
    "ir",
    "infer",
    "emit",
    "report",
    "IMPLEMENTED_STAGES",
    "UNIMPLEMENTED_STAGES",
]

IMPLEMENTED_STAGES = (
    "acquire", "identify", "blutter", "parse", "link", "infer", "emit", "report",
)
UNIMPLEMENTED_STAGES = ("parse_objs", "parse_pp", "verify")

# Submodules are deliberately NOT imported here: the emitter half (ir/infer/
# emit/report) must stay importable without pulling in the Blutter driver, and
# vice versa.  Import what you need explicitly, e.g.
#     from flutter_decompile.emit import emit_program

CAPABILITY = """\
flutter_decompile %s -- CAPABILITY STATEMENT

This tool does NOT decompile Dart. It reconstructs a structural skeleton from
an AOT snapshot and marks every hole. Statement-level source, comments,
formatting, import lists, local variable names and parameter names are NOT in
the snapshot and are never coming back.

  SURVIVES (RECOVERED)
    library URL / file path        class + superclass names, class id, size
    method names, getter/setter    async / sync* markers
    static field NAMES             `late` instance field names
    enum names + ordinals + .name  string literals, const object graphs
    machine code                   named-argument names (at call sites)

  PARTIAL
    method return types            `_` means the snapshot did not record one
    positional parameter TYPES     `/* No info */` means none recorded
    instance field types           lowered to VM types (_Mint, _OneByteString)

  DESTROYED -- not "unknown", provably absent
    instance field NAMES (non-late)   -> emitted as field_<offset>
    positional parameter NAMES        local variable names
    comments, doc comments, imports   default parameter values
    annotations, mixin provenance     anything tree-shaken

RULES OF OUTPUT
  RECOVERED facts are emitted as code.
  INFERRED guesses are emitted with an explicit evidence trail.
  UNKNOWN is emitted as a body that throws, with a banner explaining why.
  A field_N is NEVER renamed without recording the exact evidence.

If the app was built with --obfuscate, names are meaningless and the report
says so before anything else.
""" % __version__
