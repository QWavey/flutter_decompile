#!/usr/bin/env python3
"""flutter_decompile - one command, from an APK to a readable skeleton.

    python main.py --decompile app.apk

This is the launcher for people running from a checkout. The command itself
lives in flutter_decompile/frontend.py so that the pip-installed console
script (`flutter-decompile`) and this file are the same program rather than
two copies that drift apart. Run `python main.py --help` for the options and
`--capability` for what can and cannot be recovered.

Nothing is put on sys.path by hand here: Python already puts this file's
directory first, which is exactly the checkout that contains the package.
"""

import sys

from flutter_decompile.frontend import main

if __name__ == "__main__":
    sys.exit(main())
