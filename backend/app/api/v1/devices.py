"""Fleet and device APIs: device list, organization overview, digital twin snapshot / explain,
timeline, history, ownership, pipeline stats.

Current state comes from the in-memory twin documents (no database query); history from
PostgreSQL/TimescaleDB; the timeline from persisted events. Every device-scoped endpoint enforces
device-level authorization (employees: assigned devices only; others answered 404).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.access import Staff, check_device, visible_devices
from app.api.deps import Admin, ContainerDep, Reader, require_platform_scope
from app.services.twin_engine import twin_id_for

router = APIRouter(prefix="/devices", tags=["devices"])
pipeline_router = APIRouter(prefix="/pipeline", tags=["pipeline"])
fleet_router = APIRouter(prefix="/fleet", tags=["devices"])


CONNECTIVITY_ORDER = {"OFFLINE": 0, "STALE": 1, "DEGRADED": 2, "UNKNOWN": 3, "ONLINE": 4}
HEALTH_ORDER = {"CRITICAL": 0, "WARNING": 1, "UNKNOWN": 2, "HEALTHY": 3}
SORT_KEYS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "device": lambda r: (r.get("hostname") or r.get("model") or r["device_id"]).lower(),
    "owner": lambda r: (r.get("owner") or "~").lower(),
    "status": lambda r: CONNECTIVITY_ORDER.get(r.get("connectivity") or "UNKNOWN", 9),
    "health": lambda r: HEALTH_ORDER.get(r.get("health") or "UNKNOWN", 9),
    "cpu": lambda r: _num(r.get("cpu")),
    "memory": lambda r: _num(r.get("memory")),
    "disk": lambda r: _num(r.get("disk")),
    "battery": lambda r: _num(r.get("battery")),
    "temperature": lambda r: _num(r.get("temperature")),
    "last_seen": lambda r: r.get("last_telemetry_at") or "",
}


def _num(v: Any) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else float("-inf")


def _row(container: Any, device: Any) -> dict[str, Any]:
    """Fleet row: the twin summary (current state) plus identity, presence and ownership."""
    summary = container.twin_state.summary(device.device_id) or {}
    presence = container.presence.get(device.device_id)
    a = container.assignments.get(device.device_id)
    deps = container.assignments.departments(device.device_id)
    return {
        "device_id": device.device_id,
        "hostname": summary.get("hostname") or (device.inventory or {}).get("hostname"),
        "manufacturer": device.manufacturer,
        "model": device.model,
        "owner": (a.employee_name or a.username) if a else None,
        "owner_username": a.username if a else None,
        "department": deps[0] if deps else None,
        "connectivity": summary.get("connectivity") or "UNKNOWN",
        "presence": presence.presence.value if presence else "UNKNOWN",
        "health": summary.get("health") or "UNKNOWN",
        "visual": summary.get("visual") or "unknown",
        "active_alerts": summary.get("active_alerts", 0),
        "last_telemetry_at": summary.get("last_telemetry_at"),
        "last_contact_at": presence.last_contact_at.isoformat()
        if presence and presence.last_contact_at
        else None,
        "twin_version": summary.get("twin_version", 0),
        "primary": device.device_id == container.twin.primary_device_id,
        **{k: summary.get(k) for k in ("cpu", "memory", "disk", "battery", "temperature", "internet")},
        **{
            f"{k}_status": summary.get(f"{k}_status")
            for k in ("cpu", "memory", "disk", "battery", "temperature")
        },
    }


def _visible_rows(container: Any, principal: Any) -> list[dict[str, Any]]:
    allowed = visible_devices(principal, container)
    return [_row(container, d) for d in container.twin.devices() if allowed is None or d.device_id in allowed]


def _counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def tally(key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in rows:
            out[r[key]] = out.get(r[key], 0) + 1
        return out

    return {"total": len(rows), "by_health": tally("health"), "by_connectivity": tally("connectivity")}


@router.get("", summary="Device list: server-side search, filters, sorting and pagination")
async def list_devices(
    container: ContainerDep,
    principal: Reader,
    q: str | None = Query(default=None, max_length=100, description="Search id, hostname, model, owner"),
    connectivity: list[str] | None = Query(default=None),
    health: list[str] | None = Query(default=None),
    department: str | None = Query(default=None, max_length=128),
    sort: str = Query(default="health", pattern="^(" + "|".join(SORT_KEYS) + ")$"),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    rows = _visible_rows(container, principal)
    counts = _counts(rows)
    if q:
        needle = q.lower()
        rows = [
            r
            for r in rows
            if any(needle in str(r.get(k) or "").lower() for k in ("device_id", "hostname", "model", "owner"))
        ]
    if connectivity:
        rows = [r for r in rows if r["connectivity"] in connectivity]
    if health:
        rows = [r for r in rows if r["health"] in health]
    if department:
        rows = [r for r in rows if r.get("department") == department]
    key = SORT_KEYS[sort]
    rows.sort(key=lambda r: (key(r), r["device_id"]), reverse=order == "desc")
    total = len(rows)
    start = (page - 1) * page_size
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": rows[start : start + page_size],
        "counts": counts,
    }


@fleet_router.get("/summary", summary="Organization overview: devices by health, connectivity, department")
async def fleet_summary(container: ContainerDep, principal: Reader) -> dict[str, Any]:
    rows = _visible_rows(container, principal)
    departments: dict[str, dict[str, Any]] = {}
    for r in rows:
        dep = r.get("department") or "Unassigned"
        d = departments.setdefault(dep, {"name": dep, "total": 0, "by_health": {}, "by_connectivity": {}})
        d["total"] += 1
        d["by_health"][r["health"]] = d["by_health"].get(r["health"], 0) + 1
        d["by_connectivity"][r["connectivity"]] = d["by_connectivity"].get(r["connectivity"], 0) + 1
    return {
        "organization": "This deployment",
        **_counts(rows),
        "departments": sorted(departments.values(), key=lambda d: d["name"]),
        "generated_at": datetime.now(UTC).isoformat(),
    }


def _require(container: Any, principal: Any, device_id: str) -> Any:
    check_device(principal, container, device_id)
    twin = container.twin.get(device_id) if container.twin.has_device(device_id) else None
    if twin is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown device")
    return twin


@router.get("/{device_id}", summary="Device identity, ownership and current summary")
async def get_device(device_id: str, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    twin = _require(container, principal, device_id)
    a = container.assignments.get(device_id)
    return {
        **_row(container, twin.device),
        "twin_id": twin_id_for(device_id),
        "os_name": twin.device.os_name,
        "agent_version": twin.device.agent_version,
        "first_seen": twin.device.first_seen.isoformat() if twin.device.first_seen else None,
        "assignment": a.public() if a else None,
    }


@router.get("/{device_id}/twin", summary="Digital twin snapshot: identity, current state, health, freshness")
async def get_twin_snapshot(
    device_id: str,
    container: ContainerDep,
    principal: Reader,
    format_: str = Query(default="nested", alias="format", pattern="^(nested|flat)$"),
) -> dict[str, Any]:
    _require(container, principal, device_id)
    snap = container.twin_state.engine.snapshot(device_id, flat=format_ == "flat")
    if snap is None:  # registered but no telemetry/inventory projected yet
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No telemetry received yet for this device")
    return snap


@router.get("/{device_id}/twin/explain", summary="Why does the twin show this value? (provenance + rules)")
async def explain_field(
    device_id: str,
    container: ContainerDep,
    principal: Reader,
    path: str = Query(max_length=128, pattern=r"^[a-z_]+(\.[a-z0-9_]+)+$"),
) -> dict[str, Any]:
    _require(container, principal, device_id)
    out = container.twin_state.engine.explain(device_id, path)
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown twin field")
    return out


TIMELINE_TYPES = ("twin.", "device.", "alert.", "diagnosis.", "remediation.")


@router.get("/{device_id}/timeline", summary="Device timeline: twin and agent events, newest first")
async def timeline(
    device_id: str,
    container: ContainerDep,
    principal: Reader,
    limit: int = Query(default=100, ge=1, le=500),
    severity: str | None = Query(default=None, pattern="^(info|warning|error|critical)$"),
) -> dict[str, Any]:
    _require(container, principal, device_id)
    try:
        stored = await container.event_repo.list_system_events(device_id, 2000)
        source = container.persistence_mode
    except Exception:  # database down: serve what is in memory
        stored, source = [], "unavailable"
    items: list[dict[str, Any]] = []
    for e in stored:
        if not e.event_type.startswith(TIMELINE_TYPES):
            continue
        if severity and e.severity != severity:
            continue
        kind = "agent" if e.event_type.startswith("device.") else e.event_type.removeprefix("twin.")
        items.append(
            {
                "event_id": e.event_uid or f"{e.event_type}:{e.time.isoformat()}",
                "time": e.time.isoformat(),
                "type": e.event_type,
                "kind": kind,
                "severity": e.severity,
                "message": e.message,
                "data": {k: v for k, v in (e.data or {}).items() if k not in ("source",)},
            }
        )
        if len(items) >= limit:
            break
    return {"device_id": device_id, "source": source, "items": items}


RANGES = {"15m": (15, 10), "1h": (60, 30), "6h": (360, 180), "24h": (1440, 720)}


@router.get("/{device_id}/history", summary="History of twin fields or metric keys (mini trends)")
async def device_history(
    device_id: str,
    container: ContainerDep,
    principal: Reader,
    fields: list[str] | None = Query(default=None, max_length=12),
    keys: list[str] | None = Query(default=None, max_length=20),
    range_: str = Query(default="1h", alias="range", pattern="^(15m|1h|6h|24h)$"),
) -> dict[str, Any]:
    _require(container, principal, device_id)
    doc = container.twin_state.engine.docs.get(device_id)
    resolved: dict[str, str] = {}
    for path in fields or []:
        src = ((doc.state.get(path) if doc else None) or {}).get("source") if doc else None
        if isinstance(src, dict) and src.get("metric_key"):
            resolved[path] = src["metric_key"]
    for k in keys or []:
        resolved[k] = k
    if not resolved:
        raise HTTPException(422, "No known fields or keys requested")
    minutes, bucket = RANGES[range_]
    end = datetime.now(UTC)
    start = end - timedelta(minutes=minutes)
    series = await container.telemetry.history(device_id, sorted(set(resolved.values())), start, end, bucket)
    return {
        "device_id": device_id,
        "range": range_,
        "bucket_seconds": bucket,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "series": {
            name: [
                {"t": p.time.isoformat(), "avg": p.avg, "min": p.min, "max": p.max}
                for p in series.get(key, [])
            ]
            for name, key in resolved.items()
        },
    }


class AssignmentIn(BaseModel):
    username: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    employee_name: str | None = Field(default=None, max_length=128)


@router.put("/{device_id}/assignment", summary="Assign the device to an employee account (admin)")
async def assign_device(
    device_id: str, body: AssignmentIn, container: ContainerDep, admin: Admin
) -> dict[str, Any]:
    if not container.twin.has_device(device_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown device")
    a = await container.assignments.assign(device_id, body.username, body.employee_name, admin.subject)
    await container.twin_state.on_applied(device_id)  # identity.owner changes in the twin
    return a.public()


@router.get("/{device_id}/state", summary="Current state of one device (in-memory projection, no DB)")
async def device_state(device_id: str, container: ContainerDep, principal: Reader) -> dict[str, Any]:
    twin = _require(container, principal, device_id)
    now = datetime.now(UTC)
    snapshot = container.twin.snapshot(device_id, now) or {}
    presence = container.presence.get(device_id)
    return {
        **snapshot,
        "summary": _row(container, twin.device),
        "presence": presence.to_dict(now) if presence else {"presence": "UNKNOWN"},
        "device_health": twin.device_health,
        "agent_health": twin.agent_health,
        "recent_events": list(twin.recent_events)[:50],
        "sequence": container.ingest.sequences.snapshot(device_id).get(device_id),
    }


@pipeline_router.get("/stats", summary="Telemetry pipeline observability (ingest, latency, sequences, WS)")
async def pipeline_stats(container: ContainerDep, principal: Staff) -> dict[str, Any]:
    require_platform_scope(principal)  # platform-wide ingest statistics (every organisation's devices)
    now = datetime.now(UTC)
    persister = container.persister
    drift_warn = container.settings.clock_drift_warn_s
    sequences = container.ingest.sequences.snapshot()
    return {
        "generated_at": now.isoformat(),
        "uptime_s": round(__import__("time").monotonic() - container.started_monotonic, 1),
        "ingest": {
            **container.ingest.stats(),
            "rate_limit": {
                "per_device_per_min": container.settings.ingest_rate_per_device_per_min,
                "burst": container.settings.ingest_rate_burst,
                "limited_total": container.agent_limiter.limited_total,
            },
            "dedupe_cache_size": len(container.deduper._seen),
        },
        "presence": {
            "summary": container.presence.summary(),
            "stale_after_s": container.presence.stale_after_s,
            "offline_after_s": container.presence.offline_after_s,
        },
        "sequences": sequences,
        "clock_drift_warnings": [
            d for d, s in sequences.items() if abs(float(str(s.get("clock_drift_s") or 0))) > drift_warn
        ],
        "persistence": {
            "mode": container.persistence_mode,
            "timescaledb": bool(container.db and container.db.timescale),
            "aggregate_5m": bool(container.db and container.db.aggregate_5m),
            "queue_depth": persister.depth,
            "written_total": persister.written,
            "last_write_at": persister.last_write_at.isoformat() if persister.last_write_at else None,
            "last_error": persister.last_error,
            "events_dropped": container.recorder.dropped,
            "events_failed": container.recorder.failures,
        },
        "retention": {
            "raw_days": container.settings.retention_days,
            "aggregate_days": container.settings.aggregate_retention_days,
            "event_days": container.settings.event_retention_days,
            "receipt_hours": container.settings.receipt_retention_hours,
        },
        "websocket": {
            **container.ws.subscription_stats(),
            "fanout_skipped_no_viewer": container.fanout_skipped,
        },
        "twin": container.twin_state.stats(),
    }
