# Telemetry, data classification and sensor compatibility

## Sample format

Every value the agent sends is a sample shaped like this:

```json
{
  "metric": "thermal.zone_temperature_c",
  "component": "thermal",
  "value": 74.05,
  "unit": "celsius",
  "timestamp": "2026-10-06T11:58:25.312Z",
  "source": "Windows Performance Counter (ACPI Thermal Zone)",
  "quality": "GOOD",
  "availability": "available",
  "kind": "measured",
  "reason": null,
  "labels": {"zone": "_TZ.THM0"}
}
```

When a metric cannot be read, the sample has `"value": null`, an `availability` of
`unavailable`, a `quality` of `UNAVAILABLE` or `ERROR`, and a `reason`. Null is never replaced by
a guess.

**Quality states.**

- `GOOD`: fresh, valid reading.
- `DEGRADED`: older than 3 s.
- `STALE`: older than 10 s. Readings are aged at read time on the backend, so old data is
  never reported as `GOOD`.
- `UNAVAILABLE`: the sensor is not exposed.
- `ERROR`: the read failed, or the value was malformed or implausible. NaN or out-of-range
  values are rejected, never clamped.

## Data classification

| Class | Meaning | How it is marked |
|---|---|---|
| **REAL** | Read directly from an OS or hardware interface | `kind: measured` (or `static` for capacities); UI tag **REAL** |
| **DERIVED** | Computed only from real values (rates, ratios, products) | `kind: derived`; UI tag **DERIVED** |
| **UNAVAILABLE** | Not exposed on this machine or in this privilege context | `value: null` + `reason`; UI shows **Unavailable** + reason |
| **PREDICTED** | Trend extrapolation from history | `/api/v1/analytics/predictions`, `kind: PREDICTED`, always with confidence + method |
| **SIMULATED** | What-if model output | Only from `/api/v1/simulation/run` and the SIMULATION tab, labelled `SIMULATION — GENERATED DATA` |

## Metric catalogue

The last column is the status on the reference machine: LENOVO ThinkPad L14 Gen 4, Windows 11
Pro, non-administrator user, LibreHardwareMonitor not running.

