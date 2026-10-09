"""Windows Update status (read-only: nothing is downloaded or installed).

Uses the Windows Update Agent COM API. The pending-update search runs **offline** against the
results of Windows' own last scan, so the agent generates no traffic to Microsoft; it can still take
10-30 s, which is why it runs on its own worker lane and on a long interval.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.errors import DriverUnavailableError, TelemetryError

_RESULT_SUCCEEDED = 2
_REBOOT_KEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
)


@dataclass(frozen=True, slots=True)
class UpdateHistoryEntry:
    date: datetime
    title: str
    succeeded: bool


def _com() -> Any:
    if sys.platform != "win32":
        raise DriverUnavailableError("Requires Windows")
    import win32com.client

    return win32com.client


def reboot_required() -> bool:
    try:
        flag = bool(_com().Dispatch("Microsoft.Update.SystemInfo").RebootRequired)
    except TelemetryError:
        raise
    except Exception as exc:
        raise TelemetryError(f"Windows Update agent not available: {exc}") from exc
    if flag:
        return True
    import winreg

    for key in _REBOOT_KEYS:
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key))
            return True
        except OSError:
            continue
    return False


def update_history(limit: int = 20) -> tuple[int, list[UpdateHistoryEntry]]:
    try:
        searcher = _com().Dispatch("Microsoft.Update.Session").CreateUpdateSearcher()
        total = int(searcher.GetTotalHistoryCount())
        entries = []
        for h in searcher.QueryHistory(0, min(limit, total)) if total else []:
            date = h.Date
            when = datetime(date.year, date.month, date.day, date.hour, date.minute, date.second, tzinfo=UTC)
            entries.append(
                UpdateHistoryEntry(when, str(h.Title)[:200], int(h.ResultCode) == _RESULT_SUCCEEDED)
            )
    except TelemetryError:
        raise
    except Exception as exc:
        raise TelemetryError(f"Windows Update history not readable: {exc}") from exc
    return total, entries


def pending_updates() -> dict[str, int]:
    """Counts of applicable, not-installed updates from Windows' last scan (offline search)."""
    try:
        searcher = _com().Dispatch("Microsoft.Update.Session").CreateUpdateSearcher()
        searcher.Online = False
        result = searcher.Search("IsInstalled=0 and IsHidden=0")
        updates = result.Updates
        security = 0
        for i in range(updates.Count):
            for cat in updates.Item(i).Categories:
                if "security" in str(cat.Name).lower():
                    security += 1
                    break
        return {"total": int(updates.Count), "security": security}
    except TelemetryError:
        raise
    except Exception as exc:
        raise TelemetryError(f"Windows Update search failed: {exc}") from exc
