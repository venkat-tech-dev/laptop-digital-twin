"""Builds platform clients, hardware inventory and collectors (dependency wiring only)."""

from __future__ import annotations

import gc
from collections.abc import Callable
from typing import Any

import structlog

from app.config.settings import AgentSettings, SensorProvider
from app.discovery.inventory import InventoryCollector, device_id_from
from app.errors import TelemetryError
from app.platform import winsys
from app.platform.dxgi import DxgiAdapter, enumerate_adapters
from app.platform.lhm import LhmClient
from app.platform.ntprocess import NtProcessSnapshotter, RawProcess
from app.platform.powercfg import read_battery_report
from app.platform.wmi import WmiClient
from app.platform.worker import LANE_FAST, LANE_SLOW, WorkerPool
from app.providers.base import TelemetryProvider
from app.providers.battery import BatteryProvider
from app.providers.cpu import CPUProvider
from app.providers.disk import DiskProvider, StorageCapacityProvider
from app.providers.eventlog import EventLogProvider
from app.providers.fan import FanProvider
from app.providers.gpu import GPUProvider
from app.providers.memory import MemoryProvider
from app.providers.network import NetworkProvider
from app.providers.network_health import NetworkHealthProvider
from app.providers.process import ProcessProvider
from app.providers.reliability import ReliabilityProvider
from app.providers.security import SecurityProvider
from app.providers.services import ServicesProvider
from app.providers.system import DisplayProvider, SystemProvider
from app.providers.temperature import TemperatureProvider
from app.providers.updates import UpdatesProvider
from app.remote_config import RemoteConfig

log = structlog.get_logger("agent.runtime")

Bookmarks = tuple[Callable[[str], str | None], Callable[[str, str], None]]


def _security_inventory() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, fn in (("secure_boot_enabled", winsys.secure_boot_enabled), ("tpm", winsys.tpm_info)):
        try:
            out[key] = fn()
        except TelemetryError as exc:
            out[key] = None
            out[f"{key}_reason"] = exc.detail
    return out


