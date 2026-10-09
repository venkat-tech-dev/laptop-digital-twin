"""Per-device rows shared by the organization console, compliance and fleet intelligence (Phase 9/10).

Moved out of ``api/v1/org.py`` so that services (fleet intelligence) do not import the API layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domain.governance import compliance as comp


def device_facts(container: Any, device_id: str, creds: dict[str, Any]) -> comp.DeviceFacts:
    t = container.tenancy
    rec = t.registry.get(device_id)
    twin = container.twin.get(device_id)
    doc = container.twin_state.engine.docs.get(device_id)
    state = doc.state if doc is not None else {}
    p = container.presence.get(device_id)
    hb = (p.heartbeat or {}) if p else {}
    last = p.last_batch_at if p else None
    cred = creds.get(device_id)
    now = datetime.now(UTC)
    if rec is not None and rec.enrollment_id == "legacy" and cred is None:
        valid: bool | None = None
    else:
        valid = cred is not None and not cred.revoked and (cred.expires_at is None or cred.expires_at > now)
    return comp.DeviceFacts(
        device_id,
        rec.lifecycle.value if rec else "UNKNOWN",
        rec is not None,
        valid,
        hb.get("agent_version") or (twin.device.agent_version if twin else None),
        twin.device.os_name if twin else None,
        (now - last).total_seconds() if last else None,
        comp.controls_from_state(state),
    )


def device_row(container: Any, device_id: str, creds: dict[str, Any]) -> dict[str, Any]:
    t = container.tenancy
    rec = t.registry.get(device_id)
    twin = container.twin.get(device_id)
    doc = container.twin_state.engine.docs.get(device_id)
    state = doc.state if doc is not None else {}
    org = rec.org_id if rec else None
    facts = device_facts(container, device_id, creds)
    result = comp.evaluate(
        facts,
        container.policies.effective("agent", org, device_id)[0],
        container.policies.effective("compliance", org, device_id)[0],
    )
    presence = container.presence.presence_of(device_id)
    lifecycle = rec.lifecycle.value if rec else "UNKNOWN"
    display = "STALE" if lifecycle == "ACTIVE" and presence in ("STALE", "OFFLINE") else lifecycle
    return {
        "device_id": device_id,
        "lifecycle": lifecycle,
        "lifecycle_display": display,
        "presence": presence,
        "health": (state.get("health") or {}).get("state"),
        "model": twin.device.model if twin else None,
        "os": facts.os_name,
        "agent_version": facts.agent_version,
        "groups": sorted(t.device_groups.get(device_id, set())),
        "group_names": sorted(
            t.groups[g].name for g in t.device_groups.get(device_id, set()) if g in t.groups
        ),
        "enrolled_at": rec.enrolled_at.isoformat() if rec and rec.enrolled_at else None,
        "enrollment": "legacy key" if rec and rec.enrollment_id == "legacy" else "token" if rec else None,
        "compliance": result["status"],
        "compliance_reasons": result["reasons"][:5],
        "security_posture": state.get("security.posture"),
        "last_seen": twin.device.last_seen.isoformat() if twin and twin.device.last_seen else None,
    }


async def load_credentials(container: Any) -> dict[str, Any]:
    try:
        return {c.device_id: c for c in await container.device_auth.list()}
    except Exception:
        return {}