| Metric | Unit | Source | Class | Reference machine |
|---|---|---|---|---|
| `cpu.usage_percent`, `cpu.core_usage_percent{core}` | % | psutil / `GetSystemTimes` | REAL | ✅ |
| `cpu.performance_percent` | % of nominal | PDH `Processor Information\% Processor Performance` | REAL | ✅ (exceeds 100 under turbo) |
| `cpu.nominal_frequency_mhz` | MHz | WMI `Win32_Processor.MaxClockSpeed` | REAL (static) | ✅ 1300 |
| `cpu.frequency_mhz` | MHz | nominal × performance % (Task Manager method) | DERIVED | ✅ |
| `cpu.temperature_c`, `cpu.package_power_w` | °C, W | LibreHardwareMonitor | REAL when LHM runs | ⛔ Unavailable: needs a kernel driver (LHM as admin) |
| `thermal.zone_temperature_c{zone}` | °C | PDH `Thermal Zone Information\High Precision Temperature` | REAL | ✅ `_TZ.THM0` |
| `thermal.passive_limit_percent{zone}`, `thermal.throttle_reasons{zone}` | %, bitmask | PDH thermal zone | REAL | ✅ |
| `memory.*` (total, used, available, usage, page file) | bytes, % | psutil / `GlobalMemoryStatusEx` | REAL / DERIVED (used) | ✅ |
| `gpu.usage_percent{adapter,luid}` | % | PDH `GPU Engine` (busiest engine, summed over processes) | REAL | ✅ |
| `gpu.engine_usage_percent{engine}` | % | PDH `GPU Engine` | REAL | ✅ |
| `gpu.dedicated/shared_memory_used_bytes` | bytes | PDH `GPU Adapter Memory` | REAL | ✅ |
| `gpu.dedicated/shared_memory_total_bytes` | bytes | DXGI `IDXGIAdapter1::GetDesc1` | REAL (static) | ✅ |
| `gpu.temperature_c`, `gpu.core_clock_mhz`, `gpu.power_w` | °C, MHz, W | LibreHardwareMonitor | REAL when LHM runs | ⛔ Unavailable (Intel iGPU exposes none without LHM) |
| `disk.read/write_bytes_per_sec`, `disk.read/write_ops_per_sec` | B/s, ops/s | psutil `IOCTL_DISK_PERFORMANCE` deltas | DERIVED | ✅ |
| `disk.active_time_percent` | % | 100 − PDH `% Idle Time` | DERIVED | ✅ |
| `disk.avg_read/write_latency_ms`, `disk.queue_length` | ms, count | PDH `PhysicalDisk` | REAL | ✅ |
| `disk.usage_percent/free/used/total{volume}` | %, bytes | `GetDiskFreeSpaceEx` | REAL | ✅ |
| `disk.health_status{disk}` | state | `MSFT_PhysicalDisk.HealthStatus` | REAL | ✅ Healthy |
| `disk.temperature_c` | °C | LibreHardwareMonitor (NVMe SMART) | REAL when LHM runs | ⛔ Unavailable (SMART reliability counters need admin) |
| `network.rx/tx_bytes_per_sec`, packets, errors, drops | /s | psutil `GetIfTable2` deltas | DERIVED | ✅ |
| `network.link_up{nic}`, `network.link_speed_mbps{nic}` | bool, Mbps | psutil | REAL | ✅ (Ethernet: link down → speed Unavailable) |
| `battery.charge_percent`, `battery.power_plugged`, `battery.time_remaining_s` | | `GetSystemPowerStatus` | REAL | ✅ (time remaining Unavailable on AC) |
| `battery.voltage_v`, `charge_rate_w`, `discharge_rate_w`, `remaining_capacity_wh` | V, W, Wh | `root\wmi BatteryStatus` (ACPI battery driver) | REAL | ✅ |
| `battery.full_charge_capacity_wh`, `battery.cycle_count` | Wh, count | `root\wmi BatteryFullChargedCapacity` / `BatteryCycleCount` | REAL | ✅ 44.45 Wh, 168 cycles |
| `battery.design_capacity_wh` | Wh | `powercfg /batteryreport` (no admin needed) | REAL (static) | ✅ 46.5 Wh |
| `battery.health_percent` | % | full-charge ÷ design capacity | DERIVED | ✅ 95.6 % |
| `battery.charging_state` | state | Charging/Discharging flags + plugged + charge | DERIVED | ✅ |
| `power.source` | state | `GetSystemPowerStatus` | REAL | ✅ |
| `power.system_power_w` | W | battery discharge rate while on battery | REAL (on battery only) | ⛔ Unavailable on AC: no AC-side meter is exposed |
| `fan.speed_rpm{fan}` | rpm | LibreHardwareMonitor / `Win32_Fan` | REAL when exposed | ⛔ Unavailable: `Win32_Fan` empty, embedded controller needs LHM |
| `display.brightness_percent` | % | `root\wmi WmiMonitorBrightness` | REAL | ✅ |
| `system.uptime_s`, `system.process_count`, `system.thread_count` | | psutil / `NtQuerySystemInformation` | REAL / DERIVED | ✅ |
| `agent.*` | | agent self-monitoring | REAL | ✅ |

Processes are sent as a separate snapshot: the top N by CPU, memory, GPU and disk I/O, via
`NtQuerySystemInformation`. Per-process GPU % comes from the `GPU Engine` counter's per-PID
instances. Per-process network throughput is **Unavailable** because it needs ETW kernel tracing
(admin). Command lines, user names and window titles are never read.

## Collection intervals

| Provider | Default interval | Variable |
|---|---|---|
| CPU | 1000 ms (500 possible) | `CPU_INTERVAL_MS` / `TELEMETRY_INTERVAL_MS` |
| Memory, GPU, disk I/O, network, temperatures | 1000 ms | `TELEMETRY_INTERVAL_MS` |
| Fan | max(1000, 2000) ms | — |
| Battery | 5000 ms | `BATTERY_INTERVAL_MS` |
| Processes | 3000 ms | `PROCESS_INTERVAL_MS` |
| Volume capacity + disk health | 30 s | `DISK_SPACE_INTERVAL_MS` |
| Uptime, display | 5 s | — |
| Hardware inventory and battery report | startup + hourly | `STATIC_REFRESH_INTERVAL_S` |

Measured footprint on the reference machine: about **0.2–0.3 % of total CPU** (12 logical
processors) and about **55 MB RSS**. The agent reports this itself as `agent.cpu_percent` and
`agent.memory_rss_bytes`, plus per-provider `agent.collect_duration_ms`. It stretches its intervals
if it exceeds `AGENT_CPU_BUDGET_PERCENT`.

## Sensor compatibility