class AgentRuntime:
    def __init__(self, settings: AgentSettings, worker: WorkerPool | None = None) -> None:
        self.settings = settings
        self.worker = worker or WorkerPool(hung_after_s=settings.lane_hung_after_s)
        self.wmi: WmiClient | None = None
        self.adapters: list[DxgiAdapter] = []
        self.lhm: LhmClient | None = None
        self.inventory: dict[str, Any] = {}
        self.device_id = ""
        self.config = RemoteConfig.from_settings(settings)
        self.base_providers: list[TelemetryProvider] = []
        self.process_provider: ProcessProvider | None = None
        self.network_health: NetworkHealthProvider | None = None
        self.providers: list[TelemetryProvider] = []

    # ------------------------------------------------------------------ platform / inventory
    def _init_platform(self) -> None:
        try:
            self.wmi = WmiClient()
        except TelemetryError as exc:
            log.error("wmi_unavailable", reason=exc.detail)
        try:
            self.adapters = enumerate_adapters()
        except TelemetryError as exc:
            log.warning("dxgi_unavailable", reason=exc.detail)
        if self.settings.hardware_sensor_provider in (SensorProvider.AUTO, SensorProvider.LHM):
            self.lhm = LhmClient(self.settings.lhm_url, self.wmi)

    def discover(self) -> dict[str, Any]:
        s = self.settings

        def _collect() -> dict[str, Any]:
            if self.wmi is None and not self.adapters:
                self._init_platform()
            collector = InventoryCollector(
                self.wmi,
                lambda: self.adapters,
                read_battery_report,
                include_serials=s.include_serial_numbers,
                include_macs=s.include_mac_addresses,
                include_hostname=s.include_hostname,
                extras={
                    "cpu_topology": winsys.cpu_topology,
                    "security": _security_inventory,
                    "whea": winsys.whea_events,
                },
            )
            return collector.collect()

        inventory = self.worker.submit_sync(_collect, lane=LANE_SLOW)
        self.device_id = device_id_from(inventory)
        inventory.pop("_uuid_hash", None)
        inventory["sensor_provider"] = s.hardware_sensor_provider.value
        if s.agent_id:
            inventory["agent_id"] = s.agent_id
        self.inventory = inventory
        return inventory

    # ------------------------------------------------------------------ collectors
    def build_providers(
        self, bookmarks: Bookmarks, backend_latency: Callable[[], float | None]
    ) -> list[TelemetryProvider]:
        s = self.settings

        def _build() -> list[TelemetryProvider]:
            base = s.telemetry_interval_ms
            use_acpi = s.hardware_sensor_provider in (SensorProvider.AUTO, SensorProvider.ACPI)
            gpu = GPUProvider(base, self.adapters, self.lhm)
            nics = [n["interface"] for n in self.inventory.get("network", []) if n.get("interface")]
            nic_types = {
                n["interface"]: str(n.get("type") or "")
                for n in self.inventory.get("network", [])
                if n.get("interface")
            }
            drives = [
                int(d["disk"].removeprefix("PhysicalDrive"))
                for d in self.inventory.get("storage", [])
                if str(d.get("disk", "")).startswith("PhysicalDrive") and d["disk"][13:].isdigit()
            ]
            try:
                details: Callable[[int, int], Any] | None = winsys.ProcessDetails().get
            except TelemetryError:
                details = None
            self.process_provider = ProcessProvider(
                s.process_interval_ms,
                self.config.top_process_count,
                self._process_snapshotter(),
                gpu_usage_by_pid=lambda: gpu.per_pid_usage,
                sockets_by_pid=winsys.sockets_by_pid,
                details=details,
                details_enabled=lambda: self.config.collect_process_details,
            )
            self.network_health = NetworkHealthProvider(
                s.network_health_interval_ms,
                adapter_types=nic_types,
                probe_host=s.latency_probe_host,
                icmp_count=s.icmp_count,
                icmp_timeout_ms=s.icmp_timeout_ms,
                include_ip=s.include_ip_addresses,
                backend_latency_ms=backend_latency,
            )
            self.base_providers = [
                CPUProvider(s.effective_cpu_interval_ms, self.inventory.get("cpu", {}).get("base_clock_mhz")),
                MemoryProvider(s.interval(s.memory_interval_ms)),
                gpu,
                DiskProvider(s.interval(s.disk_interval_ms)),
                NetworkProvider(s.interval(s.network_interval_ms), nics),
                TemperatureProvider(s.interval(s.temperature_interval_ms), self.lhm, use_acpi=use_acpi),
            ]
            providers: list[TelemetryProvider] = [
                *self.base_providers,
                FanProvider(max(base, 2000), self.lhm, self.wmi),
                BatteryProvider(s.battery_interval_ms, self.wmi),
                StorageCapacityProvider(s.disk_space_interval_ms, self.wmi),
                self.process_provider,
                ReliabilityProvider(
                    s.reliability_interval_ms, drives, winsys.nvme_health, winsys.whea_events, self.wmi
                ),
                self.network_health,
                ServicesProvider(s.services_interval_ms, s.service_allowlist),
                SystemProvider(max(base, 5000)),
                DisplayProvider(max(base, 5000), self.wmi),
            ]
            if s.enable_security_collection:
                providers.append(
                    SecurityProvider(
                        s.security_interval_ms,
                        self.wmi,
                        secure_boot=winsys.secure_boot_enabled,
                        tpm=winsys.tpm_info,
                    )
                )
            if s.enable_update_collection:
                providers.append(UpdatesProvider(s.updates_interval_ms, s.updates_search_interval_s))
            if s.enable_eventlog_collection:
                providers.append(EventLogProvider(s.eventlog_interval_ms, bookmarks))
            return providers

        self.providers = self.worker.submit_sync(_build, lane=LANE_FAST)
        return self.providers

    def apply_config(self, cfg: RemoteConfig) -> list[str]:
        """Apply operator configuration from the backend. Returns the names of changed settings."""
        changed: list[str] = []
        if cfg.telemetry_interval_ms != self.config.telemetry_interval_ms:
            for p in self.base_providers:
                p.interval_ms = cfg.telemetry_interval_ms
            changed.append("telemetry_interval_ms")
        if cfg.process_interval_ms != self.config.process_interval_ms and self.process_provider is not None:
            self.process_provider.interval_ms = cfg.process_interval_ms
            changed.append("process_interval_ms")
        if cfg.top_process_count != self.config.top_process_count and self.process_provider is not None:
            self.process_provider.top_n = cfg.top_process_count
            changed.append("top_process_count")
        if cfg.collect_process_details != self.config.collect_process_details:
            changed.append("collect_process_details")
        self.config = cfg
        return changed

    @staticmethod
    def _process_snapshotter() -> Callable[[], list[RawProcess]]:
        try:
            return NtProcessSnapshotter().snapshot
        except TelemetryError as exc:
            reason = exc.detail

            def unavailable() -> list[RawProcess]:
                raise TelemetryError(reason)

            return unavailable

    def close(self) -> None:
        def _close() -> None:
            for p in self.providers:
                try:
                    p.close()
                except Exception as exc:  # best-effort shutdown; logged, not swallowed silently
                    log.warning("provider_close_failed", provider=p.name, error=str(exc)[:200])
            if self.lhm is not None:
                self.lhm.close()
            self.lhm = None
            if self.wmi is not None:
                self.wmi.close()
            gc.collect()

        try:
            self.worker.submit_sync(_close, lane=LANE_FAST, timeout_s=15.0)
        except Exception as exc:
            log.warning("runtime_close_incomplete", error=str(exc)[:200])
        self.worker.shutdown()
