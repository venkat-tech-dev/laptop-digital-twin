# ruff: noqa: E501  (prompt / recommendation text catalogue)
"""Rule-based pre-diagnosis: candidate hypotheses with supporting / contradicting / missing evidence.

Rules only *propose* explanations from evidence that already exists; confidence is computed separately
(``confidence.py``). Wording is deliberately hedged ("likely contributor", "consistent with"); no rule
claims a definite cause. Recommendations are investigation steps for a human - never commands, never
actions the platform performs.
"""

from __future__ import annotations

from collections.abc import Callable

from app.domain.diagnosis.context import DiagnosticContext
from app.domain.diagnosis.evidence import EvidenceIndex
from app.domain.diagnosis.models import DiagnosisType, Evidence, Hypothesis

# signal -> category used to decide which hypotheses match the trigger
SIGNAL_CATEGORY: dict[str, DiagnosisType] = {
    "cpu": DiagnosisType.CPU_PRESSURE,
    "memory": DiagnosisType.MEMORY_PRESSURE,
    "disk_active": DiagnosisType.DISK_PRESSURE,
    "disk_usage": DiagnosisType.DISK_PRESSURE,
    "disk": DiagnosisType.DISK_PRESSURE,
    "temperature": DiagnosisType.THERMAL_ISSUE,
    "net_latency": DiagnosisType.NETWORK_DEGRADATION,
    "packet_loss": DiagnosisType.NETWORK_DEGRADATION,
    "network": DiagnosisType.NETWORK_DEGRADATION,
    "battery": DiagnosisType.BATTERY_DEGRADATION,
    "security": DiagnosisType.SECURITY_STATE,
}

# Safe, human-only investigation steps (no commands, nothing the platform executes).
RECOMMENDATIONS: dict[str, list[str]] = {
    "cpu.process": [
        "Check whether {process} is expected to be busy right now (a build, scan, update or sync).",
        "Review {process} in Task Manager over the next few minutes to see whether its usage settles.",
        "If the usage is unexpected, check with the application owner before restarting or updating it.",
    ],
    "cpu.background": [
        "Open Task Manager (sorted by CPU) to see which background tasks are active.",
        "Check whether Windows Update, antivirus scans or indexing are running.",
    ],
    "thermal.workload": [
        "Check whether the heavy workload is expected; temperature should fall once it finishes.",
        "Make sure the laptop sits on a hard, flat surface with the vents unobstructed.",
    ],
    "thermal.cooling": [
        "Check that the air vents are not blocked and the fan is audible under load.",
        "If temperatures stay high at light load, arrange a cooling-system inspection (dust, fan).",
    ],
    "memory.process_growth": [
        "Check whether {process} is expected to hold this much memory (many tabs, large files).",
        "Watch whether {process}'s memory keeps growing; steady growth without a matching workload can indicate a leak.",
    ],
    "memory.leak_like": [
        "Compare memory use now with the start of the session; note which applications have been open longest.",
        "If one application's memory keeps rising, save work and plan a restart of that application at a convenient time.",
    ],
    "memory.overall_load": [
        "Review the number of open applications and browser tabs.",
        "Consider whether this workload regularly needs more memory than the device has.",
    ],
    "disk.capacity": [
        "Review large or old files and the Downloads folder for content that can be archived.",
        "Check the size of the Recycle Bin and temporary files with Windows Storage settings.",
    ],
    "disk.activity": [
        "Check which processes are reading/writing heavily (Task Manager, Disk column).",
        "If memory is also high, the disk activity may be paging; reducing memory load usually reduces it.",
    ],
    "network.degradation": [
        "Check whether other devices on the same network see the same delays.",
        "Check Wi-Fi signal strength or try a wired connection to separate local from upstream problems.",
    ],
    "battery.drain": [
        "Check which applications have high power usage in Windows Battery settings.",
        "Compare the drain rate with typical days; a persistent change can indicate battery wear.",
    ],
    "security.posture": [
        "Ask IT to review the device's security configuration ({finding}).",
        "Confirm whether this configuration is an approved exception for this device.",
    ],
    "resource_contention": [
        "Several resources are busy at once; review which workloads overlap and whether they can be scheduled apart.",
    ],
    "unknown": [
        "Keep observing; collect more telemetry around the next occurrence before acting.",
    ],
}

