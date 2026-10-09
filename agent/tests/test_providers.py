"""Provider behaviour, especially on machines where hardware or sensors are missing."""

from __future__ import annotations

import pytest

from app.errors import (
    DriverUnavailableError,
    HardwareMissingError,
    PermissionDeniedError,
    SensorUnavailableError,
)
from app.platform.dxgi import DxgiAdapter
from app.platform.ntprocess import RawProcess
from app.platform.powercfg import BatteryReportEntry
from app.providers.battery import BatteryProvider, charging_state
from app.providers.cpu import CPUProvider
from app.providers.disk import DiskProvider, StorageCapacityProvider, pdh_instance_to_disk
from app.providers.fan import FanProvider
from app.providers.gpu import GPUProvider, aggregate_engines
from app.providers.network import NetworkProvider
from app.providers.process import ProcessProvider
from app.providers.temperature import TemperatureProvider

from .fakes import Battery, DiskIo, FakeWmi, NetIo, NicStat, counter_factory, fake_psutil


def by_metric(readings, metric):  # type: ignore[no-untyped-def]
    return [r for r in readings if r.metric == metric]


# ------------------------------------------------------------------ battery
def test_battery_absent_reports_unavailable_and_ac_power() -> None:
    p = BatteryProvider(
        5000, FakeWmi(), psutil=fake_psutil(sensors_battery=lambda: None), report_reader=lambda: []
    )
    out = p.collect()
    charge = by_metric(out, "battery.charge_percent")[0]
    assert charge.value is None and charge.reason == "No battery present"
    assert by_metric(out, "power.source")[0].value == "ac"


def test_battery_health_derived_from_design_and_full_capacity() -> None:
    wmi = FakeWmi(
        {
            "BatteryStatus": [
                {
                    "Voltage": 12000,
                    "ChargeRate": 0,
                    "DischargeRate": 9000,
                    "RemainingCapacity": 30000,
                    "Charging": False,
                    "Discharging": True,
                    "PowerOnline": False,
                }
            ],
            "BatteryFullChargedCapacity": [{"FullChargedCapacity": 40000}],
            "BatteryCycleCount": [{"CycleCount": 200}],
        }
    )
    report = [BatteryReportEntry("b", "M", "LiP", 50000, 40000, 200)]
    p = BatteryProvider(
        5000,
        wmi,
        psutil=fake_psutil(sensors_battery=lambda: Battery(60, 7200, False)),
        report_reader=lambda: report,
    )
    p.collect()
    p.wait_for_report()
    out = p.collect()
    health = by_metric(out, "battery.health_percent")[0]
    assert health.value == 80.0 and health.kind.value == "derived"
    assert by_metric(out, "power.system_power_w")[0].value == 9000  # discharging -> measured system draw
    assert by_metric(out, "battery.time_remaining_s")[0].value == 7200
    assert by_metric(out, "battery.charging_state")[0].value == "discharging"


def test_battery_wmi_permission_denied_still_reports_charge() -> None:
    wmi = FakeWmi({"Battery": PermissionDeniedError("denied")})
    p = BatteryProvider(
        5000,
        wmi,
        psutil=fake_psutil(sensors_battery=lambda: Battery(50, -2, True)),
        report_reader=lambda: (_ for _ in ()).throw(HardwareMissingError()),
    )
    out = p.collect()
    assert by_metric(out, "battery.charge_percent")[0].value == 50.0
    voltage = by_metric(out, "battery.voltage_v")[0]
    assert voltage.value is None and "denied" in (voltage.reason or "")
    assert by_metric(out, "power.system_power_w")[0].value is None


def test_charging_state_mapping() -> None:
    assert charging_state(True, True, False, 50) == "charging"
    assert charging_state(True, False, False, 100) == "full"
    assert charging_state(True, False, False, 80) == "idle_on_ac"
    assert charging_state(False, None, None, 80) == "discharging"


# ---------------------------------------------------------------------- cpu
def test_cpu_frequency_from_nominal_times_performance() -> None:
    p = CPUProvider(1000, 1300, psutil=fake_psutil(), counter_factory=counter_factory({"performance": 250.0}))
    out = p.collect()
    assert by_metric(out, "cpu.frequency_mhz")[0].value == 3250.0
    assert len(by_metric(out, "cpu.core_usage_percent")) == 2


