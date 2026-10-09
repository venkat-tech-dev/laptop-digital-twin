"""Worker lanes, scheduler isolation, Phase-1 collectors, posture evaluation and log redaction."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog

from app.contracts import Availability, HealthState, MetricSample, Quality
from app.errors import HardwareMissingError, PermissionDeniedError, TelemetryError
from app.health.compliance import PostureFacts, evaluate
from app.health.posture import PostureTracker
from app.observability.logging import configure_logging
from app.platform.eventlog import LogEvent, parse_event, parse_system_time
from app.platform.netinfo import Connectivity, PingResult, Route, parse_forward_table
from app.platform.security import decode_product_state, parse_wmi_datetime
from app.platform.updates import UpdateHistoryEntry
from app.platform.worker import LANE_FAST, LANE_SLOW, WorkerPool
from app.providers.base import MetricSpec, Reading, TelemetryProvider
from app.providers.eventlog import EventLogProvider
from app.providers.network_health import NetworkHealthProvider, adapter_for_gateway
from app.providers.security import SecurityProvider
from app.providers.services import ServicesProvider
from app.providers.updates import UpdatesProvider
from app.scheduler import CollectionScheduler

NO_COM = {"init": lambda: None}


# ------------------------------------------------------------------------------------- worker lanes
async def test_hung_lane_does_not_block_other_lanes() -> None:
    pool = WorkerPool(**NO_COM)
    gate = threading.Event()
    slow = asyncio.ensure_future(pool.run(lambda: gate.wait(5), timeout_s=10, lane=LANE_SLOW))
    started = time.perf_counter()
    assert await pool.run(lambda: 42, timeout_s=2, lane=LANE_FAST) == 42
    assert time.perf_counter() - started < 0.5  # fast lane answered while slow lane was busy
    gate.set()
    await slow
    pool.shutdown()


async def test_hung_lane_is_replaced() -> None:
    pool = WorkerPool(hung_after_s=0.05, **NO_COM)
    block = threading.Event()
    stuck = asyncio.ensure_future(pool.run(lambda: block.wait(10), timeout_s=0.2, lane=LANE_SLOW))
    with pytest.raises(TimeoutError):
        await stuck
    await asyncio.sleep(0.1)
    assert pool.check_hung() == [LANE_SLOW]
    assert await pool.run(lambda: "fresh thread", timeout_s=2, lane=LANE_SLOW) == "fresh thread"
    assert sum(s.replaced for s in pool.stats()) == 1
    block.set()
    pool.shutdown()


class Sleepy(TelemetryProvider):
    name = "sleepy"
    component = "os"
    lane = LANE_SLOW
    timeout_s = 0.1

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [MetricSpec("system.app_crashes_24h", "count", "x")]

    def collect(self) -> list[Reading]:
        time.sleep(0.5)
        return []


class Fast(TelemetryProvider):
    name = "fast"
    component = "memory"

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return []

    def collect(self) -> list[Reading]:
        return [Reading("memory.usage_percent", "memory", 50.0, "percent", "x")]


async def test_slow_collector_times_out_without_delaying_others() -> None:
    got: list[MetricSample] = []
    pool = WorkerPool(**NO_COM)
    sched = CollectionScheduler(
        [Sleepy(200), Fast(50)], pool, lambda s, _: got.extend(s), cpu_budget_percent=50
    )
    stop = asyncio.Event()
    task = asyncio.create_task(sched.run(stop))
    await asyncio.sleep(0.4)
    stop.set()
    await task
    fast = [s for s in got if s.metric == "memory.usage_percent"]
    assert len(fast) >= 4  # kept collecting every 50 ms while the slow lane was stuck
    timed_out = [s for s in got if s.metric == "system.app_crashes_24h"]
    assert (
        timed_out
        and timed_out[0].quality is Quality.ERROR
        and "did not answer" in (timed_out[0].reason or "")
    )
    assert sched.health["sleepy"].consecutive_failures >= 1 and sched.health["fast"].last_success_at
    await asyncio.sleep(0.6)
    pool.shutdown()


# ------------------------------------------------------------------------------------- security
class FakeWmi:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses

    def query(self, wql: str, namespace: str = "root\\cimv2") -> list[dict[str, Any]]:
        value = self.responses.get(namespace)
        if isinstance(value, Exception):
            raise value
        return value or []


DEFENDER = {
    "AMServiceEnabled": True,
    "AntivirusEnabled": True,
    "RealTimeProtectionEnabled": True,
    "AntivirusSignatureAge": 1,
    "AntivirusSignatureLastUpdated": "20261005231232.000000+000",
    "QuickScanEndTime": "20261006082059.769000+000",
    "FullScanEndTime": None,
    "IsTamperProtected": True,
}


def test_product_state_and_wmi_dates() -> None:
    assert decode_product_state(0x61100) == (True, True)  # Defender on, up to date
    assert decode_product_state(0x60110) == (False, False)
    assert parse_wmi_datetime("20261006130533.500000+330") == datetime(
        2026, 10, 6, 7, 35, 33, tzinfo=UTC
    )  # +330 = minutes (IST)
    assert parse_wmi_datetime(None) is None


def test_security_provider_reads_posture_and_detects_changes() -> None:
    wmi = FakeWmi(
        {
            "root\\Microsoft\\Windows\\Defender": [DEFENDER],
            "root\\SecurityCenter2": [{"displayName": "Windows Defender", "productState": 0x61100}],
        }
    )
    fw = {"domain": True, "private": True, "public": True}
    p = SecurityProvider(
        300000,
        wmi,
        firewall=lambda: dict(fw),
        active_profiles=lambda: ["public"],
        secure_boot=lambda: False,
        tpm=lambda: {"present": True, "version": "2.0"},
    )
    r = {(x.metric, x.labels.get("profile") or x.labels.get("product")): x for x in p.collect()}
    assert r[("security.defender_realtime_enabled", None)].value is True
    assert r[("security.firewall_enabled", "public")].value is True
    assert r[("security.antivirus_enabled", "Windows Defender")].value is True
    assert r[("security.defender_last_full_scan", None)].value is None  # never run -> unavailable
    assert p.pop_events() == []
    fw["public"] = False
    p.collect()
    (ev,) = p.pop_events()
    assert ev.type == "security_posture_changed" and ev.severity.value == "warning" and "public" in ev.message


def test_security_provider_on_server_without_security_center() -> None:
    wmi = FakeWmi(
        {
            "root\\Microsoft\\Windows\\Defender": [],
            "root\\SecurityCenter2": TelemetryError("WMI namespace root\\SecurityCenter2 not present"),
        }
    )

    def no_fw() -> dict[str, bool]:
        raise TelemetryError("Windows Firewall policy not readable")

    p = SecurityProvider(300000, wmi, firewall=no_fw, active_profiles=lambda: [])
    r = {x.metric: x for x in p.collect()}
    assert r["security.antivirus_products"].value is None
    assert "Windows Server" in (r["security.antivirus_products"].reason or "")
    assert r["security.defender_realtime_enabled"].value is None
    assert r["security.firewall_enabled"].reason == "Windows Firewall policy not readable"


# ------------------------------------------------------------------------------------- updates / services
def test_updates_provider_runs_slow_search_on_its_own_interval() -> None:
    now = [0.0]
    searches = []
    history = [UpdateHistoryEntry(datetime(2026, 10, 7, tzinfo=UTC), "KB1", True)]

    def pending() -> dict[str, int]:
        searches.append(now[0])
        return {"total": 2, "security": 1}

    p = UpdatesProvider(
        60000,
        3600,
        reboot_required=lambda: True,
        history=lambda n: (1, list(history)),
        pending=pending,
        clock=lambda: now[0],
    )
    r = {x.metric: x.value for x in p.collect()}
    assert r["system.update_reboot_required"] is True and r["system.updates_pending"] == 2
    assert r["system.last_update_title"] == "KB1"
    now[0] = 100.0
    history.append(UpdateHistoryEntry(datetime(2026, 10, 8, tzinfo=UTC), "KB2", True))
    r = {x.metric: x.value for x in p.collect()}
    assert searches == [0.0] and r["system.updates_pending"] == 2  # cached until the search interval
    (ev,) = p.pop_events()
    assert ev.type == "update_installed" and "KB2" in ev.message


def test_services_provider_allowlist_and_state_change_event() -> None:
    state = {"WinDefend": {"status": "running", "start_type": "automatic", "display_name": "Defender"}}
    p = ServicesProvider(60000, ["WinDefend", "NotInstalled"], lookup=lambda n: state.get(n))
    r = {x.labels.get("service"): x for x in p.collect() if x.metric == "system.service_status"}
    assert r["WinDefend"].value == "running"
    assert r["NotInstalled"].value is None and r["NotInstalled"].reason == "Service not installed"
    state["WinDefend"] = {**state["WinDefend"], "status": "stopped"}
    p.collect()
    (ev,) = p.pop_events()
    assert ev.type == "service_state_changed" and ev.severity.value == "warning"


# ------------------------------------------------------------------------------------- event log
CRASH_XML = (
    '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System>'
    "<Provider Name='Application Error'/><EventID>1000</EventID><Level>2</Level>"
    "<TimeCreated SystemTime='2026-10-07T10:00:00.1234567Z'/><EventRecordID>77</EventRecordID></System>"
    "<EventData><Data>app.exe</Data><Data>1.2.3</Data><Data>x</Data><Data>mod.dll</Data><Data>y</Data>"
    "<Data>z</Data><Data>c0000005</Data><Data>C:\\Users\\alice\\app.exe</Data></EventData></Event>"
)


def test_event_xml_and_time_parsing() -> None:
    e = parse_event(CRASH_XML)
    assert e is not None and e.record_id == 77 and e.event_id == 1000 and e.data[0] == "app.exe"
    assert parse_system_time("2026-10-07T10:00:00Z") == datetime(2026, 10, 7, 10, tzinfo=UTC)


def test_eventlog_provider_bookmark_survives_restart_and_strips_paths() -> None:
    state: dict[str, str] = {}
    calls: list[int | None] = []
    crash = parse_event(
        CRASH_XML.replace("2026-10-07T10:00:00", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"))
    )
    assert crash is not None

    def reader(channel: str, xpath: str, after: int | None, since: timedelta) -> list[LogEvent]:
        if channel != "Application":
            raise PermissionDeniedError("denied")
        calls.append(after)
        return [crash] if after is None else []

    p = EventLogProvider(60000, (state.get, state.__setitem__), reader)
    r = {x.metric: x for x in p.collect()}
    assert r["system.app_crashes_24h"].value == 1
    assert "administrator" in (r["system.last_boot_duration_ms"].reason or "")
    (ev,) = p.pop_events()
    assert ev.type == "app_crash" and ev.data["faulting_module"] == "mod.dll" and ev.event_id == "evt-app-77"
    assert "alice" not in json.dumps(ev.model_dump(mode="json"))  # no paths / user names forwarded
    assert state["eventlog.application"] == "77"
    p2 = EventLogProvider(60000, (state.get, state.__setitem__), reader)  # agent restart
    p2.collect()
    assert calls == [None, 77] and p2.pop_events() == []  # resumed from bookmark: no duplicates


# ------------------------------------------------------------------------------------- network health
def test_forward_table_and_adapter_matching() -> None:
    import socket
    import struct

    gw = struct.unpack("<I", socket.inet_aton("192.168.0.1"))[0]
    row = struct.pack("<14I", 0, 0, 0, gw, 6, 4, 3, 0, 0, 50, 0, 0, 0, 0)
    other = struct.pack("<14I", 0, 0, 0, gw, 7, 4, 3, 0, 0, 10, 0, 0, 0, 0)
    routes = parse_forward_table(struct.pack("<I", 2) + row + other)
    assert routes[0].if_index == 7 and routes[0].gateway == "192.168.0.1"  # lowest metric first

    class Addr:
        family, address, netmask = 2, "192.168.0.20", "255.255.255.0"

    assert adapter_for_gateway("192.168.0.1", {"Wi-Fi": [Addr()], "Ethernet": []}) == (
        "Wi-Fi",
        "192.168.0.20",
    )


def _net(conn: list[Connectivity], route: Any = None, ping: Any = None) -> NetworkHealthProvider:
    class Addr:
        family, address, netmask = 2, "10.0.0.5", "255.255.255.0"

    return NetworkHealthProvider(
        30000,
        adapter_types={"Wi-Fi": "wifi"},
        connectivity=lambda: conn[0],
        route=route or (lambda: Route("10.0.0.1", 6, 10)),
        ping=ping or (lambda h, c, t: PingResult(4, 3, 2.5, 1.0, 4.0)),
        addrs=lambda: {"Wi-Fi": [Addr()]},
    )


def test_device_network_and_internet_reported_separately_with_events() -> None:
    conn = [Connectivity(True, True)]
    restored: list[bool] = []
    p = _net(conn)
    p.on_internet_restored = lambda: restored.append(True)
    r = {x.metric: x.value for x in p.collect()}
    assert r["network.device_connected"] is True and r["network.internet_connected"] is True
    assert r["network.active_adapter"] == "Wi-Fi" and r["network.connection_type"] == "wifi"
    assert r["network.gateway_latency_ms"] == 2.5 and r["network.gateway_packet_loss_percent"] == 25.0
    assert "network.ipv4_address" not in r  # IP addresses are opt-in
    conn[0] = Connectivity(True, False)  # Wi-Fi up, internet down
    r = {x.metric: x.value for x in p.collect()}
    assert r["network.device_connected"] is True and r["network.internet_connected"] is False
    conn[0] = Connectivity(True, True)
    p.collect()
    assert [e.type for e in p.pop_events()] == ["internet_lost", "internet_restored"]
    assert restored == [True]  # sync backoff is skipped as soon as the internet is back


def test_no_network_degrades_gracefully() -> None:
    def no_route() -> Route:
        raise HardwareMissingError("No default route (not connected to a network)")

    p = _net([Connectivity(False, False)], route=no_route)
    r = {x.metric: x for x in p.collect()}
    assert r["network.device_connected"].value is False
    assert r["network.gateway_latency_ms"].value is None and "No default route" in (
        r["network.gateway_latency_ms"].reason or ""
    )


# ------------------------------------------------------------------------------------- posture
def _sample(metric: str, value: Any, **labels: str) -> MetricSample:
    return MetricSample(
        metric=metric,
        component="motherboard",
        value=value,
        unit="bool",
        timestamp=datetime.now(UTC),
        source="t",
        quality=Quality.GOOD,
        availability=Availability.AVAILABLE,
        labels=labels,
    )


def test_compliance_states() -> None:
    healthy = evaluate(PostureFacts(True, True, 1, {"domain": True, "public": True}, True, True, False, 0))
    assert healthy.state is HealthState.HEALTHY and healthy.reasons == []
    warn = evaluate(PostureFacts(True, True, 1, {"domain": True, "public": True}, False, True, True, 0))
    assert warn.state is HealthState.WARNING and len(warn.reasons) == 2
    crit = evaluate(PostureFacts(av_enabled=False))
    assert crit.state is HealthState.CRITICAL
    assert crit.checks["firewall"] is HealthState.UNKNOWN  # unreadable facts never count as healthy
    assert evaluate(PostureFacts()).state is HealthState.UNKNOWN


def test_posture_tracker_combines_collectors_and_emits_change_event() -> None:
    t = PostureTracker()
    t.observe(
        [
            _sample("security.defender_realtime_enabled", True),
            _sample("security.firewall_enabled", True, profile="public"),
            _sample("security.secure_boot_enabled", True),
            _sample("system.update_reboot_required", False),
        ]
    )
    health, events = t.evaluate()
    assert health.state is HealthState.HEALTHY and events == []
    t.observe([_sample("security.firewall_enabled", False, profile="public")])
    health, (ev,) = t.evaluate()
    assert health.state is HealthState.CRITICAL and ev.type == "device_health_changed"


# ------------------------------------------------------------------------------------- logging
def test_logs_are_json_with_component_and_redact_secrets(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "agent.log"
    configure_logging("INFO", log_file, console=False)
    structlog.get_logger("agent.test").info(
        "registered", device_token="abc123", agent_ingest_key="k", note="ok"
    )
    for h in logging.getLogger().handlers:
        h.flush()
    line = json.loads(log_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert line["component"] == "agent.test" and line["level"] == "info" and "timestamp" in line
    assert (
        line["device_token"] == "[REDACTED]"
        and line["agent_ingest_key"] == "[REDACTED]"
        and line["note"] == "ok"
    )
    configure_logging("WARNING", None, console=False)


def test_posture_startup_does_not_emit_spurious_change() -> None:
    t = PostureTracker()
    assert t.evaluate()[1] == []  # UNKNOWN before collectors report
    t.observe([_sample("security.secure_boot_enabled", False)])
    health, events = t.evaluate()
    assert health.state is HealthState.WARNING and events == []  # first known state: not a change
