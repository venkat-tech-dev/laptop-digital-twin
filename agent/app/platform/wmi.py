"""Thin WMI/CIM client over pywin32 COM.

Must be used from a thread that has called ``pythoncom.CoInitializeEx`` (see ``app.platform.worker``).
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Any, Protocol

from app.errors import (
    DriverUnavailableError,
    PermissionDeniedError,
    SensorUnavailableError,
    TelemetryError,
    UnsupportedMetricError,
)

# WBEM HRESULTs (signed 32-bit as pywin32 reports them)
_WBEM_E_ACCESS_DENIED = -2147217405  # 0x80041003
_WBEM_E_INVALID_NAMESPACE = -2147217394  # 0x8004100E
_WBEM_E_INVALID_CLASS = -2147217392  # 0x80041010
_WBEM_E_NOT_SUPPORTED = -2147217396  # 0x8004100C
_WBEM_E_FAILED = -2147217407  # 0x80041001
_E_ACCESSDENIED = -2147024891  # 0x80070005


class WmiQueryable(Protocol):
    def query(self, wql: str, namespace: str = "root\\cimv2") -> list[dict[str, Any]]: ...


def _translate_com_error(exc: Exception, namespace: str, wql: str) -> TelemetryError:
    hresult = _extract_hresult(exc)
    detail = f"{namespace}: {wql}"
    if hresult in (_WBEM_E_ACCESS_DENIED, _E_ACCESSDENIED):
        return PermissionDeniedError(f"WMI access denied for {detail} (requires administrator)")
    if hresult == _WBEM_E_INVALID_NAMESPACE:
        return DriverUnavailableError(f"WMI namespace {namespace} not present")
    if hresult == _WBEM_E_INVALID_CLASS:
        return UnsupportedMetricError(f"WMI class not available: {detail}")
    if hresult == _WBEM_E_NOT_SUPPORTED:
        return UnsupportedMetricError(f"WMI query not supported by firmware/driver: {detail}")
    if hresult == _WBEM_E_FAILED:
        return SensorUnavailableError(f"WMI provider reported generic failure: {detail}")
    return TelemetryError(f"WMI error {hresult}: {detail}")


def _extract_hresult(exc: Exception) -> int | None:
    args: tuple[Any, ...] = tuple(getattr(exc, "args", ()))
    # com_error(hresult, text, excepinfo, argerror); excepinfo[5] holds the WBEM scode
    info = args[2] if len(args) >= 3 else None
    if isinstance(info, tuple) and len(info) >= 6:
        scode = info[5]
        if isinstance(scode, int) and scode != 0:
            return scode
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _to_python(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [_to_python(v) for v in value]
    return str(value)


class WmiClient:
    """Caches one SWbemServices connection per namespace."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise DriverUnavailableError("WMI is only available on Windows")
        import pywintypes  # noqa: F401  (ensures pywin32 is importable early)
        import win32com.client

        self._client = win32com.client
        self._services: dict[str, Any] = {}

    def _service(self, namespace: str) -> Any:
        svc = self._services.get(namespace)
        if svc is None:
            moniker = f"winmgmts:{{impersonationLevel=impersonate}}!\\\\.\\{namespace}"
            try:
                svc = self._client.GetObject(moniker)
            except Exception as exc:  # pywintypes.com_error
                raise _translate_com_error(exc, namespace, "<connect>") from exc
            self._services[namespace] = svc
        return svc

    def query(self, wql: str, namespace: str = "root\\cimv2") -> list[dict[str, Any]]:
        svc = self._service(namespace)
        rows: list[dict[str, Any]] = []
        try:
            for item in svc.ExecQuery(wql):
                rows.append({prop.Name: _to_python(prop.Value) for prop in item.Properties_})
        except Exception as exc:  # errors surface lazily while enumerating
            raise _translate_com_error(exc, namespace, wql) from exc
        return rows

    def close(self) -> None:
        """Release COM connections; call on the thread that created them."""
        self._services.clear()
