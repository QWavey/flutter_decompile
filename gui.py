#!/usr/bin/env python3
"""Launch the flutter_decompile Fluent (Windows 11) GUI from a checkout.

    python gui.py

Same program as the installed ``flutter-decompile-gui`` command. Needs the GUI
extra: ``pip install "flutter_decompile[gui]"`` (or ``pip install PyQt6
PyQt6-Fluent-Widgets``).
"""
import sys

from flutter_decompile.gui import main

if __name__ == "__main__":
    sys.exit(main())