def test_cpu_counters_missing_frequency_unavailable_usage_still_reported() -> None:
    p = CPUProvider(
        1000,
        1300,
        psutil=fake_psutil(),
        counter_factory=counter_factory(construct_error=SensorUnavailableError("no counters")),
    )
    out = p.collect()
    assert by_metric(out, "cpu.usage_percent")[0].value == 15.0
    assert by_metric(out, "cpu.frequency_mhz")[0].value is None


# ---------------------------------------------------------------------- gpu
def test_gpu_not_installed() -> None:
    p = GPUProvider(1000, [], None, counter_factory=counter_factory({}))
    out = p.collect()
    assert out[0].value is None and out[0].reason == "No hardware GPU adapter detected"


def test_gpu_usage_is_busiest_engine_summed_over_processes() -> None:
    luid = "0x00000000_0x0000F10E"
    raw = {
        f"pid_1_luid_{luid}_phys_0_eng_0_engtype_3D": 20.0,
        f"pid_2_luid_{luid}_phys_0_eng_0_engtype_3D": 15.0,
        f"pid_2_luid_{luid}_phys_0_eng_3_engtype_VideoDecode": 50.0,
    }
    engines, per_pid = aggregate_engines(raw)
    assert engines[luid] == {"3D": 35.0, "VideoDecode": 50.0}
    assert per_pid == {1: 20.0, 2: 50.0}
    adapter = DxgiAdapter("Intel UHD", 0x8086, 1, luid, 128, 0, 8 * 2**30, False)
    p = GPUProvider(
        1000,
        [adapter],
        None,
        counter_factory=counter_factory(
            {"engine": raw, "dedicated": {f"luid_{luid}_phys_0": 0.0}, "shared": {f"luid_{luid}_phys_0": 1e9}}
        ),
    )
    out = p.collect()
    assert by_metric(out, "gpu.usage_percent")[0].value == 50.0
    assert by_metric(out, "gpu.shared_memory_used_bytes")[0].value == 1_000_000_000
    assert by_metric(out, "gpu.temperature_c")[0].value is None  # never guessed


# --------------------------------------------------------------- temperature
def test_temperature_acpi_zone_and_unavailable_cpu_package() -> None:
    p = TemperatureProvider(
        1000,
        None,
        counter_factory=counter_factory(
            {
                "temperature": {"\\_TZ.THM0": 3452.0},
                "passive_limit": {"\\_TZ.THM0": 100.0},
                "throttle": {"\\_TZ.THM0": 0.0},
            }
        ),
    )
    out = p.collect()
    zone = by_metric(out, "thermal.zone_temperature_c")[0]
    assert zone.value == 3452.0 and zone.labels == {"zone": "_TZ.THM0"}
    pkg = by_metric(out, "cpu.temperature_c")[0]
    assert pkg.value is None and "administrator" in (pkg.reason or "")


def test_temperature_unavailable_when_firmware_exposes_no_zones() -> None:
    p = TemperatureProvider(
        1000,
        None,
        counter_factory=counter_factory(construct_error=SensorUnavailableError("no thermal zone object")),
    )
    out = p.collect()
    assert by_metric(out, "thermal.zone_temperature_c")[0].value is None


# ----------------------------------------------------------------------- fan
def test_fan_unavailable_reason_and_no_value() -> None:
    out = FanProvider(2000, None, FakeWmi({"Win32_Fan": []})).collect()
    assert out[0].value is None and "tachometer" in (out[0].reason or "")


# ---------------------------------------------------------------------- disk
def test_disk_rates_need_two_samples_then_compute() -> None:
    t = iter([0.0, 1.0])
    io = iter([{"PhysicalDrive0": DiskIo(0, 0, 0, 0)}, {"PhysicalDrive0": DiskIo(2048, 1024, 4, 2)}])
    p = DiskProvider(
        1000,
        psutil=fake_psutil(disk_io_counters=lambda perdisk=True: next(io)),
        counter_factory=counter_factory({}),
        clock=lambda: next(t),
    )
    first = p.collect()
    assert by_metric(first, "disk.read_bytes_per_sec")[0].value is None
    second = p.collect()
    totals = [r for r in by_metric(second, "disk.read_bytes_per_sec") if not r.labels]
    assert totals[0].value == 2048.0


