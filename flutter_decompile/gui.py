"""flutter_decompile.gui -- a Fluent (Windows 11) front end.

Pick an APK, press Start, watch it run, press Stop to abort. It drives exactly
the same pipeline as ``python -m flutter_decompile.frontend`` -- the GUI is a
thin shell around it, launched as a child process so Stop can actually kill a
30-minute Dart VM build rather than pretend to.

Requires PyQt6 and PyQt6-Fluent-Widgets (``pip install "flutter_decompile[gui]"``).
"""

from __future__ import annotations

import os
import sys

try:
    from PyQt6.QtCore import QProcess, Qt
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import (
        QApplication, QFileDialog, QHBoxLayout, QVBoxLayout, QWidget,
    )
    from qfluentwidgets import (
        BodyLabel, CaptionLabel, FluentIcon, InfoBar, InfoBarPosition,
        LineEdit, PlainTextEdit, PrimaryPushButton, PushButton, SubtitleLabel,
        setTheme, Theme, TitleLabel, ProgressRing, CheckBox,
    )
except ImportError as e:  # pragma: no cover - import guard
    sys.stderr.write(
        "The GUI needs PyQt6 and PyQt6-Fluent-Widgets.\n"
        '  pip install "flutter_decompile[gui]"\n'
        "or:\n"
        "  pip install PyQt6 PyQt6-Fluent-Widgets\n\n"
        "Underlying import error: %s\n" % e)
    raise SystemExit(2)


APK_FILTER = ("Flutter app (*.apk *.apks *.xapk *.aab *.so);;"
              "Android package (*.apk *.apks *.xapk *.aab);;"
              "All files (*)")


