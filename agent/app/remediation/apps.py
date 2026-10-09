"""Restart of an approved desktop application in the user's own session.

Graceful only: windows of the application receive WM_CLOSE (the same as clicking the close button);
the agent never terminates or force-kills a process. If the application does not exit within the timeout,
the action fails and nothing else is done. The program is started again from the executable path the
running process itself reported (never a path from the network), without arguments.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Protocol

import psutil


@dataclass
class AppProcess:
    pid: int
    exe_path: str
    session_id: int | None


class AppController(Protocol):
    def find(self, executables: tuple[str, ...]) -> list[AppProcess]: ...

    def close_gracefully(self, pids: list[int], timeout_s: float) -> bool: ...

    def launch(self, exe_path: str) -> bool: ...


def _session_of(pid: int) -> int | None:
    if sys.platform != "win32":
        return None
    try:
        import win32ts

        return int(win32ts.ProcessIdToSessionId(pid))  # type: ignore[no-untyped-call]
    except Exception:
        return None


class WindowsAppController:
    """psutil + pywin32 implementation, limited to processes in the agent's own Windows session."""

    def __init__(self) -> None:
        self.own_session = _session_of(os.getpid())

    def find(self, executables: tuple[str, ...]) -> list[AppProcess]:
        out = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if str(p.info["name"] or "").lower() not in executables:
                    continue
                session = _session_of(p.pid)
                if self.own_session is not None and session != self.own_session:
                    continue  # another user's session: never touched
                out.append(AppProcess(p.pid, p.exe(), session))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return out

    def close_gracefully(self, pids: list[int], timeout_s: float) -> bool:
        if sys.platform != "win32":
            return False
        import win32con
        import win32gui
        import win32process

        targets = set(pids)

        def ask(hwnd: int, _: object) -> bool:
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid in targets and win32gui.IsWindowVisible(hwnd):
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:  # noqa: S110 - a window that vanished is fine
                pass
            return True

        win32gui.EnumWindows(ask, None)
        procs = []
        for pid in pids:
            try:
                procs.append(psutil.Process(pid))
            except psutil.NoSuchProcess:
                continue
        _, alive = psutil.wait_procs(procs, timeout=timeout_s)
        return not alive

    def launch(self, exe_path: str) -> bool:
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        subprocess.Popen(
            [exe_path],
            cwd=os.path.dirname(exe_path),
            close_fds=True,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True


def wait_running(
    ctrl: AppController, executables: tuple[str, ...], old_pids: set[int], timeout_s: float
) -> list[int]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        fresh = [p.pid for p in ctrl.find(executables) if p.pid not in old_pids]
        if fresh:
            return fresh
        time.sleep(0.5)
    return []
