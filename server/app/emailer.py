"""Outbound email over SMTP, with a no-config dev fallback.

If ``SMTP_HOST`` is set the message is sent through that relay; otherwise the
message is logged to stdout and nothing leaves the box. That keeps the entire
account flow runnable on a laptop and in CI with no mail provider, while a single
environment variable switches on real delivery in production.

Only the standard library is used (``smtplib`` + ``email.message``), so there is
no new dependency to install.
"""
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from . import config

log = logging.getLogger("pdfte.email")


def send_email(to: str, subject: str, text_body: str, html_body: str) -> bool:
    """Send one message. Returns True if handed to an SMTP server, False if it
    was only logged (no SMTP configured) or delivery failed. Never raises, so a
    mail hiccup cannot break the request that triggered it."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((config.EMAIL_FROM_NAME, config.EMAIL_FROM))
    msg["To"] = to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    if not config.SMTP_HOST:
        log.warning("[email:dev-fallback] no SMTP configured, not sending. "
                    "to=%s subject=%s\n%s", to, subject, text_body)
        return False

    try:
        if config.SMTP_SECURITY == "ssl":
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,
                                  context=ctx, timeout=15) as s:
                _login_and_send(s, msg)
        else:
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT,
                              timeout=15) as s:
                if config.SMTP_SECURITY == "starttls":
                    s.starttls(context=ssl.create_default_context())
                _login_and_send(s, msg)
        return True
    except Exception:  # noqa: BLE001 - never let mail failure break the flow
        log.exception("[email] send failed to=%s subject=%s", to, subject)
        return False


def _login_and_send(server: smtplib.SMTP, msg: EmailMessage) -> None:
    if config.SMTP_USER:
        server.login(config.SMTP_USER, config.SMTP_PASSWORD)
    server.send_message(msg)


# --- the two messages the account system sends ------------------------------
def _shell(heading: str, intro: str, button_label: str, link: str,
           footer: str) -> str:
    """A small branded HTML email in the app's clay/paper palette. Plain inline
    styles only, since email clients ignore stylesheets."""
    return f"""\
<div style="margin:0;padding:24px;background:#ECE6DC;
  font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#2A2520">
  <div style="max-width:520px;margin:0 auto;background:#FBF9F5;border:1px solid #E2DACD;
    border-radius:16px;overflow:hidden">
    <div style="padding:20px 28px;border-bottom:1px solid #E2DACD;font-weight:700;
      font-size:16px;letter-spacing:-.01em;color:#8B3E23">PDF Text Editor</div>
    <div style="padding:28px">
      <h1 style="margin:0 0 12px;font-size:21px;line-height:1.2;color:#2A2520">{heading}</h1>
      <p style="margin:0 0 22px;font-size:15px;line-height:1.55;color:#5C5346">{intro}</p>
      <a href="{link}" style="display:inline-block;background:#AA4E2C;color:#fff;
        text-decoration:none;font-weight:600;font-size:15px;padding:12px 22px;
        border-radius:10px">{button_label}</a>
      <p style="margin:22px 0 0;font-size:13px;line-height:1.5;color:#897E6E">
        Or paste this link into your browser:<br>
        <span style="color:#5C5346;word-break:break-all">{link}</span></p>
      <p style="margin:22px 0 0;font-size:13px;line-height:1.5;color:#897E6E">{footer}</p>
    </div>
  </div>
</div>"""


def send_verification_email(to: str, link: str) -> bool:
    subject = "Confirm your email for PDF Text Editor"
    text = ("Confirm your email for PDF Text Editor.\n\n"
            f"Open this link to confirm the address:\n{link}\n\n"
            f"The link is valid for {config.VERIFY_TOKEN_TTL_HOURS} hours. "
            "If you did not create an account, you can ignore this email.")
    html = _shell(
        "Confirm your email",
        "Thanks for creating a PDF Text Editor account. Confirm this address to "
        "finish setting it up.",
        "Confirm email", link,
        f"This link is valid for {config.VERIFY_TOKEN_TTL_HOURS} hours. "
        "If you did not create an account, you can ignore this email.")
    return send_email(to, subject, text, html)


def send_reset_email(to: str, link: str) -> bool:
    subject = "Reset your PDF Text Editor password"
    text = ("Reset your PDF Text Editor password.\n\n"
            f"Open this link to choose a new password:\n{link}\n\n"
            f"The link is valid for {config.RESET_TOKEN_TTL_HOURS} hours. "
            "If you did not ask to reset your password, you can ignore this "
            "email and nothing will change.")
    html = _shell(
        "Reset your password",
        "We received a request to reset the password on your PDF Text Editor "
        "account. Choose a new one below.",
        "Choose a new password", link,
        f"This link is valid for {config.RESET_TOKEN_TTL_HOURS} hours. "
        "If you did not ask for this, you can ignore this email and nothing "
        "will change.")
    return send_email(to, subject, text, html)