class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.proc: QProcess | None = None

        self.setWindowTitle("flutter_decompile")
        self.resize(860, 620)
        self.setMinimumSize(680, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(14)

        root.addWidget(TitleLabel("flutter_decompile"))
        sub = CaptionLabel(
            "Reconstructs a Flutter / Dart app from its release APK as readable "
            "Dart — bodies included. A faithful machine reconstruction of "
            "the AOT snapshot, honestly labelled: names the snapshot dropped "
            "stay marked as holes.")
        sub.setWordWrap(True)
        root.addWidget(sub)

        # ---- APK row -----------------------------------------------------
        root.addWidget(SubtitleLabel("Input"))
        apk_row = QHBoxLayout()
        self.apk_edit = LineEdit()
        self.apk_edit.setPlaceholderText("Select an .apk / .apks / .xapk / .aab (or a libapp.so)")
        self.apk_edit.setClearButtonEnabled(True)
        browse = PushButton(FluentIcon.FOLDER, "Browse")
        browse.clicked.connect(self._browse_apk)
        apk_row.addWidget(self.apk_edit, 1)
        apk_row.addWidget(browse)
        root.addLayout(apk_row)

        # ---- output row --------------------------------------------------
        out_row = QHBoxLayout()
        self.out_edit = LineEdit()
        self.out_edit.setPlaceholderText("Output folder (default: <apk name>_decompiled)")
        self.out_edit.setClearButtonEnabled(True)
        out_browse = PushButton(FluentIcon.FOLDER, "Choose")
        out_browse.clicked.connect(self._browse_out)
        out_row.addWidget(BodyLabel("Output"))
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(out_browse)
        root.addLayout(out_row)

        # ---- options -----------------------------------------------------
        opt_row = QHBoxLayout()
        self.quick_cb = CheckBox("Quick (skeleton only, no bodies)")
        opt_row.addWidget(self.quick_cb)
        opt_row.addStretch(1)
        root.addLayout(opt_row)

        # ---- Start / Stop ------------------------------------------------
        ctl_row = QHBoxLayout()
        self.start_btn = PrimaryPushButton(FluentIcon.PLAY, "Start")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = PushButton(FluentIcon.CANCEL, "Stop")
        self.stop_btn.clicked.connect(self._stop)
        self.stop_btn.setEnabled(False)
        self.ring = ProgressRing()
        self.ring.setFixedSize(28, 28)
        self.ring.setVisible(False)
        ctl_row.addWidget(self.start_btn)
        ctl_row.addWidget(self.stop_btn)
        ctl_row.addWidget(self.ring)
        ctl_row.addStretch(1)
        root.addLayout(ctl_row)

        # ---- log ---------------------------------------------------------
        root.addWidget(SubtitleLabel("Log"))
        self.log = PlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Cascadia Mono, Consolas, monospace", 9))
        root.addWidget(self.log, 1)

    # ---------------------------------------------------------------- ui --
    def _browse_apk(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select a Flutter APK", "", APK_FILTER)
        if path:
            self.apk_edit.setText(path)

    def _browse_out(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select an output folder")
        if path:
            self.out_edit.setText(path)

    def _append(self, text: str) -> None:
        self.log.appendPlainText(text.rstrip("\n"))
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ------------------------------------------------------------- run --
    def _start(self) -> None:
        apk = self.apk_edit.text().strip().strip('"')
        if not apk:
            self._toast("No input", "Choose an APK first.", error=True)
            return
        if not os.path.exists(apk):
            self._toast("Not found", "That path does not exist:\n%s" % apk, error=True)
            return

        out = self.out_edit.text().strip().strip('"')
        if not out:
            base = os.path.splitext(os.path.basename(os.path.abspath(apk)))[0]
            out = os.path.join(os.path.dirname(os.path.abspath(apk)),
                               base + "_decompiled")
            self.out_edit.setText(out)

        argv = ["-m", "flutter_decompile.frontend",
                "--decompile", apk, "--out", out, "--yes"]
        if self.quick_cb.isChecked():
            argv.append("--quick")

        self.log.clear()
        self._append("$ python " + " ".join(argv))
        self._append("")

        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._on_output)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)
        # Unbuffered, so the log streams instead of arriving in one lump at exit.
        env = self.proc.processEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        self.proc.setProcessEnvironment(env)
        self.proc.start(sys.executable, argv)

        self._set_running(True)

    def _stop(self) -> None:
        if self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning:
            self._append("\n[stopping — killing the pipeline]")
            self.proc.kill()

    def _set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.apk_edit.setEnabled(not running)
        self.out_edit.setEnabled(not running)
        self.quick_cb.setEnabled(not running)
        self.ring.setVisible(running)

    def _on_output(self) -> None:
        if self.proc is None:
            return
        data = self.proc.readAllStandardOutput().data().decode("utf-8", "replace")
        for line in data.splitlines():
            self._append(line)

    def _on_error(self, _err) -> None:
        if self.proc is not None:
            self._append("[process error] " + self.proc.errorString())

    def _on_finished(self, code: int, _status) -> None:
        self._set_running(False)
        self._append("\n[finished, exit code %d]" % code)
        if code == 0:
            out = self.out_edit.text().strip()
            dart = os.path.join(out, "dart")
            self._toast("Done", "Reconstructed Dart written to:\n%s"
                        % (dart if os.path.isdir(dart) else out), error=False)
        else:
            self._toast("Exited with code %d" % code,
                        "See the log for what went wrong.", error=True)
        self.proc = None

    def _toast(self, title: str, content: str, error: bool) -> None:
        make = InfoBar.error if error else InfoBar.success
        make(title=title, content=content, orient=Qt.Orientation.Vertical,
             isClosable=True, position=InfoBarPosition.TOP_RIGHT,
             duration=6000 if error else 4000, parent=self)

    def closeEvent(self, event) -> None:
        if self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning:
            self.proc.kill()
            self.proc.waitForFinished(2000)
        event.accept()


def main(argv=None) -> int:
    app = QApplication(sys.argv if argv is None else argv)
    setTheme(Theme.AUTO)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
