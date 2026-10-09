"""Parsers for Windows system facts and the reliability provider (no live Windows calls)."""

from __future__ import annotations

import struct
from typing import Any

from app.config.settings import AgentSettings
from app.errors import HardwareMissingError, PermissionDeniedError
from app.platform.winsys import (
    parse_core_relations,
    parse_event_xml,
    parse_nvme_health,
    parse_socket_tables,
    redact_profile,
    summarize_topology,
)
from app.providers.reliability import ReliabilityProvider
from app.remote_config import RemoteConfig


def _core_record(efficiency: int, mask: int, smt: bool = False) -> bytes:
    body = struct.pack("<BB", int(smt), efficiency) + bytes(20) + struct.pack("<H", 1)
    body += bytes(32 - 8 - len(body))  # pad to GroupMask offset (32)
    body += struct.pack("<QH", mask, 0) + bytes(6)
    return struct.pack("<II", 0, 8 + len(body)) + body


def test_hybrid_topology_splits_performance_and_efficient_cores() -> None:
    buf = _core_record(1, 0b11, smt=True) + _core_record(1, 0b1100, smt=True)
    buf += b"".join(_core_record(0, 1 << i) for i in range(4, 12))
    topo = summarize_topology(parse_core_relations(buf))
    assert topo["hybrid"] is True
    assert (topo["performance_cores"], topo["efficient_cores"], topo["physical_cores"]) == (2, 8, 10)
    assert topo["performance_logical"] == [0, 1, 2, 3]
    assert topo["efficient_logical"] == list(range(4, 12))


def test_uniform_topology_is_not_reported_as_hybrid() -> None:
    topo = summarize_topology(parse_core_relations(_core_record(0, 1) + _core_record(0, 2)))
    assert topo["hybrid"] is False and topo["performance_cores"] is None


def test_socket_tables_count_per_pid_without_addresses() -> None:
    tcp4 = struct.pack("<I", 3) + struct.pack("<6I", 5, 0, 0, 0, 0, 100)
    tcp4 += struct.pack("<6I", 2, 0, 0, 0, 0, 100) + struct.pack("<6I", 5, 0, 0, 0, 0, 200)
    udp4 = struct.pack("<I", 1) + struct.pack("<3I", 0, 0, 100)
    empty = struct.pack("<I", 0)
    out = parse_socket_tables(tcp4, empty, udp4, empty)
    assert out[100] == {"tcp_established": 1, "tcp_listening": 1, "udp": 1}
    assert out[200]["tcp_established"] == 1


def test_nvme_health_log_layout() -> None:
    d = bytearray(512)
    d[0] = 0
    struct.pack_into("<H", d, 1, 327)  # 54 °C
    d[3], d[4], d[5] = 100, 10, 1
    d[128:144] = (697).to_bytes(16, "little")
    d[160:176] = (2).to_bytes(16, "little")
    d[48:64] = (1000).to_bytes(16, "little")
    h = parse_nvme_health(bytes(d))
    assert h["temperature_c"] == 54 and h["percentage_used"] == 1 and h["available_spare_percent"] == 100
    assert h["power_on_hours"] == 697 and h["media_errors"] == 2
    assert h["data_written_bytes"] == 1000 * 512_000


def test_whea_event_xml() -> None:
    xml = (
        '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System>'
        "<EventID>19</EventID><Level>3</Level><TimeCreated SystemTime='2026-10-01T10:00:00.000Z'/>"
        "</System></Event>"
    )
    ev = parse_event_xml(xml)
    assert ev is not None
    assert ev["event_id"] == 19 and ev["description"] == "Corrected machine check" and ev["fatal"] is False


def test_profile_name_is_redacted_from_paths() -> None:
    assert redact_profile(r"C:\Users\alice\AppData\x.exe") == r"C:\Users\<user>\AppData\x.exe"
    assert redact_profile(r"C:\Windows\System32\svchost.exe") == r"C:\Windows\System32\svchost.exe"


def test_remote_config_is_clamped() -> None:
    base = RemoteConfig.from_settings(AgentSettings(AGENT_INGEST_KEY="k"))
    cfg = base.merged(
        {"telemetry_interval_ms": 10, "top_process_count": 999, "collect_process_details": True}
    )
    assert cfg.telemetry_interval_ms == 250 and cfg.top_process_count == 100
    assert cfg.collect_process_details is True
    assert base.merged({"telemetry_interval_ms": "bad"}).telemetry_interval_ms == base.telemetry_interval_ms


def _provider(**overrides: Any) -> ReliabilityProvider:
    health = {"temperature_c": 50, "percentage_used": 3, "available_spare_percent": 100, "power_on_hours": 10}

    def no_whea() -> dict[str, Any]:
        raise PermissionDeniedError("System event log not readable")

    kwargs: dict[str, Any] = {
        "interval_ms": 30000,
        "drives": [0],
        "nvme_health": lambda i: health,
        "whea": no_whea,
        "wmi": None,
    }
    kwargs.update(overrides)
    return ReliabilityProvider(**kwargs)


def test_reliability_provider_reports_measured_and_unavailable_values() -> None:
    readings = {(r.metric, r.labels.get("disk")): r for r in _provider().collect()}
    assert readings[("disk.wear_percent", "PhysicalDrive0")].value == 3
    assert readings[("disk.smart_temperature_c", "PhysicalDrive0")].value == 50
    assert readings[("disk.media_errors", "PhysicalDrive0")].value is None  # not in the log -> unavailable
    whea = readings[("system.whea_errors_30d", None)]
    assert whea.value is None and whea.reason == "System event log not readable"


def test_reliability_provider_non_nvme_drive() -> None:
    def not_nvme(i: int) -> dict[str, Any]:
        raise HardwareMissingError("Drive is not NVMe")

    readings = {r.metric: r for r in _provider(nvme_health=not_nvme).collect()}
    assert readings["disk.wear_percent"].value is None
    assert readings["disk.wear_percent"].reason == "Drive is not NVMe"
