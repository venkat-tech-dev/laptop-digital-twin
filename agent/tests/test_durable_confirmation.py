"""Phase 10 (agent 1.8): accepted batches are kept until the backend confirms they are durable."""

from __future__ import annotations

from pathlib import Path

from app.transport.client import BulkSummary
from tests.test_store_sync import Clock, FakeClient, manager, payload, store


def test_store_keeps_sent_batches_until_confirmed_and_resends_unknown(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, clock)
    for b in ("a", "b", "c"):
        s.put(b, payload(b))
    rows = s.due(10)
    s.mark_sent([r.row_id for r in rows], resend_after_s=600)
    assert s.due(10) == [] and s.stats().depth == 3  # kept, not re-sent
    assert s.unconfirmed(10) == ["a", "b", "c"]
    assert s.confirm(["a", "x"]) == 1  # unknown ids are ignored
    assert s.resend(["b"]) == 1  # the backend lost it: due again at once
    assert [r.batch_id for r in s.due(10)] == ["b"] and s.unconfirmed(10) == ["c"]
    clock.t += 601  # never confirmed: sent again eventually (the backend deduplicates)
    assert {r.batch_id for r in s.due(10)} == {"b", "c"}


def test_sent_batches_are_evicted_before_unsent_ones_under_pressure(tmp_path: Path) -> None:
    clock = Clock()
    s = store(tmp_path, clock, max_batches=4)
    for b in ("s1", "s2"):
        s.put(b, payload(b))
    s.mark_sent([r.row_id for r in s.due(10)], 600)
    for b in ("u1", "u2", "u3"):
        s.put(b, payload(b))  # 5 > 4: one batch must go
    assert s.unconfirmed(10) == ["s2"] and {r.batch_id for r in s.due(10)} == {"u1", "u2", "u3"}


class ConfirmingClient(FakeClient):
    def __init__(self, confirm: bool) -> None:
        super().__init__()
        self.last_summary = BulkSummary(1, 0, 0, None, durable_confirmation=confirm)


async def test_sync_keeps_batches_only_when_the_backend_offers_confirmation(tmp_path: Path) -> None:
    clock = Clock()
    s, c = store(tmp_path, clock), ConfirmingClient(confirm=True)
    sync = manager(s, c, clock)
    s.put("x", payload("x"))
    assert await sync.sync_once() == 1
    assert s.unconfirmed(10) == ["x"] and s.stats().depth == 1
    old = store(tmp_path / "old", clock)
    oc = ConfirmingClient(confirm=False)  # an older backend: delete on 202, as before
    old.put("y", payload("y"))
    await manager(old, oc, clock).sync_once()
    assert old.stats().depth == 0 and old.unconfirmed(10) == []
