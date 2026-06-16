"""Unified Settings / Preferences surface.

One place for app preferences: the macOS OCR engine choice, software updates,
and the optional account. New sections self-source their data so the dialog's
constructor signature stays stable (the window builds it the same way).

Non-modal by construction (``show()``, never ``exec()`` -- the offscreen-test
rule). The object name ``SettingsDialog`` is load-bearing for tests + QSS.
Network calls run on a worker thread and deliver results back on the GUI thread
via ``_asyncDone``, so the dialog never freezes.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .. import __version__, appconfig, cloud, updater

_ENGINE_LABELS = {"applevision": "Apple Vision", "rapidocr": "RapidOCR"}
_TOKEN_KEY = "account/token"
_EMAIL_KEY = "account/email"


class SettingsDialog(QDialog):
    """App settings. ``current_engine`` is the effective OCR engine name;
    ``on_engine_changed(name)`` persists a new choice; ``is_mac`` gates the OCR
    chooser (Windows has only RapidOCR)."""

    # (callback, result) marshalled from a worker thread to the GUI thread.
    _asyncDone = Signal(object, object)

    def __init__(self, *, is_mac: bool, current_engine: str,
                 on_engine_changed, parent=None):
        super().__init__(parent)
        self.setObjectName("SettingsDialog")
        self.setWindowTitle("Settings")
        self.setModal(False)
        self._on_engine_changed = on_engine_changed
        self._engine_buttons: dict = {}
        self._pending_update = None
        self._asyncDone.connect(lambda cb, res: cb(res))

        col = QVBoxLayout(self)
        col.setContentsMargins(28, 24, 28, 24)
        col.setSpacing(14)

        col.addWidget(self._section_title("OCR Engine"))
        col.addWidget(self._mac_ocr_section(current_engine) if is_mac
                      else self._windows_ocr_section())
        col.addWidget(self._divider())
        col.addWidget(self._section_title("Software Update"))
        col.addWidget(self._updates_section())
        col.addWidget(self._divider())
        col.addWidget(self._section_title("Account"))
        self._account_box = QWidget()
        QVBoxLayout(self._account_box).setContentsMargins(0, 0, 0, 0)
        col.addWidget(self._account_box)
        col.addStretch(1)
        self._rebuild_account()

    # -- shared builders -------------------------------------------------
    def _section_title(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("SettingsSectionTitle")
        lbl.setFont(theme.ui_font(13, semibold=True))
        return lbl

    def _hint(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("SettingsHint")
        lbl.setFont(theme.ui_font(11))
        lbl.setWordWrap(True)
        return lbl

    def _divider(self) -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.HLine)
        f.setObjectName("SettingsDivider")
        return f

    def _run_async(self, work, on_done) -> None:
        def runner():
            try:
                res = work()
            except Exception as exc:  # noqa: BLE001
                res = ("__error__", str(exc))
            self._asyncDone.emit(on_done, res)
        threading.Thread(target=runner, daemon=True).start()

    # -- OCR -------------------------------------------------------------
    def _mac_ocr_section(self, current_engine: str) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        group = QButtonGroup(self)
        rows = (
            ("applevision",
             "Built into macOS. Fastest, most accurate here, no download."),
            ("rapidocr",
             "Cross-platform engine. Downloads a one-time component the first "
             "time you use it."),
        )
        for name, hint in rows:
            rb = QRadioButton(_ENGINE_LABELS[name])
            rb.setObjectName(f"OcrEngine_{name}")
            rb.setFont(theme.ui_font(12))
            rb.setChecked(current_engine == name)
            rb.toggled.connect(lambda on, n=name: self._pick(n) if on else None)
            group.addButton(rb)
            self._engine_buttons[name] = rb
            v.addWidget(rb)
            v.addWidget(self._hint(hint))
        return box

    def _windows_ocr_section(self) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        lbl = QLabel(_ENGINE_LABELS["rapidocr"])
        lbl.setFont(theme.ui_font(12))
        v.addWidget(lbl)
        v.addWidget(self._hint(
            "RapidOCR is the OCR engine on Windows. It downloads a one-time "
            "component the first time you use OCR."))
        return box

    def _pick(self, name: str) -> None:
        if self._on_engine_changed is not None:
            self._on_engine_changed(name)

    def set_current_engine(self, name: str) -> None:
        rb = self._engine_buttons.get(name)
        if rb is not None and not rb.isChecked():
            rb.setChecked(True)

    # -- Software Update -------------------------------------------------
    def _updates_section(self) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        chan = "stable" if not appconfig.IS_DEV else "dev"
        lbl = QLabel(f"Version {__version__}  ·  {chan} channel")
        lbl.setFont(theme.ui_font(12))
        v.addWidget(lbl)

        row = QHBoxLayout()
        self._btn_check = QPushButton("Check for Updates")
        self._btn_check.setObjectName("CheckUpdatesBtn")
        self._btn_check.clicked.connect(self._check_updates)
        self._btn_install = QPushButton("Install && Restart")
        self._btn_install.setObjectName("InstallUpdateBtn")
        self._btn_install.clicked.connect(self._install_update)
        self._btn_install.hide()
        row.addWidget(self._btn_check)
        row.addWidget(self._btn_install)
        row.addStretch(1)
        v.addLayout(row)

        self._upd_status = self._hint("")
        v.addWidget(self._upd_status)
        if not updater.updates_supported():
            self._upd_status.setText(
                "Running from source. Self-update applies to installed builds; "
                "you can still check what the channel offers.")
        return box

    def _check_updates(self) -> None:
        self._btn_check.setEnabled(False)
        self._btn_install.hide()
        self._upd_status.setText("Checking…")
        self._run_async(lambda: updater.check_for_updates(__version__),
                        self._on_update_checked)

    def _on_update_checked(self, result) -> None:
        self._btn_check.setEnabled(True)
        if isinstance(result, tuple) and result and result[0] == "__error__":
            self._upd_status.setText("Could not check for updates right now.")
            return
        if result is None:
            self._upd_status.setText("You’re up to date.")
            return
        self._pending_update = result
        ver = getattr(result, "version", None) or str(result)
        self._upd_status.setText(f"Update available: {ver}")
        if updater.updates_supported():
            self._btn_install.show()

    def _install_update(self) -> None:
        self._btn_install.setEnabled(False)
        self._upd_status.setText("Downloading and installing…")
        self._run_async(lambda: updater.apply_update(__version__),
                        self._on_update_applied)

    def _on_update_applied(self, ok) -> None:
        if ok is True:
            self._upd_status.setText("Update installed. Quit and reopen to finish.")
        else:
            self._btn_install.setEnabled(True)
            self._upd_status.setText("Update could not be installed.")

    # -- Account ---------------------------------------------------------
    def _store(self) -> QSettings:
        return QSettings("eddaboss", "PDF Text Editor")

    def _rebuild_account(self) -> None:
        lay = self._account_box.layout()
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        token = self._store().value(_TOKEN_KEY, "")
        if token:
            email = self._store().value(_EMAIL_KEY, "") or "your account"
            lay.addWidget(self._hint(f"Signed in as {email}."))
            btn = QPushButton("Sign out")
            btn.clicked.connect(self._sign_out)
            r = QHBoxLayout()
            r.addWidget(btn)
            r.addStretch(1)
            lay.addLayout(r)
        else:
            lay.addWidget(self._hint(
                "Optional. The editor works fully without an account; sign in "
                "to carry settings across devices later."))
            # Sign-in code from the website's download gate: redeem it to sign in
            # here with no password.
            lay.addWidget(self._hint(
                "Have a sign-in code from the website? Paste it to sign in."))
            self._code = QLineEdit()
            self._code.setPlaceholderText("Sign-in code")
            cr = QHBoxLayout()
            cr.addWidget(self._code, 1)
            b_code = QPushButton("Apply")
            b_code.clicked.connect(self._apply_setup_code)
            cr.addWidget(b_code)
            lay.addLayout(cr)
            self._email = QLineEdit()
            self._email.setPlaceholderText("Email")
            self._pw = QLineEdit()
            self._pw.setPlaceholderText("Password")
            self._pw.setEchoMode(QLineEdit.Password)
            # Pin the system UI font + height so text renders correctly and fits
            # (an unset font falls back to Qt's default and clips on macOS).
            for w in (self._code, self._email, self._pw):
                w.setFont(theme.ui_font())
                w.setMinimumHeight(34)
            lay.addWidget(self._email)
            lay.addWidget(self._pw)
            r = QHBoxLayout()
            b_in = QPushButton("Sign in")
            b_in.clicked.connect(lambda: self._auth(cloud.login))
            b_up = QPushButton("Create account")
            b_up.clicked.connect(lambda: self._auth(cloud.register))
            r.addWidget(b_in)
            r.addWidget(b_up)
            r.addStretch(1)
            lay.addLayout(r)
            self._acct_status = self._hint("")
            lay.addWidget(self._acct_status)

    def _apply_setup_code(self) -> None:
        code = self._code.text().strip()
        if not code:
            self._acct_status.setText("Enter your setup code.")
            return
        self._acct_status.setText("Checking your code…")
        self._run_async(lambda: cloud.claim_setup_code(code),
                        self._on_setup_code)

    def _on_setup_code(self, result) -> None:
        if not isinstance(result, tuple) or len(result) != 2:
            self._acct_status.setText("Something went wrong.")
            return
        status, body = result
        if status == 200 and isinstance(body, dict) and body.get("token"):
            # The code signs you straight in.
            self._store().setValue(_TOKEN_KEY, body["token"])
            self._store().setValue(
                _EMAIL_KEY, (body.get("user") or {}).get("email", ""))
            self._rebuild_account()
        else:
            msg = body.get("detail") if isinstance(body, dict) else ""
            self._acct_status.setText(msg or "That code did not work.")

    def _auth(self, fn) -> None:
        email = self._email.text().strip()
        pw = self._pw.text()
        if not email or not pw:
            self._acct_status.setText("Enter an email and password.")
            return
        self._acct_status.setText("Working…")
        self._run_async(lambda: fn(email, pw), self._on_auth)

    def _on_auth(self, result) -> None:
        if not isinstance(result, tuple) or len(result) != 2:
            self._acct_status.setText("Something went wrong.")
            return
        status, body = result
        if status == 200 and isinstance(body, dict) and body.get("token"):
            self._store().setValue(_TOKEN_KEY, body["token"])
            self._store().setValue(
                _EMAIL_KEY, body.get("user", {}).get("email", ""))
            self._rebuild_account()
        else:
            msg = body.get("detail") if isinstance(body, dict) else ""
            self._acct_status.setText(msg or "Sign in failed.")

    def _sign_out(self) -> None:
        s = self._store()
        s.remove(_TOKEN_KEY)
        s.remove(_EMAIL_KEY)
        self._rebuild_account()
