"""Offscreen smoke test for the Fluent GUI.

Skipped wherever PyQt6 / PyQt6-Fluent-Widgets (the optional [gui] extra) is not
installed, which includes CI. Where it is installed, it proves the window
constructs and its input validation rejects an empty/missing APK without
launching a subprocess.
"""

import os

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("qfluentwidgets")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication
    inst = QApplication.instance() or QApplication([])
    yield inst


def test_window_constructs(app):
    from flutter_decompile.gui import MainWindow
    w = MainWindow()
    assert w.proc is None
    assert not w.stop_btn.isEnabled()          # nothing running yet
    assert w.start_btn.isEnabled()


def test_start_rejects_missing_input_without_spawning(app):
    from flutter_decompile.gui import MainWindow
    w = MainWindow()
    w.apk_edit.setText("")                     # no APK chosen
    w._start()
    assert w.proc is None, "must not launch a process without an input"
    w.apk_edit.setText(r"C:\definitely\not\here.apk")
    w._start()
    assert w.proc is None, "must not launch a process for a nonexistent path"
