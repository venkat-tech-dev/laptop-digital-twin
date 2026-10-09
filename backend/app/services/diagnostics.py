"""Diagnostics bundle: a ZIP the operator downloads and shares manually (nothing is uploaded).

Contents: backend configuration summary (no secrets), device identity and geometry, per-component
availability with the reasons sensors are unavailable, provider failure counters, active/recent
anomalies, recent health transitions and system events, and agent configuration. Optional
anonymisation replaces the device ID with a hash and drops serial-like fields.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from typing import Any

from app.core.container import VERSION, Container

_SECRET_HINTS = ("key", "secret", "token", "password")


def _redact_settings(container: Container) -> dict[str, Any]:
    raw = container.settings.model_dump(mode="json")
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if any(h in k.lower() for h in _SECRET_HINTS) or k in ("database_url", "redis_url"):
            out[k] = "<set>" if v else "<empty>"
        else:
            out[k] = v
    return out


async def build_bundle(container: Container, anonymize: bool) -> tuple[bytes, str]:
    twin = container.twin.get()
    now = datetime.now(UTC)
    device_id = twin.device.device_id if twin else None
    alias = f"device-{hashlib.sha256(device_id.encode()).hexdigest()[:10]}" if device_id else None

    expose_serials = container.settings.expose_serial_numbers and not anonymize

    def strip_serials(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: strip_serials(v)
                for k, v in node.items()
                if expose_serials
                or ("serial" not in k.lower() and k.lower() not in ("uuid", "mac", "mac_address"))
            }
        if isinstance(node, list):
            return [strip_serials(v) for v in node]
        return node

    def scrub(obj: Any) -> Any:
        data = json.loads(json.dumps(obj, default=str))
        if anonymize and device_id is not None:
            data = json.loads(json.dumps(data).replace(device_id, alias or "device"))
        return strip_serials(data)

    files: dict[str, Any] = {
        "README.txt": (
            "Laptop Digital Twin diagnostics bundle\n"
            f"Created {now.isoformat()} by backend {VERSION}.\n"
            "Contains configuration (secrets redacted), component availability and unavailability reasons,\n"
            "anomalies, health transitions and system events. No passwords, keys, documents, browser data\n"
            "or keystrokes are collected by this product.\n"
            + ("Device identifiers are anonymised.\n" if anonymize else "")
        ),
        "backend.json": {
            "version": VERSION,
            "generated_at": now.isoformat(),
            "persistence": container.persistence_mode,
            "timescaledb": bool(container.db and container.db.timescale),
            "redis": "configured" if container.redis is not None else "disabled",
            "websocket_clients": container.ws.count,
            "persisted_samples": container.persister.written,
            "persist_queue_depth": container.persister.depth,
            "settings": _redact_settings(container),
            "sync": {**(await container.admin.sync_config()), **container.sync.status()},
            "agent_config": await container.admin.agent_config(),
        },
    }
    if twin is not None:
        d = twin.device
        files["device.json"] = scrub(
            {
                "device_id": d.device_id,
                "manufacturer": d.manufacturer,
                "model": d.model,
                "model_number": d.model_number,
                "os": d.os_name,
                "agent_version": d.agent_version,
                "status": d.status.value,
                "last_seen": d.last_seen.isoformat() if d.last_seen else None,
                "geometry": container.geometry.resolve(d.manufacturer, d.model, d.inventory),
                "inventory": d.inventory,
            }
        )
        components = {}
        for cid, comp in twin.components.items():
            components[cid] = {
                "type": comp.component_type.value,
                "name": comp.name,
                "availability": comp.availability.value,
                "health": comp.health.status.value,
                "unavailable": {
                    k: r.reason for k, r in comp.telemetry.items() if not r.available and r.reason
                },
            }
        files["components.json"] = scrub(components)
        files["anomalies.json"] = scrub(
            {
                "active": [a.to_dict() for a in twin.anomalies.active.values()],
                "recent_resolved": [a.to_dict() for a in list(twin.anomalies.recent_resolved)[:100]],
            }
        )
        try:
            health = await container.event_repo.list_health_events(d.device_id, 200)
            events = await container.event_repo.list_system_events(d.device_id, 200)
            files["health_events.json"] = scrub(
                [h.__dict__ if hasattr(h, "__dict__") else _slots(h) for h in health]
            )
            files["system_events.json"] = scrub([_slots(e) for e in events])
        except Exception as exc:  # repository unavailable
            files["events_error.txt"] = f"Event history unavailable: {exc}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            payload = content if isinstance(content, str) else json.dumps(content, indent=2, default=str)
            zf.writestr(name, payload)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return buf.getvalue(), f"ldt-diagnostics-{stamp}.zip"


def _slots(obj: Any) -> dict[str, Any]:
    names = getattr(obj, "__slots__", None) or getattr(obj, "__dataclass_fields__", {})
    return {n: getattr(obj, n) for n in names}
