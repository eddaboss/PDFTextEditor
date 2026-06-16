"""Application entry point.

Launch semantics (navigation M3):

* ``pdftexteditor file.pdf …`` opens each existing ``.pdf`` argument in its
  own tab and SKIPS session restore (an explicit open wins over history).
* An argv-less launch restores the previous session's tabs
  (``MainWindow._restore_session``; written by the guarded close).
* Finder "Open With" / dock drops arrive as ``QEvent.FileOpen`` on the
  application object (macOS never passes them through argv) -- an app-level
  event filter routes them to ``open_path``.

The ``launch.py`` / PyInstaller contract is untouched: no new data files, and
this module stays inside the package so ``collect_submodules`` bundles it.
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication

from .ui import theme
from .ui.main_window import MainWindow


class _FileOpenFilter(QObject):
    """Routes macOS ``QEvent.FileOpen`` (Finder "Open With", dock drops) to
    the window. Installed application-wide; everything but the one event type
    falls straight through."""

    def __init__(self, window: MainWindow):
        super().__init__(window)
        self._window = window

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.FileOpen:
            path = event.file()
            if path and path.lower().endswith(".pdf"):
                self._window.open_path(path)
                return True
        return super().eventFilter(obj, event)


def _disable_macos_font_smoothing() -> None:
    """Turn off macOS 'font smoothing' (stem darkening) for this app.

    macOS draws on-screen text through Core Text with a contrast boost that
    renders glyphs HEAVIER than the same face rasterized by fitz/MuPDF. The page
    is a fitz pixmap, but the inline editor is live Qt text, so opening a box to
    edit makes its glyphs look bolder than the baked page (and already-bold text
    looks double-bold) even though the resolved weight is identical. Clearing the
    smoothing default makes the editor's strokes match the page and the saved
    output. The write also persists to the app's preferences domain, so it holds
    on the next launch even if this runtime set lands after Core Graphics' first
    read. No-op off macOS (Windows/Linux have no equivalent pass)."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        import ctypes.util

        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        msg = objc.objc_msgSend

        def call(recv, sel, *args, argtypes=None, restype=ctypes.c_void_p):
            msg.restype = restype
            msg.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + (argtypes or [])
            return msg(recv, objc.sel_registerName(sel), *args)

        ns_string = objc.objc_getClass(b"NSString")

        def nsstr(raw: bytes):
            return call(ns_string, b"stringWithUTF8String:", raw,
                        argtypes=[ctypes.c_char_p])

        defaults = call(objc.objc_getClass(b"NSUserDefaults"),
                        b"standardUserDefaults")
        call(defaults, b"setBool:forKey:", True,
             nsstr(b"CGFontRenderingFontSmoothingDisabled"),
             argtypes=[ctypes.c_bool, ctypes.c_void_p], restype=None)
        call(defaults, b"setInteger:forKey:", 0,
             nsstr(b"AppleFontSmoothing"),
             argtypes=[ctypes.c_longlong, ctypes.c_void_p], restype=None)
    except Exception:  # noqa: BLE001 - a cosmetic default must never block launch
        pass


def _prompt_update(window, meta) -> None:
    """One-click 'Update available' prompt (installed builds only)."""
    from PySide6.QtWidgets import QMessageBox

    from . import __version__, updater
    ver = getattr(meta, "version", None) or str(meta)
    btn = QMessageBox.question(
        window, "Update available",
        f"Version {ver} is available. Install it and restart now?")
    if btn == QMessageBox.StandardButton.Yes and updater.apply_update(__version__):
        QMessageBox.information(
            window, "Update installed",
            "The update is ready. Quit and reopen to finish updating.")


def _maybe_onboard(window) -> None:
    """First launch only: offer to redeem a website sign-in code, or sign in /
    create an account. Optional and skippable; shown once, never if already
    signed in. Uses a short timer so the main window paints first."""
    from PySide6.QtCore import QSettings
    s = QSettings("eddaboss", "PDF Text Editor")
    if s.value("account/token", "") or s.value("account/onboarded", False,
                                                type=bool):
        return
    s.setValue("account/onboarded", True)  # show it at most once

    def _show():
        from .ui.onboarding_dialog import OnboardingDialog
        OnboardingDialog(window).exec()
    QTimer.singleShot(600, _show)


def _update_check_due() -> bool:
    """Throttle the on-launch update check to once a day so we are not hitting the
    update server on every single launch. The manual check in Settings is
    unaffected. Returns True (and records 'now') when a check is due."""
    from PySide6.QtCore import QDateTime, QSettings
    s = QSettings("eddaboss", "PDF Text Editor")
    now = QDateTime.currentMSecsSinceEpoch()
    last = int(s.value("update/last_check_ms", 0) or 0)
    if now - last < 24 * 60 * 60 * 1000:
        return False
    s.setValue("update/last_check_ms", now)
    return True


def main() -> int:
    # Must run before any window renders text so Core Graphics picks it up.
    _disable_macos_font_smoothing()
    app = QApplication(sys.argv)
    app.setApplicationName("PDF Text Editor")
    # Match Qt's native color scheme to the Clay mode we picked from the OS
    # appearance (theme detected it at import). This keeps the native macOS title
    # bar (and native dialogs/controls) light-over-light or dark-over-dark instead
    # of a mismatched title bar. Qt 6.8+ API; the Info.plist NSAppearance is the
    # fallback on older Qt.
    try:
        scheme = (Qt.ColorScheme.Dark if theme.current_mode() == "dark"
                  else Qt.ColorScheme.Light)
        app.styleHints().setColorScheme(scheme)
    except (AttributeError, TypeError):
        pass
    # Apply the Clay theme at the APPLICATION level so every top-level window AND
    # dialog inherits it (a window-level stylesheet does not reliably reach
    # separately-created dialogs). MainWindow also sets it on itself so a
    # directly-constructed window (e.g. the headless tests) is styled too.
    app.setStyleSheet(theme.global_stylesheet())
    # Register the app-bundled DejaVu faces with Qt up front so they render in
    # the editor and the family picker even before the first document opens (a
    # PDF in DejaVu Sans -- mPDF's default -- is then editable in its real face
    # on a machine that does not have it installed).
    from .font_engine import FontEngine
    FontEngine.register_bundled_fonts()
    window = MainWindow()
    window._restore_window_geometry()   # reopen at the last size + position
    window._enable_persistence()        # flush geometry+session on crash/quit
    window.show()
    app.installEventFilter(_FileOpenFilter(window))
    cli_paths = [p for p in sys.argv[1:]
                 if p.lower().endswith(".pdf") and os.path.isfile(p)]
    for path in cli_paths:
        window.open_path(path)
    # Crash recovery wins over session restore: if a previous run went down with
    # unsaved work, reopen that (the recovered tabs are the session that
    # mattered); otherwise restore the last session as before.
    recovered = window._offer_recovery()
    if not cli_paths and not recovered:
        window._restore_session()
    # First-run account onboarding (optional, skippable): redeem a website
    # sign-in code, or sign in / create an account. Shown once.
    _maybe_onboard(window)
    # On-launch update check (installed builds only): non-blocking; offers a
    # one-click install when a newer signed release exists on this channel.
    from . import __version__, updater
    if updater.updates_supported() and _update_check_due():
        window._update_checker = updater.UpdateChecker()
        window._update_checker.available.connect(
            lambda meta: _prompt_update(window, meta))
        QTimer.singleShot(2500, lambda: window._update_checker.check(__version__))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
