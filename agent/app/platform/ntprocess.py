"""Single-syscall process enumeration via ``NtQuerySystemInformation(SystemProcessInformation)``.

psutil queries per-process attributes (status, thread count) with one whole-system snapshot *per
call*, which costs several seconds for ~400 processes. One ``SystemProcessInformation`` snapshot
returns CPU times, thread states, working set and I/O transfer counters for every process at once,
without opening process handles (so protected processes are not "access denied").

Layout: 64-bit ``SYSTEM_PROCESS_INFORMATION`` / ``SYSTEM_THREAD_INFORMATION`` (winternl / phnt).
"""

from __future__ import annotations

import ctypes
import struct
import sys
from dataclasses import dataclass

from app.errors import DriverUnavailableError

_SYSTEM_PROCESS_INFORMATION = 5
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_PROC_SIZE = 256
_THREAD_SIZE = 80
_THREAD_STATE_WAITING = 5
_WAIT_REASON_SUSPENDED = 5


@dataclass(frozen=True, slots=True)
class RawProcess:
    pid: int
    name: str
    num_threads: int
    cpu_time_100ns: int  # user + kernel
    working_set_bytes: int
    private_bytes: int
    read_transfer_bytes: int
    write_transfer_bytes: int
    suspended: bool
    create_time_100ns: int = 0  # FILETIME (100 ns since 1601-01-01 UTC); 0 for System/Idle
    handle_count: int = 0


def parse_process_buffer(buf: bytes, base_address: int) -> list[RawProcess]:
    out: list[RawProcess] = []
    offset = 0
    while True:
        (next_offset, n_threads) = struct.unpack_from("<II", buf, offset)
        create_time = struct.unpack_from("<q", buf, offset + 32)[0]
        user, kernel = struct.unpack_from("<qq", buf, offset + 40)
        handles = struct.unpack_from("<I", buf, offset + 96)[0]
        name_len = struct.unpack_from("<H", buf, offset + 56)[0]
        name_ptr = struct.unpack_from("<Q", buf, offset + 64)[0]
        pid = struct.unpack_from("<Q", buf, offset + 80)[0]
        working_set = struct.unpack_from("<Q", buf, offset + 144)[0]
        private = struct.unpack_from("<Q", buf, offset + 200)[0]
        read_xfer, write_xfer = struct.unpack_from("<qq", buf, offset + 232)

        name = ""
        if name_ptr and name_len:
            start = name_ptr - base_address
            if 0 <= start < len(buf):
                name = buf[start : start + name_len].decode("utf-16-le", errors="replace")
        if not name:
            name = "System Idle Process" if pid == 0 else f"pid {pid}"

        suspended = n_threads > 0
        thread_base = offset + _PROC_SIZE
        for t in range(n_threads):
            state, reason = struct.unpack_from("<II", buf, thread_base + t * _THREAD_SIZE + 68)
            if not (state == _THREAD_STATE_WAITING and reason == _WAIT_REASON_SUSPENDED):
                suspended = False
                break

        out.append(
            RawProcess(
                pid,
                name,
                n_threads,
                user + kernel,
                working_set,
                private,
                max(0, read_xfer),
                max(0, write_xfer),
                suspended,
                max(0, create_time),
                handles,
            )
        )
        if next_offset == 0:
            return out
        offset += next_offset


class NtProcessSnapshotter:
    def __init__(self) -> None:
        if sys.platform != "win32" or struct.calcsize("P") != 8:
            raise DriverUnavailableError("SystemProcessInformation parser requires 64-bit Windows")
        self._ntdll = ctypes.WinDLL("ntdll")
        self._size = 1 << 20

    def snapshot(self) -> list[RawProcess]:
        query = self._ntdll.NtQuerySystemInformation
        for _ in range(5):
            buf = ctypes.create_string_buffer(self._size)
            needed = ctypes.c_ulong(0)
            status = query(_SYSTEM_PROCESS_INFORMATION, buf, self._size, ctypes.byref(needed)) & 0xFFFFFFFF
            if status == _STATUS_INFO_LENGTH_MISMATCH:
                self._size = max(self._size * 2, needed.value + 65536)
                continue
            if status != 0:
                raise DriverUnavailableError(f"NtQuerySystemInformation failed: 0x{status:08X}")
            return parse_process_buffer(buf.raw, ctypes.addressof(buf))
        raise DriverUnavailableError("Process table kept growing during snapshot")
