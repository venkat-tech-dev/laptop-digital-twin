"""Enumerate GPUs through DXGI (ctypes, no extra dependencies).

DXGI gives the adapter LUID (needed to map ``GPU Engine`` performance-counter instances to a named
adapter) and the real dedicated/shared memory sizes, which ``Win32_VideoController.AdapterRAM``
truncates to 4 GiB.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

from app.errors import DriverUnavailableError


@dataclass(frozen=True, slots=True)
class DxgiAdapter:
    name: str
    vendor_id: int
    device_id: int
    luid: str  # formatted like PDH: "0x00000000_0x0000D1B5"
    dedicated_video_memory: int
    dedicated_system_memory: int
    shared_system_memory: int
    is_software: bool


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):  # noqa: N801  (Win32 struct name)
    _fields_ = [
        ("Description", wintypes.WCHAR * 128),
        ("VendorId", wintypes.UINT),
        ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT),
        ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", _LUID),
        ("Flags", wintypes.UINT),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


# IID_IDXGIFactory1 = 770aae78-f26f-4dba-a829-253c83d1b387
_IID_IDXGIFactory1 = _GUID(
    0x770AAE78, 0xF26F, 0x4DBA, (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87)
)
_DXGI_ERROR_NOT_FOUND = 0x887A0002
_DXGI_ADAPTER_FLAG_SOFTWARE = 2
_VT_RELEASE = 2
_VT_ENUM_ADAPTERS1 = 12
_VT_GET_DESC1 = 10


def format_luid(high: int, low: int) -> str:
    return f"0x{high & 0xFFFFFFFF:08X}_0x{low & 0xFFFFFFFF:08X}"


def _vcall(obj: ctypes.c_void_p, index: int, restype: type, *argtypes: type) -> Any:
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtable[index])


def enumerate_adapters() -> list[DxgiAdapter]:
    if sys.platform != "win32":
        raise DriverUnavailableError("DXGI is only available on Windows")
    try:
        dxgi = ctypes.WinDLL("dxgi.dll")
    except OSError as exc:
        raise DriverUnavailableError(f"dxgi.dll not loadable: {exc}") from exc

    factory = ctypes.c_void_p()
    hr = dxgi.CreateDXGIFactory1(ctypes.byref(_IID_IDXGIFactory1), ctypes.byref(factory))
    if hr != 0:
        raise DriverUnavailableError(f"CreateDXGIFactory1 failed: 0x{hr & 0xFFFFFFFF:08X}")

    adapters: list[DxgiAdapter] = []
    try:
        enum1 = _vcall(factory, _VT_ENUM_ADAPTERS1, ctypes.c_long, wintypes.UINT, ctypes.c_void_p)
        index = 0
        while True:
            adapter = ctypes.c_void_p()
            hr = enum1(factory, index, ctypes.byref(adapter))
            if (hr & 0xFFFFFFFF) == _DXGI_ERROR_NOT_FOUND:
                break
            if hr != 0:
                raise DriverUnavailableError(f"EnumAdapters1 failed: 0x{hr & 0xFFFFFFFF:08X}")
            try:
                desc = _DXGI_ADAPTER_DESC1()
                get_desc = _vcall(adapter, _VT_GET_DESC1, ctypes.c_long, ctypes.c_void_p)
                if get_desc(adapter, ctypes.byref(desc)) == 0:
                    adapters.append(
                        DxgiAdapter(
                            name=desc.Description.strip(),
                            vendor_id=desc.VendorId,
                            device_id=desc.DeviceId,
                            luid=format_luid(desc.AdapterLuid.HighPart, desc.AdapterLuid.LowPart),
                            dedicated_video_memory=int(desc.DedicatedVideoMemory),
                            dedicated_system_memory=int(desc.DedicatedSystemMemory),
                            shared_system_memory=int(desc.SharedSystemMemory),
                            is_software=bool(desc.Flags & _DXGI_ADAPTER_FLAG_SOFTWARE),
                        )
                    )
            finally:
                _vcall(adapter, _VT_RELEASE, wintypes.ULONG)(adapter)
            index += 1
    finally:
        _vcall(factory, _VT_RELEASE, wintypes.ULONG)(factory)
    return adapters