#: destructive or command-like wording that must never appear in a recommendation (word-bounded regexes)
FORBIDDEN_ACTIONS = (
    r"\bkill",
    r"\bterminat",
    r"\btaskkill\b",
    r"\bstop-process\b",
    r"\bend (the )?task\b",
    r"\bdelet",
    r"\bremove-item\b",
    r"\brm\s+-",
    r"\bdel\s+(/|[a-z]:|\S+\.\w+)",
    r"\bregedit\b",
    r"\bregistry\b",
    r"\breg (add|delete)\b",
    r"\bfirewall\b",
    r"\bnetsh\b",
    r"\bpowershell\b",
    r"\bcmd(\.exe)?\b",
    r"\bcommand prompt\b",
    r"\bformat\s+(the\s+)?(drive|disk|[a-z]:)",
    r"\bdisable\b",
    r"\bsc\s+stop\b",
    r"\bset-\w",
    r"\bbcdedit\b",
    r"\bshutdown\b",
    r"\brun the following\b",
    r"\buninstall",
)


def recommendations(code: str, **values: str) -> list[str]:
    out = []
    for r in RECOMMENDATIONS.get(code, RECOMMENDATIONS["unknown"]):
        try:
            out.append(r.format(**values))
        except KeyError:
            out.append(r.replace("{process}", "the application").replace("{finding}", "see findings"))
    return out


def _ids(*items: Evidence | list[Evidence] | None) -> list[str]:
    out: list[str] = []
    for it in items:
        if it is None:
            continue
        for e in it if isinstance(it, list) else [it]:
            if e.evidence_id not in out:
                out.append(e.evidence_id)
    return out


def _cpu(ctx: DiagnosticContext, ix: EvidenceIndex) -> list[Hypothesis]:
    obs = ix.get("cpu", "observation")
    if obs is None:
        return []
    out = []
    top = ix.processes[0] if ix.processes else None
    share = (top.deviation or 0.0) if top else 0.0
    common = _ids(obs, ix.get("cpu", "temporal"), ix.anomalies.get("cpu", []), ix.get("cpu", "trend"))
    if top is not None and share >= 0.35 and (top.observed or 0) >= 10:
        rose = top.baseline is not None and (top.observed or 0) - top.baseline >= 5
        out.append(
            Hypothesis(
                code="cpu.process",
                category=DiagnosisType.CPU_PRESSURE,
                cause=f"Sustained CPU usage by {top.process} is a likely contributor",
                supporting=common + _ids(top),
                missing=[] if top.baseline is not None else ["process CPU before the episode"],
                recommendations=recommendations("cpu.process", process=top.process or "the application"),
                factors={"process_share": round(share, 2), "process_rose": 1.0 if rose else 0.0},
            )
        )
    out.append(
        Hypothesis(
            code="cpu.background",
            category=DiagnosisType.CPU_PRESSURE,
            cause="Several background tasks together are keeping the CPU busy",
            supporting=common + (_ids(ix.processes[1:3]) if len(ix.processes) > 1 else []),
            contradicting=_ids(top) if top is not None and share >= 0.6 else [],
            missing=["per-process CPU for the window"] if not ix.processes else [],
            recommendations=recommendations("cpu.background"),
        )
    )
    return out


