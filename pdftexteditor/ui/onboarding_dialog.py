"""First-run account onboarding (optional, skippable).

Three ways in, all ending signed in:
  * paste a one-time sign-in code from the website (no password),
  * log in with email + password, or
  * create an account.

The editor works fully without any of this, so the dialog can always be closed.
Network calls go through ``cloud`` (stdlib urllib) on a worker thread.
"""
import threading

from PySide6.QtCore import QSettings, Signal
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

from .. import cloud
from . import theme

_TOKEN_KEY = "account/token"
_EMAIL_KEY = "account/email"


class OnboardingDialog(QDialog):
    """Shown once on first launch when no account is stored. Accepts (signs in)
    or is dismissed; either way the editor proceeds normally."""

    # (callback, result) marshalled from a worker thread back to the GUI thread.
    _asyncDone = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("OnboardingDialog")
        self.setWindowTitle("Welcome")
        self.setModal(True)
        self.setMinimumWidth(430)
        self._mode = "login"
        self._asyncDone.connect(lambda cb, res: cb(res))

        col = QVBoxLayout(self)
        col.setContentsMargins(30, 26, 30, 24)
        col.setSpacing(12)

        title = QLabel("Welcome to PDF Text Editor")
        title.setFont(theme.ui_font(17, semibold=True))
        col.addWidget(title)
        col.addWidget(self._hint(
            "Set up your account, or skip it. The editor works fully without "
            "one; an account just carries your settings across devices later."))

        # Primary path: redeem the one-time code shown on the website.
        col.addWidget(self._hint("Have a sign-in code from the website?"))
        self._code = QLineEdit()
        self._code.setPlaceholderText("Sign-in code")
        self._code.returnPressed.connect(self._use_code)
        crow = QHBoxLayout()
        crow.addWidget(self._code, 1)
        b_code = QPushButton("Continue")
        b_code.setDefault(True)
        b_code.clicked.connect(self._use_code)
        crow.addWidget(b_code)
        col.addLayout(crow)

        col.addWidget(self._divider())

        # The two "without a code" entry points.
        lrow = QHBoxLayout()
        b_login = QPushButton("Log in without a code")
        b_login.clicked.connect(lambda: self._set_mode("login"))
        b_reg = QPushButton("Create account without a code")
        b_reg.clicked.connect(lambda: self._set_mode("register"))
        lrow.addWidget(b_login)
        lrow.addWidget(b_reg)
        lrow.addStretch(1)
        col.addLayout(lrow)

        # The email/password form, hidden until one of the two is chosen.
        self._form = QWidget()
        fl = QVBoxLayout(self._form)
        fl.setContentsMargins(0, 4, 0, 0)
        fl.setSpacing(8)
        self._name = QLineEdit()
        self._name.setPlaceholderText("Name (optional)")
        self._email = QLineEdit()
        self._email.setPlaceholderText("Email")
        self._pw = QLineEdit()
        self._pw.setPlaceholderText("Password")
        self._pw.setEchoMode(QLineEdit.Password)
        self._pw.returnPressed.connect(self._submit_form)
        # Pin the system UI font + a comfortable height on every field. Without an
        # explicit font a QLineEdit falls back to Qt's default family, which on
        # macOS mis-renders (per-glyph fallback) and clips inside the box.
        for w in (self._code, self._name, self._email, self._pw):
            w.setFont(theme.ui_font())
            w.setMinimumHeight(34)
        fl.addWidget(self._name)
        fl.addWidget(self._email)
        fl.addWidget(self._pw)
        self._submit = QPushButton("Sign in")
        self._submit.clicked.connect(self._submit_form)
        fl.addWidget(self._submit)
        self._form.setVisible(False)
        col.addWidget(self._form)

        self._status = self._hint("")
        col.addWidget(self._status)
        col.addStretch(1)

        brow = QHBoxLayout()
        brow.addStretch(1)
        later = QPushButton("Maybe later")
        later.clicked.connect(self.reject)
        brow.addWidget(later)
        col.addLayout(brow)

    # -- helpers ---------------------------------------------------------
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

    def _store(self) -> QSettings:
        return QSettings("eddaboss", "PDF Text Editor")

    def _run_async(self, work, on_done) -> None:
        def runner():
            try:
                res = work()
            except Exception as exc:  # noqa: BLE001
                res = (0, {"detail": str(exc)})
            self._asyncDone.emit(on_done, res)
        threading.Thread(target=runner, daemon=True).start()

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        self._name.setVisible(mode == "register")
        self._submit.setText("Create account" if mode == "register"
                             else "Sign in")
        self._form.setVisible(True)
        self._status.setText("")
        self._email.setFocus()

    def _save_and_accept(self, body: dict) -> None:
        self._store().setValue(_TOKEN_KEY, body["token"])
        email = (body.get("user") or {}).get("email", "") or body.get("email", "")
        self._store().setValue(_EMAIL_KEY, email)
        self.accept()

    def _signed_in(self, result, fallback: str) -> None:
        if not isinstance(result, tuple) or len(result) != 2:
            self._status.setText("Something went wrong.")
            return
        status, body = result
        if status == 200 and isinstance(body, dict) and body.get("token"):
            self._save_and_accept(body)
        else:
            msg = body.get("detail") if isinstance(body, dict) else ""
            self._status.setText(msg or fallback)

    # -- code path -------------------------------------------------------
    def _use_code(self) -> None:
        code = self._code.text().strip()
        if not code:
            self._status.setText("Enter your sign-in code.")
            return
        self._status.setText("Checking your code…")
        self._run_async(lambda: cloud.claim_setup_code(code),
                        lambda r: self._signed_in(r, "That code did not work."))

    # -- login / register paths -----------------------------------------
    def _submit_form(self) -> None:
        email = self._email.text().strip()
        pw = self._pw.text()
        if not email or not pw:
            self._status.setText("Enter an email and password.")
            return
        self._status.setText("Working…")
        if self._mode == "register":
            name = self._name.text().strip()
            self._run_async(lambda: cloud.register(email, pw, name),
                            lambda r: self._signed_in(r, "Could not create the "
                                                      "account."))
        else:
            self._run_async(lambda: cloud.login(email, pw),
                            lambda r: self._signed_in(r, "Sign in failed."))