def test_disk_unavailable_when_no_disk_counters() -> None:
    p = DiskProvider(
        1000,
        psutil=fake_psutil(disk_io_counters=lambda perdisk=True: {}),
        counter_factory=counter_factory(error=SensorUnavailableError("x")),
    )
    out = p.collect()
    assert all(r.value is None for r in out)


def test_pdh_instance_mapping() -> None:
    assert pdh_instance_to_disk("0 C:") == "PhysicalDrive0"
    assert pdh_instance_to_disk("_Total") is None


def test_storage_health_zero_means_healthy() -> None:
    wmi = FakeWmi({"MSFT_PhysicalDisk": [{"DeviceId": "0", "FriendlyName": "SSD", "HealthStatus": 0}]})
    out = StorageCapacityProvider(30000, wmi, psutil=fake_psutil()).collect()
    assert by_metric(out, "disk.health_status")[0].value == "Healthy"


# ------------------------------------------------------------------- network
def test_network_adapter_disconnected_and_rates() -> None:
    t = iter([0.0, 2.0])
    io = iter([{"Wi-Fi": NetIo(0, 0, 0, 0, 0, 0)}, {"Wi-Fi": NetIo(4000, 2000, 10, 8, 0, 0)}])
    ps = fake_psutil(
        net_io_counters=lambda pernic=True: next(io), net_if_stats=lambda: {"Wi-Fi": NicStat(True, 300)}
    )
    p = NetworkProvider(1000, ["Wi-Fi", "Ethernet"], psutil=ps, clock=lambda: next(t))
    p.collect()
    out = p.collect()
    rx = next(r for r in by_metric(out, "network.rx_bytes_per_sec") if not r.labels)
    assert rx.value == 2000.0
    ps2 = fake_psutil(
        net_io_counters=lambda pernic=True: {"Ethernet": NetIo(0, 0, 0, 0, 0, 0)}, net_if_stats=lambda: {}
    )
    out2 = NetworkProvider(1000, ["Ethernet"], psutil=ps2).collect()
    link = by_metric(out2, "network.link_up")[0]
    assert link.value is None and "disconnected" in (link.reason or "").lower()


# ------------------------------------------------------------------ process
def test_process_cpu_from_cpu_time_delta() -> None:
    snaps = iter(
        [
            [
                RawProcess(0, "Idle", 12, 0, 0, 0, 0, 0, False),
                RawProcess(42, "app.exe", 4, 0, 100, 50, 0, 0, False),
            ],
            [
                RawProcess(0, "Idle", 12, 0, 0, 0, 0, 0, False),
                RawProcess(42, "app.exe", 4, 2 * 10**7, 100, 50, 1000, 0, False),
            ],
        ]
    )
    t = iter([0.0, 1.0])
    p = ProcessProvider(
        3000, 10, lambda: next(snaps), total_memory_bytes=1000, cpu_count=4, clock=lambda: next(t)
    )
    p.collect()
    p.pop_snapshot()
    p.collect()
    snap = p.pop_snapshot()
    assert snap is not None and snap.total_processes == 1  # idle excluded
    proc = snap.processes[0]
    assert proc.cpu_percent == 50.0  # 2 CPU-seconds in 1 s over 4 logical CPUs
    assert proc.io_read_bytes_per_sec == 1000.0


def test_process_snapshot_failure_is_unavailable() -> None:
    def boom() -> list[RawProcess]:
        raise DriverUnavailableError("no ntdll")

    out = ProcessProvider(3000, 10, boom, total_memory_bytes=1, cpu_count=1).collect()
    assert out[0].value is None


def test_battery_report_timeout_is_a_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung powercfg must surface as DriverUnavailableError (handled), never crash the agent."""
    import subprocess

    from app.platform import powercfg

    def hang(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="powercfg", timeout=1)

    monkeypatch.setattr(powercfg.subprocess, "run", hang)
    with pytest.raises(DriverUnavailableError, match="timed out"):
        powercfg.read_battery_report(timeout_s=1)
