"""Windows service host for the endpoint agent (pywin32 ServiceFramework).

Lifecycle: SCM START -> SvcDoRun (INITIALIZE, then COLLECT/CACHE/SYNC/HEALTH loops of AgentRunner)
-> SCM STOP / system SHUTDOWN -> graceful stop (final flush to the local store, short sync attempt).

The service runs as LocalSystem by default, which also unlocks data that needs administrator rights
(boot-performance events, storage reliability counters). Recovery actions (restart on failure) are set
by ``scripts/install-agent-service.ps1``.

Run through the base interpreter (``python.exe -m app.service``): a virtual-environment launcher
spawns a child process, which the Service Control Manager cannot track.
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback

import servicemanager
import win32event
import win32service
import win32serviceutil

from app.config.settings import AgentSettings, RunMode
from app.observability.logging import configure_logging
from app.runner import AgentRunner

SERVICE_NAME = "LaptopDigitalTwinAgent"
DISPLAY_NAME = "Laptop Digital Twin Endpoint Agent"
DESCRIPTION = (
    "Collects device health, performance and security-posture telemetry and synchronises it with the "
    "Laptop Digital Twin backend. Read-only: never changes system configuration."
)
STOP_TIMEOUT_MS = 30_000


AGENT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class AgentService(win32serviceutil.ServiceFramework):  # type: ignore[misc]
    # The SCM starts the *base* interpreter with service_entry.py, which wires up the venv.
    _exe_name_ = getattr(sys, "_base_executable", sys.executable)
    _exe_args_ = f'"{os.path.join(AGENT_ROOT, "service_entry.py")}"'
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = DISPLAY_NAME
    _svc_description_ = DESCRIPTION

    def __init__(self, args: list[str]) -> None:
        super().__init__(args)
        self._stopped = win32event.CreateEvent(None, 0, 0, None)
        self._runner: AgentRunner | None = None

    def GetAcceptedControls(self) -> int:  # noqa: N802 (pywin32 API)
        return int(super().GetAcceptedControls()) | win32service.SERVICE_ACCEPT_SHUTDOWN

    def SvcStop(self) -> None:  # noqa: N802
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=STOP_TIMEOUT_MS)
        if self._runner is not None:
            self._runner.request_stop()

    def SvcShutdown(self) -> None:  # noqa: N802  (Windows is shutting down)
        self.SvcStop()

    def SvcDoRun(self) -> None:  # noqa: N802
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        try:
            settings = AgentSettings()
            runner = AgentRunner(settings, RunMode.SERVICE)
            configure_logging(
                settings.log_level,
                runner.data_dir / "logs" / "agent.log",
                max_mb=settings.log_max_mb,
                backups=settings.log_backups,
                console=False,
            )
            self._runner = runner
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            asyncio.run(runner.run())
        except Exception:
            servicemanager.LogErrorMsg(f"{SERVICE_NAME} crashed:\n{traceback.format_exc()}")
            raise  # non-zero exit -> SCM recovery action restarts the service
        finally:
            win32event.SetEvent(self._stopped)
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STOPPED,
            (self._svc_name_, ""),
        )


def main(argv: list[str]) -> None:
    if len(argv) == 1:
        # Started by the Service Control Manager.
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(AgentService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(AgentService, argv=argv)
