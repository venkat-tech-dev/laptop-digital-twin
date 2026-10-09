"""Security posture readers (read-only; no configuration is changed).

* Microsoft Defender: ``root\\Microsoft\\Windows\\Defender`` ``MSFT_MpComputerStatus``.
* Registered antivirus products: Windows Security Center ``root\\SecurityCenter2`` (client SKUs only).
* Windows Firewall: ``HNetCfg.FwPolicy2`` COM object (effective state, includes Group Policy).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.errors import DriverUnavailableError, TelemetryError, UnsupportedMetricError
from app.platform.wmi import WmiQueryable

FIREWALL_PROFILES = {"domain": 1, "private": 2, "public": 4}


@dataclass(frozen=True, slots=True)
class AvProduct:
    name: str
    enabled: bool
    up_to_date: bool


def decode_product_state(state: int) -> tuple[bool, bool]:
    """Windows Security Center productState -> (enabled, signatures up to date).

    Byte 2 (bits 8-15) is the scanner state: 0x10/0x11 = on; byte 1 (bits 0-7) 0x00 = up to date.
    """
    scanner = (state >> 8) & 0xFF
    signatures = state & 0xFF
    return scanner in (0x10, 0x11), signatures == 0x00


def parse_wmi_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str) or len(value) < 14:
        return None
    try:
        base = datetime.strptime(value[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    tz_minutes = 0
    if len(value) >= 25 and value[21] in "+-" and value[22:25].isdigit():
        tz_minutes = int(value[22:25]) * (1 if value[21] == "+" else -1)
    return (base - timedelta(minutes=tz_minutes)).replace(tzinfo=UTC)


def defender_status(wmi: WmiQueryable) -> dict[str, Any]:
    rows = wmi.query(
        "SELECT AMServiceEnabled, AntivirusEnabled, AntispywareEnabled, RealTimeProtectionEnabled, "
        "AntivirusSignatureAge, AntivirusSignatureLastUpdated, QuickScanEndTime, FullScanEndTime, "
        "IsTamperProtected, AMRunningMode FROM MSFT_MpComputerStatus",
        "root\\Microsoft\\Windows\\Defender",
    )
    if not rows:
        raise UnsupportedMetricError("Microsoft Defender status not available (Defender not installed)")
    r = rows[0]
    return {
        "service_enabled": r.get("AMServiceEnabled"),
        "antivirus_enabled": r.get("AntivirusEnabled"),
        "realtime_enabled": r.get("RealTimeProtectionEnabled"),
        "signature_age_days": r.get("AntivirusSignatureAge"),
        "signature_updated": parse_wmi_datetime(r.get("AntivirusSignatureLastUpdated")),
        "last_quick_scan": parse_wmi_datetime(r.get("QuickScanEndTime")),
        "last_full_scan": parse_wmi_datetime(r.get("FullScanEndTime")),
        "tamper_protected": r.get("IsTamperProtected"),
        "running_mode": r.get("AMRunningMode"),
    }


def antivirus_products(wmi: WmiQueryable) -> list[AvProduct]:
    rows = wmi.query("SELECT displayName, productState FROM AntiVirusProduct", "root\\SecurityCenter2")
    out = []
    for r in rows:
        state = int(r.get("productState") or 0)
        enabled, current = decode_product_state(state)
        out.append(AvProduct(str(r.get("displayName") or "Unknown"), enabled, current))
    return out


def firewall_profiles() -> dict[str, bool]:
    if sys.platform != "win32":
        raise DriverUnavailableError("Requires Windows")
    import win32com.client

    try:
        policy = win32com.client.Dispatch("HNetCfg.FwPolicy2")
        return {name: bool(policy.FirewallEnabled(code)) for name, code in FIREWALL_PROFILES.items()}
    except Exception as exc:  # pywintypes.com_error
        raise TelemetryError(f"Windows Firewall policy not readable: {exc}") from exc


def active_firewall_profiles() -> list[str]:
    if sys.platform != "win32":
        return []
    import win32com.client

    try:
        mask = int(win32com.client.Dispatch("HNetCfg.FwPolicy2").CurrentProfileTypes)
    except Exception:
        return []
    return [name for name, code in FIREWALL_PROFILES.items() if mask & code]
