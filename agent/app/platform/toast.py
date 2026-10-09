"""Windows toast notifications for alerts routed to the "windows" channel (Phase 6).

Shown through the built-in WinRT toast API from Windows PowerShell 5.1 (no extra modules). The text
comes from the backend, so it is XML-escaped and the script is passed base64-encoded
(``-EncodedCommand``): nothing from the message can become a command. Nothing else is executed.
A toast needs an interactive desktop session: under a Windows service (session 0) it is skipped.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from xml.sax.saxutils import escape

# AUMID of Windows PowerShell (registered on every Windows 10/11): toasts appear as "Windows PowerShell"
APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('__XML__')
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('__APPID__').Show($toast)
"""  # noqa: E501


def toast_xml(title: str, body: str) -> str:
    t = escape(title[:120])
    b = escape(body[:400])
    return (
        '<toast><visual><binding template="ToastGeneric">'
        f"<text>{t}</text><text>{b}</text><text>Laptop Digital Twin</text>"
        "</binding></visual></toast>"
    )


def build_command(title: str, body: str) -> list[str]:
    xml = toast_xml(title, body).replace("'", "''")  # PowerShell single-quoted string
    script = _SCRIPT.replace("__XML__", xml).replace("__APPID__", APP_ID)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]


def interactive_session() -> bool:
    return sys.platform == "win32" and os.environ.get("SESSIONNAME", "").lower() not in ("", "services")


def show_toast(title: str, body: str, timeout_s: float = 15.0) -> bool:
    if not interactive_session():
        return False
    try:
        r = subprocess.run(
            build_command(title, body),
            capture_output=True,
            timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0