| Capability | Without admin | With LibreHardwareMonitor (admin) |
|---|---|---|
| CPU load, per-core, frequency | ✅ | ✅ |
| CPU package temperature / power | ❌ (ACPI zone shown instead, labelled) | ✅ Intel/AMD |
| GPU load / memory | ✅ (all WDDM 2.x GPUs via PDH + DXGI) | ✅ |
| GPU temperature / clock / power | ❌ | ✅ NVIDIA / AMD / most Intel |
| Fan RPM | ❌ unless `Win32_Fan` is implemented by firmware (rare) | ✅ where the EC is supported |
| NVMe temperature | ❌ | ✅ |
| Battery wear / cycles / rates | ✅ (ACPI battery driver + powercfg) | ✅ |
| Thermal throttling (passive limit) | ✅ (ACPI) | ✅ |

Desktop PCs without a battery report `battery.*` as Unavailable ("No battery present"), and the
twin keeps working. The same isolation applies to a missing GPU, a disconnected network adapter,
or a missing performance-counter object.

## Health engine

Every component starts at 100. Each rule deducts points and records a reason (shown in the UI).

| Component | Rule | Deduction |
|---|---|---|
| CPU | temperature ≥ 80 / 90 / 98 °C (package sensor, else hottest ACPI zone, labelled) | −10 / −25 / −40 |
| CPU | firmware passive limit < 100 % (throttling) | −15 |
| CPU | 60 s average load ≥ 90 % | −5 |
| GPU | temperature ≥ 85 / 95 °C; sustained load ≥ 95 % | −15 / −35; −5 |
| Memory | 60 s average ≥ 85 / 95 %; page file ≥ 50 % | −10 / −25; −5 |
| Storage | volume ≥ 90 / 95 % full | −15 / −30 |
| Disk | Windows health Warning / Unhealthy; avg latency ≥ 50 ms; temp ≥ 70 °C | −40 / −80; −10; −15 |
| Battery | wear (100 − health %), capped at 60; cycles ≥ 800; < 10 % while discharging | −wear; −5; −10 |
| Thermal | zone ≥ 80 / 90 / 98 °C; passive limit < 100 % | −8 / −20 / −40; −20 |
| Network | no adapter with link; ≥ 10 errors/s | −10; −10 |

Status bands: ≥ 85 healthy, ≥ 60 warning, otherwise critical. A component with **no observable
data** gets `score: null` / `unknown` and is **excluded** from the overall score. The overall
score is the weighted average of CPU (0.20), thermal (0.20), memory (0.15), storage (0.15),
battery (0.15), GPU (0.10) and network (0.05).

## Anomaly engine

**V1, rules** (`domain/anomalies/rules.py`). Each rule has a condition, a minimum duration and
hysteresis, so values hovering around a threshold don't make an anomaly flap. Rules cover:

- CPU or thermal zone temperature at 90 / 98 °C
- thermal throttling
- GPU temperature at 87 °C
- memory ≥ 92 % for 60 s (≥ 98 % for 30 s is critical)
- disk space ≥ 90 / 95 %
- disk latency ≥ 50 ms for 15 s
- drive health other than Healthy
- battery health ≤ 80 %
- battery ≤ 15 / 7 % while discharging
- CPU ≥ 95 % for 120 s
- network errors ≥ 10/s for 30 s
- fan at ≥ 95 % of its observed maximum for 120 s

**V2, statistical** (`domain/anomalies/statistical.py`). Each series has an EWMA baseline
(α = 0.01, 120-sample warm-up). An anomaly opens when z ≥ 4 **and** the absolute deviation is at
least a per-metric minimum, for 5 consecutive samples. It resolves after 10 normal samples. This
covers unexpected CPU or GPU spikes, unusual memory growth, temperature rises, and unusual network
or disk traffic.

**V3, ML.** Intentionally not implemented. Rules plus z-scores are explainable and adequate; an ML
model would need labelled incident history to show measurable value.

## Predictions

`GET /api/v1/analytics/predictions` returns four predictions:

| Prediction | Method | Minimum data |
|---|---|---|
| Thermal trend and throttling risk | least squares over 30 min | 5 min |
| Memory pressure | least squares, projected time to 95 % | 5 min |
| Storage capacity trend | least squares over 7 days | 24 h |
| Battery degradation | full ÷ design capacity, wear per cycle | — |

Every result includes `confidence` (`insufficient`, `low`, `medium` or `high`, plus a score),
`method`, `assumptions` and `supporting_metrics`. Wording stays conservative, for example
"Elevated thermal trend detected", never a failure date.