def _memory(ctx: DiagnosticContext, ix: EvidenceIndex) -> list[Hypothesis]:
    obs = ix.get("memory", "observation")
    if obs is None:
        return []
    out = []
    common = _ids(
        obs, ix.get("memory", "temporal"), ix.anomalies.get("memory", []), ix.predictions.get("memory")
    )
    grower = next((e for e in ix.memory_processes if (e.deviation or 0) >= 300), None)
    if grower is not None:
        out.append(
            Hypothesis(
                code="memory.process_growth",
                category=DiagnosisType.MEMORY_PRESSURE,
                cause=f"Memory growth in {grower.process} is a likely contributor",
                supporting=common + _ids(grower),
                recommendations=recommendations(
                    "memory.process_growth", process=grower.process or "the application"
                ),
            )
        )
    tmp = ix.temporal.get("memory")
    trend = ix.get("memory", "trend")
    if (
        trend is not None
        and (trend.observed or 0) > 0
        and tmp is not None
        and tmp.pattern in ("gradual", "recent", "none")
    ):
        out.append(
            Hypothesis(
                code="memory.leak_like",
                category=DiagnosisType.MEMORY_PRESSURE,
                cause="Memory use has been climbing steadily, a pattern consistent with an application holding on to memory",
                supporting=common + _ids(trend, grower),
                contradicting=_ids(ix.get("memory", "recurring")),
                missing=["per-process memory over a longer period"] if grower is None else [],
                recommendations=recommendations("memory.leak_like"),
            )
        )
    out.append(
        Hypothesis(
            code="memory.overall_load",
            category=DiagnosisType.MEMORY_PRESSURE,
            cause="The overall workload needs more memory than is comfortably available",
            supporting=common + _ids(ix.memory_processes),
            contradicting=_ids(grower) if grower is not None and (grower.deviation or 0) >= 1000 else [],
            recommendations=recommendations("memory.overall_load"),
        )
    )
    return out


def _thermal(ctx: DiagnosticContext, ix: EvidenceIndex) -> list[Hypothesis]:
    obs = ix.get("temperature", "observation")
    if obs is None:
        return []
    common = _ids(
        obs,
        ix.get("temperature", "temporal"),
        ix.anomalies.get("temperature", []),
        ix.predictions.get("temperature"),
    )
    cpu_obs = ix.get("cpu", "observation")
    corr = ix.correlations.get(("cpu", "temperature"))
    order_ok = ix.order.get(("cpu", "temperature"))
    order_bad = ix.order.get(("temperature", "cpu"))
    out = [
        Hypothesis(
            code="thermal.workload",
            category=DiagnosisType.THERMAL_ISSUE,
            cause="Heat from a heavy CPU workload is a likely contributor to the high temperature",
            supporting=common + _ids(cpu_obs, corr, order_ok, ix.processes[:1]),
            contradicting=_ids(ix.get("cpu", "absence"), order_bad),
            missing=[] if ctx.series.get("cpu") else ["CPU usage for the same window"],
            recommendations=recommendations("thermal.workload"),
            factors={"temporal_order": 1.0 if order_ok else (-1.0 if order_bad else 0.0)},
        )
    ]
    out.append(
        Hypothesis(
            code="thermal.cooling",
            category=DiagnosisType.THERMAL_ISSUE,
            cause="Temperature is high without a matching workload, consistent with reduced cooling (vents, fan, surface)",
            supporting=common + _ids(ix.get("cpu", "absence")),
            contradicting=_ids(cpu_obs, corr),
            missing=["fan speed (not reported by this device)"],
            recommendations=recommendations("thermal.cooling"),
        )
    )
    return out


def _disk(ctx: DiagnosticContext, ix: EvidenceIndex) -> list[Hypothesis]:
    out = []
    cap = _ids(
        ix.get("disk_usage", "observation"),
        ix.get("disk_usage", "trend"),
        ix.predictions.get("disk"),
        ix.anomalies.get("disk_usage", []),
        ix.anomalies.get("disk", []),
    )
    if cap:
        out.append(
            Hypothesis(
                code="disk.capacity",
                category=DiagnosisType.DISK_PRESSURE,
                cause="Drive space is running low",
                supporting=cap,
                recommendations=recommendations("disk.capacity"),
            )
        )
    act = ix.get("disk_active", "observation")
    if act is not None:
        paging = ix.correlations.get(("memory", "disk_active"))
        out.append(
            Hypothesis(
                code="disk.activity",
                category=DiagnosisType.DISK_PRESSURE,
                cause="Heavy drive activity"
                + (", consistent with paging under memory pressure" if paging else ""),
                supporting=_ids(
                    act,
                    ix.get("disk_active", "temporal"),
                    ix.anomalies.get("disk_active", []),
                    paging,
                    ix.get("memory", "observation") if paging else None,
                ),
                missing=["per-process disk I/O (not collected)"],
                recommendations=recommendations("disk.activity"),
            )
        )
    return out


