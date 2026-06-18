"""Email notifier — supervisor approval fallback when Slack is unavailable.

Plain stdlib smtplib/email (no new dependency). Sends a candidate summary with
signed one-click approval deep-links back to the API. The blocking SMTP call runs
in a worker thread so it never stalls the event loop. A no-op when SMTP is not
configured.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("cassa.transient.email")


class EmailNotifier:
    def __init__(self, settings):
        self.s = settings

    @property
    def enabled(self) -> bool:
        return bool(self.s.smtp_host and self.s.smtp_to)

    async def send(self, subject: str, body_text: str, body_html: str | None = None) -> None:
        if not self.enabled:
            return
        try:
            await asyncio.to_thread(self._send_sync, subject, body_text, body_html)
        except Exception:
            log.exception("email send failed")

    def _send_sync(self, subject: str, body_text: str, body_html: str | None) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.s.smtp_from or self.s.smtp_user
        msg["To"] = self.s.smtp_to
        msg.set_content(body_text)
        if body_html:
            msg.add_alternative(body_html, subtype="html")
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=20) as srv:
            srv.ehlo()
            try:
                srv.starttls(context=ssl.create_default_context())
                srv.ehlo()
            except smtplib.SMTPException:
                pass  # server without STARTTLS (e.g. local relay)
            if self.s.smtp_user:
                srv.login(self.s.smtp_user, self.s.smtp_password)
            srv.send_message(msg)
        log.info("approval email sent: %s", subject)
