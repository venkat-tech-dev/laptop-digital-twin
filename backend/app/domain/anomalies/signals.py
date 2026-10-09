"""Behavioral signals: which telemetry the anomaly engine learns, and what "practically different"
means for each (so a statistically significant but meaningless change does not alarm).

Only metrics with stable semantics and enough data quality are included. Battery charge, for
example, is excluded: its level is driven by the user's charging habits, not by device behavior
(low battery stays a safety threshold).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Signal:
    signal_id: str
    title: str  # operational wording ("Memory usage")
    field: str  # digital-twin field; its provenance names the concrete series on each device
    unit: str
    category: str  # performance | memory | thermal | storage | network
    family: str  # correlation family (signals of one family can form an incident)
    direction: str  # "up": only increases are abnormal; "both"
    min_delta: float  # minimum absolute deviation from the baseline median to be practically relevant
    min_scale: float  # floor of the robust sigma (flat histories)
    impact: int  # 0..2 potential operational impact (severity input)
    warning_level: float | None = None  # safety-threshold proximity (same numbers as twin rules)
    critical_level: float | None = None
    multivariate: bool = True  # feature of the per-device Isolation Forest


SIGNALS: tuple[Signal, ...] = (
    Signal(
        "cpu",
        "CPU usage",
        "performance.cpu.usage_percent",
        "%",
        "performance",
        "compute",
        "up",
        15.0,
        3.0,
        1,
        90,
        95,
    ),
    Signal(
        "memory",
        "Memory usage",
        "performance.memory.usage_percent",
        "%",
        "memory",
        "compute",
        "up",
        8.0,
        1.5,
        2,
        90,
        95,
    ),
    Signal(
        "gpu",
        "GPU usage",
        "performance.gpu.usage_percent",
        "%",
        "performance",
        "compute",
        "up",
        20.0,
        3.0,
        0,
        90,
        95,
    ),
    Signal(
        "temperature",
        "CPU-area temperature",
        "thermal.temperature_c",
        "°C",
        "thermal",
        "compute",
        "up",
        8.0,
        1.5,
        2,
        90,
        98,
    ),
    Signal(
        "disk_active",
        "Drive activity",
        "performance.disk.active_time_percent",
        "%",
        "storage",
        "io",
        "up",
        30.0,
        5.0,
        1,
        90,
        98,
    ),
    Signal(
        "disk_write",
        "Drive write rate",
        "performance.disk.write_bytes_per_sec",
        "B/s",
        "storage",
        "io",
        "up",
        20_000_000,
        1_000_000,
        0,
    ),
    Signal(
        "disk_read",
        "Drive read rate",
        "performance.disk.read_bytes_per_sec",
        "B/s",
        "storage",
        "io",
        "up",
        50_000_000,
        1_000_000,
        0,
    ),
    Signal(
        "net_latency",
        "Gateway latency",
        "network.gateway_latency_ms",
        "ms",
        "network",
        "network",
        "up",
        30.0,
        2.0,
        1,
        150,
        500,
    ),
    Signal(
        "net_rx",
        "Network download",
        "network.rx_bytes_per_sec",
        "B/s",
        "network",
        "network",
        "up",
        5_000_000,
        50_000,
        0,
        None,
        None,
        False,
    ),
    Signal(
        "net_tx",
        "Network upload",
        "network.tx_bytes_per_sec",
        "B/s",
        "network",
        "network",
        "up",
        2_000_000,
        50_000,
        0,
        None,
        None,
        False,
    ),
)

SIGNALS_BY_ID = {s.signal_id: s for s in SIGNALS}
SIGNALS_BY_FIELD = {s.field: s for s in SIGNALS}

#: Families that may form one correlated incident, and its operational name.
FAMILY_TITLES = {
    "compute": "Correlated performance incident",
    "io": "Correlated storage activity incident",
    "network": "Correlated network incident",
}

#: Physically coupled signals (target, driver, residual floor): the multivariate model learns each
#: device's own relation and treats the residual as a feature. Titles explain them in evidence.
RELATIONS: tuple[tuple[str, str, float], ...] = (("temperature", "cpu", 0.75),)
RELATION_TITLES = {"temperature~cpu": "Temperature relative to CPU load"}


def feature_title(feature: str) -> str:
    """Operational name of a model feature (signal or relation)."""
    if feature in RELATION_TITLES:
        return RELATION_TITLES[feature]
    sig = SIGNALS_BY_ID.get(feature)
    return sig.title if sig else feature
