"""Read-only diagnosis dry run against the real database (nothing is written).

    docker compose exec -T backend python /app/scripts_dry_run.py <device_id> [alert_id]

Builds a context from the persisted last hour of 1-minute history plus the given alert, runs the
deterministic pipeline (evidence -> hypotheses -> confidence -> explanation) and prints it. Use it to
check what the platform would explain for a real device without an API session.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.domain.diagnosis import composer
from app.domain.diagnosis.context import DiagnosticContext, SeriesSummary, clean
from app.infrastructure.database.engine import Database
from app.repositories.alerting import SqlAlertRepository
from app.repositories.sql import SqlTelemetryRepository
from app.services.diagnosis import SERIES, WINDOW_MARGIN, signal_for

METRIC_KEYS = {
    "cpu": "cpu.usage_percent",
    "memory": "memory.usage_percent",
    "disk_active": "disk.active_time_percent",
    "net_latency": "network.gateway_latency_ms",
    "battery": "battery.charge_percent",
}


async def main(device_id: str, alert_id: str | None) -> None:
    s = get_settings()
    db = Database(s.database_url)
    try:
        tele = SqlTelemetryRepository(db)
        now = datetime.now(UTC)
        metrics = {m.key for m in await tele.list_metrics(device_id)}
        keys = {sig: next((k for k in sorted(metrics) if k.startswith(prefix)), None) for sig, prefix in METRIC_KEYS.items()}
        keys["temperature"] = next((k for k in sorted(metrics) if k.startswith("thermal.zone_temperature_c")), None)
        hist = await tele.history(device_id, [k for k in keys.values() if k], now - timedelta(hours=1), now, 60)
        trigger: dict[str, object] = {"kind": "manual", "id": None, "title": "Dry run", "severity": None, "signal": None}
        security: dict[str, object] = {}
        if alert_id:
            a = await SqlAlertRepository(db).get_alert(alert_id)
            if a is not None:
                md = a.metadata or {}
                trigger = {"kind": "alert", "id": a.alert_id, "alert_id": a.alert_id, "title": clean(a.title, 160),
                           "severity": a.severity, "metric": md.get("metric"),
                           "signal": "security" if a.category == "security" else signal_for(md.get("metric")),
                           "started_at": a.first_detected_at.isoformat()}  # fmt: skip
                if a.category == "security":
                    security = {"posture": "WARNING", "findings": [clean(x, 120) for x in a.summary.split(";")]}
        ctx = DiagnosticContext(device_id=device_id, generated_at=now.timestamp(), trigger=trigger, security=security)
        for sig, key in keys.items():
            if not key:
                continue
            _f, label, unit, warn, crit, direction = SERIES[sig]
            pts = [(p.time.timestamp(), round(p.avg, 2)) for p in hist.get(key, [])]
            high = None
            if direction == "up" and len(pts) >= 6:
                early = sorted(v for _, v in pts[: len(pts) // 2])
                high = min(early[len(early) // 2] + WINDOW_MARGIN.get(sig, 10.0), float(warn or 1e9))
            ctx.series[sig] = SeriesSummary(sig, label, unit, pts, None, high, warn, crit, direction, "window")
        ctx.data_quality = {"coverage": round(sum(len(x.points) for x in ctx.series.values()) / max(1, 60 * len(ctx.series)), 2)}
        d = composer.finish(ctx, composer.prepare(ctx), None, "rules")
        print(json.dumps({
            "status": d.status.value, "type": d.diagnosis_type.value, "confidence": d.confidence,
            "level": d.confidence_level, "summary": d.summary, "likely_cause": d.likely_cause,
            "hypotheses": [(h.code, h.confidence_level, h.confidence) for h in d.hypotheses],
            "evidence": [f"{e.evidence_id} {e.type.value}: {e.statement}" for e in d.evidence],
            "investigate": d.explanation.get("investigate"), "missing": d.explanation.get("missing"),
            "series_points": {k: len(v.points) for k, v in ctx.series.items()},
        }, indent=2, ensure_ascii=False))  # fmt: skip
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
