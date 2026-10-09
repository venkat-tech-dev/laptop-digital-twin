"""Phase 7 benchmark: cost of the deterministic diagnosis pipeline and the size of a model prompt.

    python scripts/diagnosis_bench.py [--n 2000]

Measures, per diagnosis: evidence collection + rule hypotheses + platform confidence + explanation
(the part that always runs), validation of a typical model answer, and the prompt size sent to a local
model (characters and an approximate token count at ~4 characters/token). Synthetic contexts with the
real shapes (60 one-minute points per signal, processes, anomalies); no database, no model.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.domain.diagnosis import composer, prompts, validation  # noqa: E402
from app.domain.diagnosis.context import DiagnosticContext, ProcessFigure, SeriesSummary  # noqa: E402


def context(rng: random.Random, now: float) -> DiagnosticContext:
    def s(key: str, label: str, unit: str, base: float, spike: float, high: float) -> SeriesSummary:
        onset = rng.randint(20, 55)
        pts = [(now - 60 * (59 - i), base + rng.uniform(-3, 3) + (spike if i >= onset else 0)) for i in range(60)]
        return SeriesSummary(key, label, unit, pts, base, high, 90, 95)

    trig = rng.choice(["cpu", "memory", "temperature"])
    return DiagnosticContext(
        device_id=f"dev-{rng.randint(1, 999)}",
        generated_at=now,
        trigger={"kind": "alert", "id": "a1", "title": "High usage", "severity": "HIGH", "signal": trig,
                 "observed": 90.0, "expected": 30.0},  # fmt: skip
        series={
            "cpu": s("cpu", "CPU", "%", 20, rng.choice([0, 65]), 45),
            "memory": s("memory", "Memory", "%", 60, rng.choice([0, 30]), 80),
            "temperature": s("temperature", "Temperature", "°C", 55, rng.choice([0, 30]), 70),
            "disk_active": s("disk_active", "Drive activity", "%", 5, rng.choice([0, 80]), 40),
        },
        anomalies=[{"id": "an1", "signal": trig, "title": "Unusual usage", "level": "HIGH", "observed": 90,
                    "expected": 30, "confidence": 0.8, "status": "active"}],  # fmt: skip
        processes=[ProcessFigure(f"proc{i}.exe", rng.uniform(0, 60), rng.uniform(0, 10), rng.uniform(50, 3000),
                                 rng.uniform(50, 2500)) for i in range(5)],  # fmt: skip
        process_window={"snapshots": 30},
        security={"posture": "WARNING", "findings": ["Secure Boot: off"]},
        data_quality={"coverage": 1.0},
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000)
    args = ap.parse_args()
    rng = random.Random(7)
    now = time.time()
    ctxs = [context(rng, now) for _ in range(args.n)]
    rule_ms, val_ms, prompt_chars = [], [], []
    for c in ctxs:
        t0 = time.perf_counter()
        d = composer.prepare(c)
        composer.finish(c, d, None, "rules")
        rule_ms.append((time.perf_counter() - t0) * 1000)
        msgs = prompts.messages(c, d.index, d.hypotheses)
        prompt_chars.append(sum(len(m["content"]) for m in msgs))
        e = d.evidence[0]
        answer = json.dumps({"summary": "x", "ranking": [h.code for h in d.hypotheses],
                             "claims": [{"text": e.statement, "evidence_ids": [e.evidence_id]}] * 4,
                             "investigate": [{"text": "Check whether this workload is expected", "evidence_ids": []}]})  # fmt: skip
        t0 = time.perf_counter()
        validation.validate(answer, d.index, [h.code for h in d.hypotheses], [p.name for p in c.processes])
        val_ms.append((time.perf_counter() - t0) * 1000)
    tracemalloc.start()  # separate pass: tracing slows Python down ~50x, so it is not timed
    for c in ctxs[:200]:
        composer.finish(c, composer.prepare(c), None, "rules")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    def q(xs: list[float], p: float) -> float:
        return sorted(xs)[min(len(xs) - 1, int(p * len(xs)))]

    out = {
        "diagnoses": args.n,
        "rules_pipeline_ms": {"p50": round(statistics.median(rule_ms), 2), "p95": round(q(rule_ms, 0.95), 2),
                              "max": round(max(rule_ms), 2)},  # fmt: skip
        "validation_ms": {"p50": round(statistics.median(val_ms), 3), "p95": round(q(val_ms, 0.95), 3)},
        "throughput_per_s_one_core": round(1000 / statistics.mean(rule_ms)),
        "prompt_chars": {"p50": int(statistics.median(prompt_chars)), "max": max(prompt_chars)},
        "prompt_tokens_approx": {"p50": int(statistics.median(prompt_chars) / 4), "max": int(max(prompt_chars) / 4)},
        "peak_python_memory_mb_per_200": round(peak / 2**20, 1),
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