def _network(ctx: DiagnosticContext, ix: EvidenceIndex) -> list[Hypothesis]:
    sup = _ids(
        ix.get("net_latency", "observation"),
        ix.get("net_latency", "temporal"),
        ix.get("packet_loss", "observation"),
        ix.anomalies.get("net_latency", []),
        ix.anomalies.get("packet_loss", []),
        ix.anomalies.get("network", []),
    )
    if not sup:
        return []
    return [
        Hypothesis(
            code="network.degradation",
            category=DiagnosisType.NETWORK_DEGRADATION,
            cause="Network latency or packet loss to the gateway is elevated",
            supporting=sup,
            missing=["whether other devices on the network are affected"],
            recommendations=recommendations("network.degradation"),
        )
    ]


def _battery(ctx: DiagnosticContext, ix: EvidenceIndex) -> list[Hypothesis]:
    sup = _ids(
        ix.get("battery", "observation"),
        ix.get("battery", "trend"),
        ix.predictions.get("battery"),
        ix.anomalies.get("battery", []),
    )
    if not sup:
        return []
    return [
        Hypothesis(
            code="battery.drain",
            category=DiagnosisType.BATTERY_DEGRADATION,
            cause="Battery is draining faster than usual"
            + (" while the CPU is busy" if ix.get("cpu", "observation") else ""),
            supporting=sup + _ids(ix.get("cpu", "observation")),
            missing=["battery wear level (design vs full-charge capacity)"],
            recommendations=recommendations("battery.drain"),
        )
    ]


def _security(ctx: DiagnosticContext, ix: EvidenceIndex) -> list[Hypothesis]:
    if not ix.security:
        return []
    finding = ", ".join(str(f) for f in (ctx.security.get("findings") or [])[:2])
    return [
        Hypothesis(
            code="security.posture",
            category=DiagnosisType.SECURITY_STATE,
            cause=f"The device's security configuration differs from the expected baseline ({finding})",
            supporting=_ids(ix.security, ix.anomalies.get("security", [])),
            missing=["whether this is an approved exception"],
            recommendations=recommendations("security.posture", finding=finding),
        )
    ]


def _contention(ctx: DiagnosticContext, ix: EvidenceIndex) -> list[Hypothesis]:
    busy = [ix.get(k, "observation") for k in ("cpu", "memory", "disk_active")]
    busy_ev = [e for e in busy if e is not None]
    if len(busy_ev) < 2:
        return []
    return [
        Hypothesis(
            code="resource_contention",
            category=DiagnosisType.RESOURCE_CONTENTION,
            cause="Several resources ("
            + ", ".join(e.metric or "" for e in busy_ev).lower()
            + ") are under pressure at the same time",
            supporting=_ids(busy_ev, list(ix.correlations.values())),
            recommendations=recommendations("resource_contention"),
        )
    ]


RULES: list[Callable[[DiagnosticContext, EvidenceIndex], list[Hypothesis]]] = [
    _cpu,
    _memory,
    _thermal,
    _disk,
    _network,
    _battery,
    _security,
    _contention,
]


def hypotheses(ctx: DiagnosticContext, ix: EvidenceIndex) -> list[Hypothesis]:
    known = ix.ids()
    out: list[Hypothesis] = []
    for rule in RULES:
        for h in rule(ctx, ix):
            h.supporting = [i for i in h.supporting if i in known]
            h.contradicting = [i for i in h.contradicting if i in known and i not in h.supporting]
            if h.supporting:
                out.append(h)
    trigger_ev = [
        e.evidence_id for e in ix.items if e.ref.get("id") and e.ref.get("id") == ctx.trigger.get("id")
    ]
    out.append(
        Hypothesis(
            code="unknown",
            category=DiagnosisType.UNKNOWN,
            cause="The available telemetry does not point to a clear explanation",
            supporting=trigger_ev,
            missing=list(ix.missing) or ["more telemetry around the next occurrence"],
            recommendations=recommendations("unknown"),
        )
    )
    return out
