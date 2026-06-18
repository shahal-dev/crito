"""ApprovalService — show candidates to the supervisor and capture the decision.

Two channels, both driving the same CandidateService transitions (the console
mirrors all of them since state lives in the DB):

  • Slack (primary): a CASSA-owned ``slack_sdk`` Socket Mode bot posts an
    interactive card per candidate with Approve→Queue / Approve→Execute / Reject
    buttons. Socket Mode is an *outbound* WebSocket, so it works behind the
    observatory's NAT with no public URL. The deployed service uses its own bot
    tokens — it does NOT use Claude's MCP Slack tools (those exist only in a Claude
    session, not in this process).
  • Email (fallback): a summary with HMAC-signed one-click approval deep-links to
    ``GET /api/transient/approve`` — works even when Slack is down.

Everything degrades gracefully: no tokens → Slack disabled; no SMTP → email
disabled; ``slack_sdk`` not installed → Slack disabled. The app still runs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

from .email_notify import EmailNotifier

log = logging.getLogger("cassa.transient.approvals")

# action_id on the Slack buttons / token action → CandidateService call
_ACTION_MAP = {
    "approve_queue": ("approve", "queue"),
    "approve_execute": ("approve", "execute"),
    "reject": ("reject", None),
}


# ----------------------------------------------------------- signed deep-links
def sign_token(candidate_id: str, action: str, secret: str, ttl: int = 86400) -> str:
    """Opaque HMAC token encoding (candidate_id, action, expiry) for email links."""
    exp = int(time.time()) + ttl
    payload = f"{candidate_id}|{action}|{exp}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def verify_token(token: str, secret: str) -> tuple[str, str]:
    """Return (candidate_id, action) for a valid, unexpired token, else ValueError."""
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        cid, action, exp, sig = raw.rsplit("|", 3)
    except Exception:
        raise ValueError("malformed token")
    expected = hmac.new(secret.encode(), f"{cid}|{action}|{exp}".encode(),
                        hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        raise ValueError("bad signature")
    if int(exp) < int(time.time()):
        raise ValueError("token expired")
    if action not in ("queue", "execute", "reject"):
        raise ValueError("bad action")
    return cid, action


def _fmt(v, suffix=""):
    return f"{v}{suffix}" if v is not None else "—"


class ApprovalService:
    def __init__(self, settings):
        self.s = settings
        self.candidates = None          # wired via bind() to avoid a construction cycle
        self.email = EmailNotifier(settings)
        self.web = None
        self.sm_client = None

    def bind(self, candidates) -> None:
        self.candidates = candidates

    # ----------------------------------------------------------- slack lifecycle
    async def start(self) -> None:
        if not (self.s.slack_bot_token and self.s.slack_app_token and self.s.slack_channel):
            log.info("Slack approval disabled (no tokens configured)")
            return
        try:
            from slack_sdk.web.async_client import AsyncWebClient
            from slack_sdk.socket_mode.aiohttp import SocketModeClient
        except ImportError:
            log.warning("slack_sdk not installed — Slack approval disabled "
                        "(`pip install slack_sdk` to enable)")
            return
        try:
            self.web = AsyncWebClient(token=self.s.slack_bot_token)
            self.sm_client = SocketModeClient(app_token=self.s.slack_app_token,
                                              web_client=self.web)
            self.sm_client.socket_mode_request_listeners.append(self._on_request)
            await self.sm_client.connect()
            log.info("Slack Socket Mode connected (channel %s)", self.s.slack_channel)
        except Exception:
            log.exception("Slack connect failed — approval falls back to console/email")

    async def stop(self) -> None:
        if self.sm_client is not None:
            try:
                await self.sm_client.disconnect()
            except Exception:
                log.debug("slack disconnect failed", exc_info=True)

    # --------------------------------------------------------------- notify
    async def notify(self, candidate: dict) -> None:
        await self._slack_post(candidate)
        await self._email_post(candidate)

    def _summary_lines(self, c: dict) -> list[str]:
        return [
            f"*Class*: {c.get('class_label') or 'unknown'}  (p={_fmt(c.get('class_prob'))})",
            f"*RA/Dec*: {_fmt(round(c['ra_deg'], 3))}, {_fmt(round(c['dec_deg'], 3))}",
            f"*Mag*: {_fmt(c.get('mag'))}   *Peak alt*: {_fmt(c.get('max_alt_deg'), '°')}"
            f"   *Airmass*: {_fmt(c.get('min_airmass'))}",
            f"*Moon sep*: {_fmt(c.get('moon_sep_deg'), '°')}   *Score*: {_fmt(c.get('score'))}",
            f"*Window (UTC)*: {_fmt(c.get('window_start_utc'))} → {_fmt(c.get('window_end_utc'))}",
        ]

    def slack_blocks(self, c: dict) -> list[dict]:
        oid = c.get("alert_id") or c["id"]
        return [
            {"type": "header", "text": {"type": "plain_text",
             "text": f"🔭 {c.get('class_label') or 'transient'} candidate {oid}"}},
            {"type": "section", "text": {"type": "mrkdwn",
             "text": "\n".join(self._summary_lines(c))}},
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"<https://alerce.online/object/{oid}|View on ALeRCE>"}},
            {"type": "actions", "block_id": c["id"], "elements": [
                {"type": "button", "style": "primary", "action_id": "approve_queue",
                 "value": c["id"], "text": {"type": "plain_text", "text": "Approve → Queue"}},
                {"type": "button", "action_id": "approve_execute", "value": c["id"],
                 "text": {"type": "plain_text", "text": "Approve → Execute"}},
                {"type": "button", "style": "danger", "action_id": "reject",
                 "value": c["id"], "text": {"type": "plain_text", "text": "Reject"}},
            ]},
        ]

    async def _slack_post(self, c: dict) -> None:
        if self.web is None:
            return
        try:
            await self.web.chat_postMessage(
                channel=self.s.slack_channel,
                text=f"New {c.get('class_label') or 'transient'} candidate {c.get('alert_id')}",
                blocks=self.slack_blocks(c),
            )
        except Exception:
            log.exception("Slack post failed for candidate %s", c["id"])

    async def _email_post(self, c: dict) -> None:
        if not self.email.enabled:
            return
        oid = c.get("alert_id") or c["id"]
        base = self.s.api_base_url.rstrip("/")

        def link(action):
            return f"{base}/api/transient/approve?token={sign_token(c['id'], action, self.s.approve_secret)}"

        lines = [f"New {c.get('class_label') or 'transient'} candidate {oid}", ""]
        lines += [s.replace("*", "") for s in self._summary_lines(c)]
        lines += ["", f"Queue:   {link('queue')}", f"Execute: {link('execute')}",
                  f"Reject:  {link('reject')}", "", f"ALeRCE: https://alerce.online/object/{oid}"]
        html = (f"<h3>🔭 {c.get('class_label') or 'transient'} candidate {oid}</h3>"
                + "".join(f"<p>{s}</p>" for s in self._summary_lines(c))
                + f'<p><a href="{link("queue")}">Approve → Queue</a> · '
                f'<a href="{link("execute")}">Approve → Execute</a> · '
                f'<a href="{link("reject")}">Reject</a></p>')
        await self.email.send(f"[CASSA] candidate {oid} ({c.get('class_label') or '?'})",
                              "\n".join(lines), html)

    # ----------------------------------------------------------- slack callback
    async def _on_request(self, client, req):
        from slack_sdk.socket_mode.response import SocketModeResponse
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "interactive":
            return
        try:
            actions = (req.payload or {}).get("actions") or []
            if not actions:
                return
            action_id = actions[0]["action_id"]
            cid = actions[0].get("value")
            user = (req.payload.get("user") or {}).get("id", "?")
            text = await self._decide(cid, action_id, f"slack:{user}")
            await self._update_message(req.payload, text)
        except Exception:
            log.exception("slack interaction handling failed")

    async def _decide(self, cid: str, action_id: str, actor: str) -> str:
        verb, sub = _ACTION_MAP.get(action_id, (None, None))
        if not verb or self.candidates is None:
            return "⚠️ could not process decision"
        if verb == "approve":
            await self.candidates.approve(cid, sub, actor)
            return f"✅ Approved → {sub} by <@{actor.split(':')[-1]}>"
        await self.candidates.reject(cid, actor)
        return f"❌ Rejected by <@{actor.split(':')[-1]}>"

    async def _update_message(self, payload: dict, text: str) -> None:
        if self.web is None:
            return
        try:
            channel = (payload.get("channel") or {}).get("id") or self.s.slack_channel
            ts = (payload.get("message") or {}).get("ts") \
                or (payload.get("container") or {}).get("message_ts")
            if channel and ts:
                await self.web.chat_update(channel=channel, ts=ts, text=text, blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": text}}])
        except Exception:
            log.debug("slack message update failed", exc_info=True)
