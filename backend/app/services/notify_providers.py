"""Notification providers (one per channel) behind ``NotificationProvider``.

    in_app    stored in the inbox; pushed to the user's open sessions (notification.created)
    browser   pushed to the user's open sessions as a browser notification request; the page shows it
              only if the user granted permission. No open session: retried for up to 1 h, then EXPIRED
              (the in-app copy is never lost)
    windows   queued for the device's agent, which pulls it with its device token and shows a Windows
              toast; DELIVERED when the agent acknowledges (expires after 1 h)
    email     SMTP (host/port/user/from from settings; the password only from the environment)
    webhook   HTTPS POST of a sanitised JSON payload, HMAC-SHA256 signed (secret only from the
              environment), idempotency key header, 5 s timeout, private networks blocked unless allowed

Providers never raise for delivery problems: they return a classified ``DeliveryResult`` and keep
credentials out of payloads, results and logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import smtplib
import socket
import ssl
import time
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlparse

import httpx

from app.domain.alerting.delivery import DeliveryResult, NotificationProvider
from app.domain.alerting.models import Notification

EPHEMERAL_MAX_AGE_S = 3600.0


class InAppProvider(NotificationProvider):
    channel = "in_app"
    name = "in_app"

    def __init__(self, push: Callable[[Notification], None]) -> None:
        self._push = push

    async def send(self, notification: Notification) -> DeliveryResult:
        self._push(notification)  # the inbox row is the delivery; the push is best effort
        return DeliveryResult(True, provider_message_id=notification.notification_id)


class BrowserProvider(NotificationProvider):
    channel = "browser"
    name = "browser_push_via_websocket"

    def __init__(self, push: Callable[[Notification], int]) -> None:
        self._push = push  # returns the number of open sessions of the recipient

    async def send(self, notification: Notification) -> DeliveryResult:
        if self._push(notification) > 0:
            return DeliveryResult(True, provider_message_id=notification.notification_id)
        return DeliveryResult(False, "deferred", "recipient has no open session", retry_after_s=60.0)


class WindowsToastProvider(NotificationProvider):
    channel = "windows"
    name = "agent_toast"

    async def send(self, notification: Notification) -> DeliveryResult:
        if not notification.device_id:
            return DeliveryResult(False, "permanent", "no device to show a Windows notification on")
        return DeliveryResult(True, pending_pickup=True)


class EmailProvider(NotificationProvider):
    channel = "email"
    name = "smtp"

    def __init__(
        self,
        host: str | None,
        port: int,
        username: str | None,
        password: str | None,
        sender: str | None,
        starttls: bool,
        timeout_s: float = 10.0,
    ) -> None:
        self._host, self._port, self._user, self._password = host, port, username, password
        self._sender, self._starttls, self._timeout = sender, starttls, timeout_s

    def available(self) -> tuple[bool, str | None]:
        if not self._host or not self._sender:
            return False, "SMTP is not configured (SMTP_HOST / SMTP_FROM)"
        return True, None

    async def send(self, notification: Notification) -> DeliveryResult:
        ok, why = self.available()
        if not ok:
            return DeliveryResult(False, "permanent", why)
        to = (notification.payload or {}).get("email")
        if not to:
            return DeliveryResult(False, "permanent", "no e-mail address in the user's preferences")
        msg = EmailMessage()
        msg["Subject"] = f"[{notification.severity}] {notification.title}"[:200]
        msg["From"] = str(self._sender)
        msg["To"] = str(to)
        msg["Message-ID"] = f"<{notification.idempotency_key}@ldt>"  # idempotent for the receiving side
        msg.set_content(f"{notification.body}\n\n-- Laptop Digital Twin (automated notification)")
        try:
            await asyncio.wait_for(asyncio.to_thread(self._deliver, msg), timeout=self._timeout + 2)
        except smtplib.SMTPRecipientsRefused:
            return DeliveryResult(False, "permanent", "recipient refused by the mail server")
        except smtplib.SMTPAuthenticationError:
            return DeliveryResult(False, "permanent", "SMTP authentication failed")
        except smtplib.SMTPResponseException as exc:
            kind = "transient" if 400 <= exc.smtp_code < 500 else "permanent"
            return DeliveryResult(False, kind, f"SMTP {exc.smtp_code}")
        except (TimeoutError, OSError, smtplib.SMTPException) as exc:
            return DeliveryResult(False, "transient", type(exc).__name__)
        return DeliveryResult(True, provider_message_id=msg["Message-ID"])

    def _deliver(self, msg: EmailMessage) -> None:
        assert self._host is not None
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
            if self._starttls:
                smtp.starttls(context=ssl.create_default_context())
            if self._user and self._password:
                smtp.login(self._user, self._password)
            smtp.send_message(msg)


def check_webhook_url(url: str, allow_http: bool, allow_private: bool) -> str | None:
    """Reason the URL is not allowed (None = allowed). Blocks SSRF to internal networks."""
    try:
        u = urlparse(url)
    except ValueError:
        return "invalid URL"
    if u.scheme not in ("https", "http") or not u.hostname:
        return "URL must be https://host/..."
    if u.scheme == "http" and not allow_http:
        return "plain http is not allowed (WEBHOOK_ALLOW_HTTP)"
    if u.username or u.password:
        return "credentials in the URL are not allowed (use the signing secret)"
    if allow_private:
        return None
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except OSError:
        return "host does not resolve"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return "private / loopback / link-local addresses are not allowed (WEBHOOK_ALLOW_PRIVATE)"
    return None


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


class WebhookProvider(NotificationProvider):
    channel = "webhook"
    name = "webhook"

    def __init__(
        self,
        targets: Callable[[], dict[str, str]],
        secret: str | None,
        allow_http: bool,
        allow_private: bool,
        timeout_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._targets = targets  # target name -> URL (configured by an administrator)
        self._secret = secret
        self._allow_http, self._allow_private = allow_http, allow_private
        self._timeout = timeout_s
        self._transport = transport

    def available(self) -> tuple[bool, str | None]:
        if not self._secret:
            return False, "WEBHOOK_SIGNING_SECRET is not set (webhooks are always signed)"
        return True, None

    async def send(self, notification: Notification) -> DeliveryResult:
        ok, why = self.available()
        if not ok:
            return DeliveryResult(False, "permanent", why)
        target = notification.user_id.removeprefix("webhook:")
        url = self._targets().get(target)
        if not url:
            return DeliveryResult(False, "permanent", f"webhook target {target!r} no longer configured")
        bad = await asyncio.to_thread(check_webhook_url, url, self._allow_http, self._allow_private)
        if bad:
            return DeliveryResult(False, "permanent", bad)
        body = json.dumps(webhook_payload(notification), separators=(",", ":"), default=str).encode()
        ts = str(int(time.time()))
        assert self._secret is not None
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "laptop-digital-twin-webhook/1",
            "X-LDT-Timestamp": ts,
            "X-LDT-Signature": sign(self._secret, ts, body),
            "Idempotency-Key": notification.idempotency_key,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport, follow_redirects=False
            ) as client:
                resp = await client.post(url, content=body, headers=headers)
        except httpx.TimeoutException:
            return DeliveryResult(False, "transient", "timeout")
        except httpx.HTTPError as exc:
            return DeliveryResult(False, "transient", type(exc).__name__)
        if 200 <= resp.status_code < 300:
            return DeliveryResult(True, provider_message_id=resp.headers.get("X-Request-Id"))
        if resp.status_code == 429:
            try:
                ra = float(resp.headers.get("Retry-After", "30"))
            except ValueError:
                ra = 30.0
            return DeliveryResult(False, "rate_limited", "HTTP 429", retry_after_s=min(ra, 3600.0))
        kind = "transient" if resp.status_code in (408, 425) or resp.status_code >= 500 else "permanent"
        return DeliveryResult(False, kind, f"HTTP {resp.status_code}")


def webhook_payload(n: Notification) -> dict[str, Any]:
    """Sanitised, documented payload: identifiers, severity, wording and the alert's facts only."""
    p = n.payload or {}
    return {
        "type": "ldt.alert.notification",
        "version": 1,
        "notification_id": n.notification_id,
        "alert_id": n.alert_id,
        "device_id": n.device_id,
        "severity": n.severity,
        "category": n.category,
        "title": n.title,
        "body": n.body,
        "alert": {
            k: p.get(k)
            for k in (
                "alert_type",
                "status",
                "metric",
                "observed",
                "expected",
                "threshold",
                "confidence",
                "first_detected_at",
                "crossing_at",
            )
            if k in p
        },
        "created_at": n.created_at.isoformat(),
    }
