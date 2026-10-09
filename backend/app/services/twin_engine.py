"""Digital twin state engine.

    validated telemetry -> DigitalTwinService (latest reading per series, out-of-order safe)
                        -> TwinEngine.project()  (normalized fields, severity, freshness,
                                                  connectivity, health, timeline events)
                        -> versioned TwinDocument + patch {path: field} -> WebSocket / REST

The document is the authoritative *current* state; history stays in TimescaleDB. Every change
increments ``twin_version`` (per device, monotonic within an ``epoch`` = backend boot). A client that
sees a version gap or another epoch re-fetches the snapshot. Time-driven changes (a field turning
STALE, a device going OFFLINE) are produced by ``tick()``.

Determinism: for the same telemetry sequence and the same evaluation times the engine produces the
same documents, versions and events (no randomness, no wall-clock reads inside the rules).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.domain.telemetry.models import MetricReading
from app.domain.twin.projection import FieldValue, project_fields
from app.domain.twin.rules import (
    AGENT_FAILING_COLLECTORS_WARN,
    AGENT_QUEUE_WARN,
    BOOLEAN_ALERTS,
    CPU_SUSTAINED_PERCENT,
    CPU_SUSTAINED_WINDOW_S,
    HEALTH_RANK,
    HEALTH_RULES,
    SECTIONS,
    SEVERITY_RANK,
    THRESHOLDS,
    Connectivity,
    Freshness,
    Health,
    Severity,
)

TWIN_NAMESPACE = uuid.UUID("0b6f3c1e-4a8e-5d2f-9c71-6e1f2a3b4c5d")

#: Human labels for timeline messages.
LABELS: dict[str, str] = {
    "performance.cpu.usage_percent": "CPU usage",
    "performance.cpu.temperature_c": "CPU temperature",
    "performance.memory.usage_percent": "Memory usage",
    "performance.disk.usage_percent": "System drive usage",
    "performance.disk.active_time_percent": "Drive activity",
    "performance.gpu.usage_percent": "GPU usage",
    "thermal.temperature_c": "Temperature",
    "thermal.throttling": "Thermal throttling",
    "battery.charge_percent": "Battery",
    "battery.health_percent": "Battery health",
    "network.internet_connected": "Internet access",
    "network.device_connected": "Network connection",
    "network.gateway_latency_ms": "Gateway latency",
    "network.packet_loss_percent": "Packet loss",
    "storage.wear_percent": "Drive wear",
    "storage.smart_critical_warning": "Drive SMART warning",
    "storage.health_ok": "Drive health",
    "security.realtime_protection": "Real-time protection",
    "security.antivirus_enabled": "Antivirus",
    "security.firewall_enabled": "Firewall",
    "security.secure_boot": "Secure Boot",
    "security.signature_age_days": "Antivirus signatures",
    "operating_system.reboot_required": "Restart required",
}
#: Fields whose severity transitions become timeline events (others only change the visual).
TIMELINE_FIELDS = frozenset(LABELS) - {
    "performance.disk.active_time_percent",
    "performance.gpu.usage_percent",
}


@dataclass
class TwinPolicy:
    publish_wait_s: float = 5.0
    grace_s: float = 5.0
    default_interval_s: float = 5.0
    process_details_allowed: bool = True

    def limits(self, interval_s: float | None) -> tuple[float, float]:
        interval = interval_s or self.default_interval_s
        wait = self.publish_wait_s + self.grace_s
        return interval + wait, max(4 * interval, 60.0) + wait


@dataclass
class TwinEvent:
    event_id: str
    device_id: str
    type: str  # twin.threshold | twin.connectivity | twin.health
    severity: str  # info | warning | error | critical
    timestamp: datetime
    message: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "device_id": self.device_id,
            "type": self.type,
            "severity": self.severity,
            "timestamp": self.timestamp.isoformat(),
            "message": self.message,
            "data": self.data,
        }


@dataclass
class TwinChange:
    device_id: str
    version: int
    base_version: int
    changes: dict[str, Any]
    events: list[TwinEvent]
    connectivity: tuple[str, str] | None = None  # (previous, new)
    sync_required: str | None = None
    #: wire form: whole values to replace (``replace``) and partial updates of field objects
    #: (``merge``: only the sub-keys that changed, e.g. value / timestamp / source.sequence)
    replace: dict[str, Any] = field(default_factory=dict)
    merge: dict[str, Any] = field(default_factory=dict)


@dataclass
class TwinDocument:
    device_id: str
    epoch: str
    version: int = 0
    state: dict[str, Any] = field(default_factory=dict)  # flat: path -> JSON value
    severities: dict[str, Severity] = field(default_factory=dict)
    readings: dict[str, MetricReading | None] = field(default_factory=dict)  # path -> source reading
    intervals: dict[str, float | None] = field(default_factory=dict)
    static: set[str] = field(default_factory=set)
    restored: bool = False
    next_freshness_check: float = 0.0  # epoch seconds
    projected_at: datetime | None = None

    def nested(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for path, value in self.state.items():
            node = out
            parts = path.split(".")
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = value
        return out


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def twin_id_for(device_id: str) -> str:
    return str(uuid.uuid5(TWIN_NAMESPACE, device_id))


class TwinEngine:
    def __init__(self, policy: TwinPolicy | None = None, epoch: str | None = None) -> None:
        self.policy = policy or TwinPolicy()
        self.epoch = epoch or uuid.uuid4().hex[:12]
        self.docs: dict[str, TwinDocument] = {}
        self.projections = 0
        self.projection_seconds = 0.0

    # ------------------------------------------------------------------ public
    def document(self, device_id: str) -> TwinDocument:
        doc = self.docs.get(device_id)
        if doc is None:
            doc = self.docs[device_id] = TwinDocument(device_id, self.epoch)
        return doc

    def project(
        self, twin: Any, now: datetime, presence: str, extras: dict[str, Any] | None = None
    ) -> TwinChange | None:
        """Recompute the document of ``twin`` (a ``TwinState``) after new telemetry was applied."""
        import time

        started = time.perf_counter()
        doc = self.document(twin.device.device_id)
        fields = project_fields(twin.components, twin.processes, self.policy.process_details_allowed)
        new_state, events = self._build(doc, twin, fields, now, presence, extras or {})
        change = self._commit(doc, new_state, events, now)
        self.projections += 1
        self.projection_seconds += time.perf_counter() - started
        return change

    def tick(self, twins: dict[str, Any], presence_of: Any, now: datetime) -> list[TwinChange]:
        """Time-driven transitions: field freshness (LIVE -> RECENT -> STALE) and connectivity."""
        changes: list[TwinChange] = []
        ts = now.timestamp()
        for device_id, doc in self.docs.items():
            twin = twins.get(device_id)
            if twin is None:
                continue
            presence = presence_of(device_id)
            connectivity = self._connectivity(twin, presence, now)
            if ts < doc.next_freshness_check and doc.state.get("connectivity.status") == connectivity:
                continue
            new_state = dict(doc.state)
            events: list[TwinEvent] = []
            self._derive(doc, twin, new_state, events, now, presence, recompute_fields=False)
            change = self._commit(doc, new_state, events, now)
            if change is not None:
                changes.append(change)
        return changes

    def snapshot(self, device_id: str, flat: bool = False) -> dict[str, Any] | None:
        """``state`` nested for readability; ``flat=True`` returns the same keys that patches use."""
        doc = self.docs.get(device_id)
        if doc is None:
            return None
        return {
            "twin_id": twin_id_for(device_id),
            "device_id": device_id,
            "twin_version": doc.version,
            "epoch": doc.epoch,
            "projected_at": _iso(doc.projected_at),
            "restored_from_cache": doc.restored,
            "state": dict(doc.state) if flat else doc.nested(),
            "format": "flat" if flat else "nested",
        }

    def explain(self, device_id: str, path: str) -> dict[str, Any] | None:
        """Why does the twin say this? Source reading, provenance and the rule that applied."""
        doc = self.docs.get(device_id)
        if doc is None or path not in doc.state:
            return None
        value = doc.state[path]
        r = doc.readings.get(path)
        th = THRESHOLDS.get(path)
        return {
            "path": path,
            "current": value,
            "twin_version": doc.version,
            "source_reading": None
            if r is None
            else {
                "metric_key": r.key,
                "value": r.value,
                "unit": r.unit,
                "collected_at": r.timestamp.isoformat(),
                "source": r.source,
                "quality": r.quality.value,
                "available": r.available,
                "reason": r.reason,
                "interval_s": r.interval_s,
            },
            "severity_rule": None
            if th is None
            else {
                "elevated": th.elevated,
                "warning": th.warning,
                "critical": th.critical,
                "higher_is_worse": th.higher_is_worse,
                "hysteresis": th.hysteresis,
            },
            "boolean_rule": {"alert_when": BOOLEAN_ALERTS[path][0], "severity": BOOLEAN_ALERTS[path][1].value}
            if path in BOOLEAN_ALERTS
            else None,
            "freshness_limits_s": dict(
                zip(("live", "recent"), self.policy.limits(doc.intervals.get(path)), strict=True)
            ),
        }

    # ----------------------------------------------------------- persistence
    def dump(self, device_id: str) -> str | None:
        doc = self.docs.get(device_id)
        if doc is None:
            return None
        return json.dumps(
            {
                "version": doc.version,
                "state": doc.state,
                "severities": {k: v.value for k, v in doc.severities.items()},
                "intervals": doc.intervals,
                "static": sorted(doc.static),
            },
            default=str,
        )

    def restore(self, device_id: str, raw: str) -> None:
        """Last known state after a backend restart: values keep their original timestamps, so their
        freshness is honest (STALE/OFFLINE) until new telemetry arrives."""
        data = json.loads(raw)
        doc = self.document(device_id)
        doc.version = int(data.get("version", 0))
        doc.state = dict(data.get("state", {}))
        doc.severities = {k: Severity(v) for k, v in data.get("severities", {}).items()}
        doc.intervals = dict(data.get("intervals", {}))
        doc.static = set(data.get("static", []))
        doc.restored = True
        doc.state["twin.restored"] = True

    # ---------------------------------------------------------------- internals
    def _build(
        self,
        doc: TwinDocument,
        twin: Any,
        fields: dict[str, FieldValue],
        now: datetime,
        presence: str,
        extras: dict[str, Any],
    ) -> tuple[dict[str, Any], list[TwinEvent]]:
        state = dict(doc.state)
        events: list[TwinEvent] = []
        for path, fv in fields.items():
            if fv.reading is None and fv.value is None and state.get(path, {}).get("value") is not None:
                continue  # no reading in this process (e.g. after a restart): keep the last known value
            doc.readings[path] = fv.reading
            doc.intervals[path] = fv.reading.interval_s if fv.reading else None
            if fv.static or fv.reading is None:
                doc.static.add(path)
            else:
                doc.static.discard(path)
            prov = twin.provenance.get(fv.reading.key) if fv.reading else None
            state[path] = {
                "value": fv.value,
                "unit": fv.unit,
                "label": fv.label,
                "reason": fv.unsupported_reason,
                "timestamp": _iso(fv.reading.timestamp) if fv.reading else None,
                "interval_s": doc.intervals[path],
                "source": None
                if fv.reading is None
                else {
                    "metric_key": fv.reading.key,
                    "origin": fv.reading.source,
                    "batch_id": prov[0] if prov else None,
                    "sequence": prov[1] if prov else None,
                    "received_at": _iso(prov[2]) if prov else None,
                },
                # status / freshness are filled by _derive
                "status": state.get(path, {}).get("status", Severity.UNKNOWN.value),
                "freshness": state.get(path, {}).get("freshness", Freshness.UNKNOWN.value),
            }
        self._identity(state, twin, extras)
        if any(fv.reading is not None for fv in fields.values()):
            doc.restored = False  # live telemetry has been projected since the restart
        state["twin.restored"] = doc.restored
        self._derive(doc, twin, state, events, now, presence, recompute_fields=True)
        return state, events

    def _identity(self, state: dict[str, Any], twin: Any, extras: dict[str, Any]) -> None:
        d = twin.device
        inv = d.inventory or {}
        os_ = inv.get("os") or {}
        cpu = inv.get("cpu") or {}
        mem = inv.get("memory") or {}
        storage = inv.get("storage") or []
        gpus = inv.get("gpu") or []
        battery = inv.get("battery") or {}
        state.update(
            {
                "identity.twin_id": twin_id_for(d.device_id),
                "identity.device_id": d.device_id,
                "identity.agent_id": inv.get("agent_id"),
                "identity.hostname": inv.get("hostname"),
                "identity.manufacturer": d.manufacturer,
                "identity.model": d.model,
                "identity.model_number": d.model_number,
                "identity.agent_version": d.agent_version,
                "identity.first_seen": _iso(d.first_seen),
                "identity.owner": extras.get("owner"),
                "identity.department": extras.get("department"),
                "operating_system.name": os_.get("name") or d.os_name,
                "operating_system.version": os_.get("display_version") or os_.get("version"),
                "operating_system.build": os_.get("build"),
                "hardware.cpu_model": cpu.get("model"),
                "hardware.cpu_cores": cpu.get("cores"),
                "hardware.cpu_threads": cpu.get("threads"),
                "hardware.memory_total_bytes": mem.get("total_bytes"),
                "hardware.storage": [
                    {"model": s.get("model"), "size_bytes": s.get("size_bytes")}
                    for s in storage
                    if isinstance(s, dict)
                ][:4],
                "hardware.gpus": [g.get("name") for g in gpus if isinstance(g, dict)][:4],
                "hardware.battery_design_wh": battery.get("design_capacity_wh")
                if isinstance(battery, dict)
                else None,
            }
        )
        agent = twin.agent_health or {}
        state["agent.version"] = agent.get("agent_version") or d.agent_version
        state["agent.run_mode"] = agent.get("run_mode")
        state["agent.queue_depth"] = agent.get("queue_depth")
        state["agent.collectors_failing"] = (
            sum(1 for c in agent.get("collectors") or [] if (c.get("consecutive_failures") or 0) > 0)
            if agent
            else None
        )
        state["agent.sync_failures"] = agent.get("sync_failures_consecutive")
        state["agent.last_sync_at"] = agent.get("last_sync_at")
        state["agent.reported_at"] = agent.get("received_at")
        posture = twin.device_health or {}
        state["security.posture"] = posture.get("state")
        behavior = getattr(twin, "behavior_active", None) or []
        active = sorted(
            [*twin.anomalies.active.values(), *behavior], key=lambda a: a.started_at, reverse=True
        )
        state["alerts.active_count"] = len(active)
        state["alerts.active"] = [_alert(a) for a in active[:10]]
        highest = max((a.effective_level for a in active), key=lambda lv: lv.rank, default=None)
        state["alerts.highest_severity"] = highest.value if highest else None
        recent = list(getattr(twin, "anomalies_recent", None) or [])[:5]
        state["alerts.recent"] = [_alert(a) for a in recent]
        state["alerts.anomaly_count"] = len(active) + len(recent)
        # Phase 5: forecasts are a separate concept (PREDICTED), never mixed into observed fields
        preds = getattr(twin, "predictions", None) or {}
        for tid, item in preds.items():
            state[f"predictions.{tid}"] = item
        with_pred = [i for i in preds.values() if i.get("prediction_id")]
        state["predictions.active_count"] = len(with_pred)
        order = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
        sev = [i["severity"] for i in with_pred if i.get("severity") in order]
        state["predictions.highest_severity"] = max(sev, key=order.index) if sev else None
        # Phase 7: diagnoses explain (DIAGNOSED); they never change observed or anomalous fields
        diag = getattr(twin, "diagnoses", None) or {}
        state["diagnoses.active_count"] = diag.get("active_count", 0)
        state["diagnoses.latest"] = diag.get("latest")
        state["diagnoses.generating"] = bool(diag.get("generating"))
        # Phase 8: what remediation is doing about it (the physical endpoint stays the source of truth)
        rem = getattr(twin, "remediation", None) or {}
        state["remediation.status"] = rem.get("status", "NONE")
        state["remediation.pending_count"] = rem.get("pending_count", 0)
        state["remediation.latest"] = rem.get("latest")

    def _connectivity(self, twin: Any, presence: str, now: datetime) -> str:
        last = twin.device.last_seen
        live_limit, _ = self.policy.limits(None)
        late = last is None or (now - last).total_seconds() > live_limit
        if presence == "OFFLINE":
            return Connectivity.OFFLINE.value
        if presence == "STALE":
            return Connectivity.STALE.value
        if presence == "UNKNOWN":
            if last is None:
                return Connectivity.UNKNOWN.value
            age = (now - last).total_seconds()
            return (
                Connectivity.ONLINE.value
                if not late
                else (Connectivity.STALE.value if age < 300 else Connectivity.OFFLINE.value)
            )
        agent = twin.agent_health or {}
        failing = sum(1 for c in agent.get("collectors") or [] if (c.get("consecutive_failures") or 0) > 0)
        if late or failing >= AGENT_FAILING_COLLECTORS_WARN:
            return Connectivity.DEGRADED.value
        return Connectivity.ONLINE.value

    def _freshness(
        self, doc: TwinDocument, path: str, field_state: dict[str, Any], now: datetime, connectivity: str
    ) -> tuple[str, float | None]:
        """Freshness of one field and the epoch time of its next transition (or None)."""
        if connectivity == Connectivity.OFFLINE.value:
            return Freshness.OFFLINE.value, None
        if field_state.get("reason") and field_state.get("value") is None:
            return Freshness.UNSUPPORTED.value, None
        ts = field_state.get("timestamp")
        if path in doc.static:
            if field_state.get("value") is None:
                return Freshness.UNKNOWN.value, None
            return (
                Freshness.LIVE.value if connectivity in ("ONLINE", "DEGRADED") else Freshness.STALE.value
            ), None
        if ts is None:
            return Freshness.UNKNOWN.value, None
        t = datetime.fromisoformat(ts)
        age = (now - t).total_seconds()
        live, recent = self.policy.limits(doc.intervals.get(path))
        if age <= live:
            return Freshness.LIVE.value, t.timestamp() + live
        if age <= recent:
            return Freshness.RECENT.value, t.timestamp() + recent
        return Freshness.STALE.value, None

    def _severity(self, doc: TwinDocument, path: str, state: dict[str, Any]) -> Severity:
        value = state[path].get("value")
        if value is None:
            return Severity.UNKNOWN
        prev = doc.severities.get(path)
        if path in THRESHOLDS and isinstance(value, (int, float)) and not isinstance(value, bool):
            if path == "battery.charge_percent" and self._on_ac(state):
                return Severity.NORMAL  # a low charge is only a problem while running on battery
            return THRESHOLDS[path].classify(float(value), prev)
        if path in BOOLEAN_ALERTS:
            bad, sev = BOOLEAN_ALERTS[path]
            return sev if value == bad else Severity.NORMAL
        return Severity.NORMAL

    @staticmethod
    def _on_ac(state: dict[str, Any]) -> bool:
        src = (state.get("battery.power_source") or {}).get("value")
        charging = (state.get("battery.charging_state") or {}).get("value")
        return src == "ac" or charging in ("charging", "full", "charged")

    def _derive(
        self,
        doc: TwinDocument,
        twin: Any,
        state: dict[str, Any],
        events: list[TwinEvent],
        now: datetime,
        presence: str,
        recompute_fields: bool,
    ) -> None:
        connectivity = self._connectivity(twin, presence, now)
        prev_connectivity = doc.state.get("connectivity.status")
        state["connectivity.status"] = connectivity
        state["connectivity.presence"] = presence
        state["connectivity.last_telemetry_at"] = _iso(twin.device.last_seen)
        next_check: float | None = None
        for path, fs in list(state.items()):
            if not isinstance(fs, dict) or "interval_s" not in fs:  # metric fields only
                continue
            fresh, nxt = self._freshness(doc, path, fs, now, connectivity)
            status = fs["status"]
            if recompute_fields:
                sev = self._severity(doc, path, state)
                prev_sev = doc.severities.get(path)
                doc.severities[path] = sev
                status = sev.value
                if path in TIMELINE_FIELDS and prev_sev != sev:
                    ev = self._threshold_event(doc, twin, path, state, prev_sev, sev, now)
                    if ev is not None:
                        events.append(ev)
            if fresh != fs["freshness"] or status != fs["status"]:
                state[path] = {**fs, "freshness": fresh, "status": status}
            if nxt is not None:
                next_check = nxt if next_check is None else min(next_check, nxt)
        doc.next_freshness_check = next_check if next_check is not None else now.timestamp() + 30.0
        self._sections(doc, state, connectivity)
        self._health(doc, twin, state, events, now, connectivity)
        if prev_connectivity is not None and prev_connectivity != connectivity:
            events.append(self._connectivity_event(doc, prev_connectivity, connectivity, now))

    def _sections(self, doc: TwinDocument, state: dict[str, Any], connectivity: str) -> None:
        overall = Severity.UNKNOWN
        for name, paths in SECTIONS.items():
            known = [(doc.severities.get(p, Severity.UNKNOWN), p) for p in paths if p in state]
            known = [(s, p) for s, p in known if s is not Severity.UNKNOWN]
            if connectivity == Connectivity.OFFLINE.value:
                visual = Severity.OFFLINE
            elif connectivity == Connectivity.UNKNOWN.value or not known:
                visual = Severity.UNKNOWN
            else:
                visual = max(known, key=lambda sp: SEVERITY_RANK[sp[0]])[0]
            worst = [p for s, p in known if s is visual and visual not in (Severity.NORMAL,)]
            primary = state.get(paths[0]) or {}
            state[f"sections.{name}"] = {
                "visual": visual.value,
                "because": worst,
                "freshness": primary.get("freshness", Freshness.UNKNOWN.value),
            }
            if SEVERITY_RANK.get(visual, -1) > SEVERITY_RANK.get(overall, -1):
                overall = visual
        if connectivity == Connectivity.OFFLINE.value:
            overall = Severity.OFFLINE
        state["sections.device"] = {"visual": overall.value}

    def _health(
        self,
        doc: TwinDocument,
        twin: Any,
        state: dict[str, Any],
        events: list[TwinEvent],
        now: datetime,
        connectivity: str,
    ) -> None:
        reasons: list[dict[str, Any]] = []
        evaluated = 0
        for rule in HEALTH_RULES:
            sevs = [
                (doc.severities.get(p), p)
                for p in rule.fields
                if doc.severities.get(p) not in (None, Severity.UNKNOWN)
            ]
            if not sevs:
                continue
            evaluated += 1
            worst, path = max(sevs, key=lambda sp: SEVERITY_RANK[sp[0]])  # type: ignore[index]
            level = (
                Health.CRITICAL
                if worst is Severity.CRITICAL
                else Health.WARNING
                if worst is Severity.WARNING
                else None
            )
            if level is not None:
                v = (state.get(path) or {}).get("value")
                unit = (state.get(path) or {}).get("unit")
                reasons.append(
                    {
                        "rule": rule.rule_id,
                        "state": level.value,
                        "field": path,
                        "message": f"{LABELS.get(path, path)}: {self._fmt(v, unit)}",
                        "rule_description": rule.description,
                    }
                )
        # sustained CPU (instantaneous peaks are normal)
        cpu_key = doc.readings.get("performance.cpu.usage_percent") or None
        if cpu_key is not None:
            evaluated += 1
            pts = twin.window.values_since(
                cpu_key.key, cpu_key.timestamp.timestamp() - CPU_SUSTAINED_WINDOW_S
            )
            span = (pts[-1][0] - pts[0][0]) if len(pts) > 1 else 0.0
            if (
                span >= CPU_SUSTAINED_WINDOW_S * 0.8
                and sum(v for _, v in pts) / len(pts) >= CPU_SUSTAINED_PERCENT
            ):
                reasons.append(
                    {
                        "rule": "cpu_sustained",
                        "state": Health.WARNING.value,
                        "field": "performance.cpu.usage_percent",
                        "message": f"CPU >= {CPU_SUSTAINED_PERCENT:.0f}% for {CPU_SUSTAINED_WINDOW_S:.0f} s",
                        "rule_description": "Mean CPU usage >= 90 % over the last 120 s (warning)",
                    }
                )
        posture = state.get("security.posture")
        twin_security = max(
            (Health(r["state"]) for r in reasons if r["rule"] == "security"),
            key=lambda h: HEALTH_RANK[h],
            default=Health.HEALTHY,
        )
        if posture in ("WARNING", "CRITICAL"):
            evaluated += 1
            # the endpoint's own posture verdict adds information only when it is worse than what the
            # twin's security rule already reports (avoids listing the same finding twice)
            if HEALTH_RANK[Health(posture)] > HEALTH_RANK[twin_security]:
                reasons.append(
                    {
                        "rule": "posture",
                        "state": posture,
                        "field": "security.posture",
                        "message": "; ".join((twin.device_health or {}).get("reasons") or [])[:300]
                        or posture,
                        "rule_description": "Security posture evaluated on the endpoint",
                    }
                )
        failing = state.get("agent.collectors_failing") or 0
        queue = state.get("agent.queue_depth") or 0
        if failing >= AGENT_FAILING_COLLECTORS_WARN or queue >= AGENT_QUEUE_WARN:
            reasons.append(
                {
                    "rule": "agent",
                    "state": Health.WARNING.value,
                    "field": "agent.collectors_failing",
                    "message": f"{failing} collectors failing, {queue} batches queued",
                    "rule_description": ">= 3 failing collectors or >= 1000 queued batches (warning)",
                }
            )
        for a in state.get("alerts.active") or []:
            # Safety thresholds drive health directly. Learned (behavioral) anomalies only when they
            # are both severe and confident: "unusual for this device" alone is not "unhealthy".
            if a.get("type") == "threshold_anomaly":
                effect = a["severity"].upper() if a["severity"] in ("warning", "critical") else None
            else:
                strong = a.get("level") in ("HIGH", "CRITICAL") and (a.get("confidence") or 0) >= 0.7
                effect = Health.WARNING.value if strong else None
            if effect:
                reasons.append(
                    {
                        "rule": "anomaly",
                        "state": effect,
                        "field": a.get("metric_key"),
                        "message": a["title"],
                        "rule_description": "Active safety-threshold anomaly, or a HIGH/CRITICAL "
                        "behavioral anomaly with confidence >= 0.7 (warning)",
                    }
                )
        computed = Health.HEALTHY if evaluated else Health.UNKNOWN
        for r in reasons:
            h = Health(r["state"])
            if HEALTH_RANK[h] > HEALTH_RANK[computed]:
                computed = h
        prev = (doc.state.get("health") or {}).get("state")
        if connectivity in (Connectivity.OFFLINE.value, Connectivity.UNKNOWN.value):
            last_known = (doc.state.get("health") or {}).get("last_known") or prev
            state["health"] = {
                "state": Health.UNKNOWN.value,
                "last_known": last_known if last_known != "UNKNOWN" else None,
                "reasons": [],
                "evaluated_at": _iso(now),
                "note": "Device not connected: current health cannot be determined",
            }
        else:
            state["health"] = {
                "state": computed.value,
                "last_known": computed.value,
                "reasons": reasons,
                "evaluated_at": (doc.state.get("health") or {}).get("evaluated_at")
                if prev == computed.value and (doc.state.get("health") or {}).get("reasons") == reasons
                else _iso(now),
            }
        new = state["health"]["state"]
        if prev is not None and prev != new and new != Health.UNKNOWN.value:
            sev = {"CRITICAL": "critical", "WARNING": "warning"}.get(new, "info")
            msg = f"Health {prev} -> {new}" + (f": {reasons[0]['message']}" if reasons else "")
            events.append(
                TwinEvent(
                    f"{doc.device_id}:health:{doc.version + 1}",
                    doc.device_id,
                    "twin.health",
                    sev,
                    now,
                    msg,
                    {
                        "from": prev,
                        "to": new,
                        "reasons": [r["rule"] for r in reasons],
                        "details": [
                            {"rule": r["rule"], "state": r["state"], "message": r["message"]} for r in reasons
                        ],
                    },
                )
            )

    @staticmethod
    def _fmt(v: Any, unit: str | None) -> str:
        if isinstance(v, bool):
            return "on" if v else "off"
        if isinstance(v, (int, float)):
            suffix = "" if unit in (None, "bool") else unit if unit in ("%", "°C") else f" {unit}"
            return f"{v:.0f}{suffix}"
        return f"{v}{'' if unit in (None, 'bool', 'text', 'state') else ' ' + unit}"

    def _threshold_event(
        self,
        doc: TwinDocument,
        twin: Any,
        path: str,
        state: dict[str, Any],
        prev: Severity | None,
        sev: Severity,
        now: datetime,
    ) -> TwinEvent | None:
        if sev is Severity.UNKNOWN:
            return None
        if prev in (None, Severity.UNKNOWN) and SEVERITY_RANK[sev] < SEVERITY_RANK[Severity.WARNING]:
            return None  # first observation of a normal/elevated value is not news
        numeric = path in THRESHOLDS
        prev_rank = SEVERITY_RANK[prev] if prev is not None else -1
        if numeric and max(prev_rank, SEVERITY_RANK[sev]) < SEVERITY_RANK[Severity.WARNING]:
            return None  # normal <-> elevated is routine load, not a timeline event
        fs = state[path]
        value, unit = fs.get("value"), fs.get("unit")
        label = LABELS.get(path, path)
        ts = datetime.fromisoformat(fs["timestamp"]) if fs.get("timestamp") else now
        rising = prev is None or SEVERITY_RANK[sev] > SEVERITY_RANK.get(prev, -1)
        if isinstance(value, bool):
            msg = f"{label} {'restored' if sev is Severity.NORMAL else self._fmt(value, unit)}"
        else:
            msg = f"{label} {'reached' if rising else 'returned to'} {self._fmt(value, unit)} ({sev.value})"
        data: dict[str, Any] = {
            "field": path,
            "from": prev.value if prev else None,
            "to": sev.value,
            "value": value,
            "twin_version": doc.version + 1,
            "source": fs.get("source"),
        }
        if (
            path == "performance.memory.usage_percent"
            and rising
            and sev in (Severity.WARNING, Severity.CRITICAL)
        ):
            top = (state.get("applications.top_memory_process") or {}).get("value")
            if top:
                data["top_memory_process"] = top
                msg += f"; top memory process: {top}"
        level = {Severity.CRITICAL: "critical", Severity.WARNING: "warning"}.get(sev, "info")
        return TwinEvent(
            f"{doc.device_id}:{path}:{doc.version + 1}", doc.device_id, "twin.threshold", level, ts, msg, data
        )

    def _connectivity_event(self, doc: TwinDocument, prev: str, new: str, now: datetime) -> TwinEvent:
        msg = {
            "ONLINE": "Device online",
            "DEGRADED": "Device online, telemetry delayed",
            "STALE": "Device heartbeat missed",
            "OFFLINE": "Device went offline",
            "UNKNOWN": "Device status unknown",
        }[new]
        sev = {"OFFLINE": "warning", "STALE": "warning", "DEGRADED": "warning"}.get(new, "info")
        return TwinEvent(
            f"{doc.device_id}:connectivity:{doc.version + 1}",
            doc.device_id,
            "twin.connectivity",
            sev,
            now,
            msg,
            {"from": prev, "to": new},
        )

    def _commit(
        self, doc: TwinDocument, new_state: dict[str, Any], events: list[TwinEvent], now: datetime
    ) -> TwinChange | None:
        changes: dict[str, Any] = {}
        replace: dict[str, Any] = {}
        merge: dict[str, Any] = {}
        for path, value in new_state.items():
            old = doc.state.get(path)
            if old == value:
                continue
            changes[path] = value
            if (
                isinstance(old, dict)
                and isinstance(value, dict)
                and "interval_s" in old
                and "interval_s" in value
            ):
                partial = {k: v for k, v in value.items() if old.get(k) != v}
                src_old, src_new = old.get("source"), value.get("source")
                if "source" in partial and isinstance(src_old, dict) and isinstance(src_new, dict):
                    partial["source"] = {k: v for k, v in src_new.items() if src_old.get(k) != v}
                merge[path] = partial
            else:
                replace[path] = value
        for path in doc.state.keys() - new_state.keys():
            changes[path] = None
            replace[path] = None
        if not changes and not events:
            return None
        prev_conn = doc.state.get("connectivity.status")
        base = doc.version
        doc.version += 1
        doc.state = new_state
        doc.projected_at = now
        for ev in events:  # ids were reserved for version + 1
            ev.data["twin_version"] = doc.version
        conn = new_state.get("connectivity.status")
        transition = (str(prev_conn), str(conn)) if prev_conn is not None and prev_conn != conn else None
        return TwinChange(
            doc.device_id,
            doc.version,
            base,
            changes,
            events,
            connectivity=transition,
            replace=replace,
            merge=merge,
        )


def _alert(a: Any) -> dict[str, Any]:
    """Compact anomaly entry for the twin document (full record: /anomalies/{id})."""
    return {
        "anomaly_id": a.anomaly_id,
        "severity": a.severity.value,
        "level": a.effective_level.value,
        "type": a.anomaly_type.value,
        "confidence": a.confidence,
        "title": a.title,
        "since": _iso(a.started_at),
        "resolved_at": _iso(a.resolved_at) if a.resolved_at else None,
        "lifecycle": a.lifecycle.value,
        "metric_key": a.metric_key,
        "signal_id": a.signal_id,
        "correlation_key": a.correlation_key,
    }
