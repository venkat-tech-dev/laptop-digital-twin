# ruff: noqa: E501  (inline test payloads)
"""Phase 8 - endpoint-side validation: signatures, device binding, expiry, local allowlist, parameter
injection, replay, idempotency, graceful-only application restart, dry run and key pinning."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.remediation.apps import AppProcess
from app.remediation.envelope import key_id
from app.remediation.executor import ActionExecutor, Hooks, parse_applications
from app.remediation.keys import KeyPin
from app.remediation.ledger import ExecutionLedger

DEVICE = "dev-1"


class Platform:
    """Signs envelopes exactly like the backend (canonical JSON, Ed25519)."""

    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        raw = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public = base64.b64encode(raw).decode()

    def envelope(self, action: str = "REFRESH_TELEMETRY", **over: Any) -> dict[str, Any]:
        now = datetime.now(UTC)
        env: dict[str, Any] = {
            "execution_id": secrets.token_hex(8),
            "remediation_id": "r1",
            "action_id": action,
            "action_version": 1,
            "device_id": DEVICE,
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=2)).isoformat(),
            "requested_by": "alice",
            "approved_by": "bob",
            "policy_version": 3,
            "parameters": {},
            "execution_timeout_s": 30,
            "dry_run": False,
            "nonce": secrets.token_hex(16),
            "key_id": key_id(self.public),
            "v": 1,
        }
        env.update(over)
        body = json.dumps(env, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        env["signature"] = base64.b64encode(self.key.sign(body)).decode()
        return env


class FakeApps:
    def __init__(self, closes: bool = True, relaunches: bool = True) -> None:
        self.procs = [AppProcess(100, r"C:\Program Files\Slack\slack.exe", 1)]
        self.closes, self.relaunches = closes, relaunches
        self.closed: list[list[int]] = []
        self.launched: list[str] = []
        self.killed = False  # there is no kill API at all; this stays False by construction

    def find(self, executables: tuple[str, ...]) -> list[AppProcess]:
        return [p for p in self.procs if Path(p.exe_path).name.lower() in executables]

    def close_gracefully(self, pids: list[int], timeout_s: float) -> bool:
        self.closed.append(pids)
        if self.closes:
            self.procs = []
        return self.closes

    def launch(self, exe_path: str) -> bool:
        self.launched.append(exe_path)
        if self.relaunches:
            self.procs = [AppProcess(200, exe_path, 1)]
        return True


def make(tmp_path: Path, platform: Platform, **kw: Any) -> tuple[ActionExecutor, dict[str, int]]:
    calls = {"refresh": 0, "rescan": 0, "reconnect": 0}

    def hook(name: str) -> Any:
        async def run() -> dict[str, Any]:
            calls[name] += 1
            return {"ok": True}

        return run

    ex = ActionExecutor(
        DEVICE,
        lambda: platform.public,
        ExecutionLedger(tmp_path / "actions.json"),
        Hooks(hook("refresh"), hook("rescan"), hook("reconnect"), kw.pop("run_mode", "console")),
        **kw,
    )
    return ex, calls


def run(ex: ActionExecutor, env: dict[str, Any]) -> list[tuple[str, Any]]:
    return asyncio.run(ex.handle(env))


def test_valid_low_risk_action_runs_and_reports(tmp_path: Path) -> None:
    p = Platform()
    ex, calls = make(tmp_path, p)
    out = run(ex, p.envelope())
    assert [r[1][0] for r in out] == ["accepted", "completed"]
    assert calls["refresh"] == 1


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda e: e.update(parameters={"x": "1"}), "invalid signature"),  # tampered after signing
        (lambda e: e.update(signature=base64.b64encode(b"0" * 64).decode()), "invalid signature"),
        (lambda e: e.update(extra="x"), "unexpected envelope fields"),
        (lambda e: e.pop("nonce"), "malformed envelope"),
    ],
)
def test_tampered_or_malformed_envelopes_are_rejected(tmp_path: Path, mutate: Any, reason: str) -> None:
    p = Platform()
    ex, calls = make(tmp_path, p)
    env = p.envelope()
    mutate(env)
    out = run(ex, env)
    assert out[0][1][0] == "rejected" and reason in out[0][1][1]
    assert calls["refresh"] == 0


def test_forged_by_another_key_is_rejected(tmp_path: Path) -> None:
    attacker, platform = Platform(), Platform()
    ex, calls = make(tmp_path, platform)
    out = run(ex, attacker.envelope())
    assert out[0][1] == ("rejected", "invalid signature", {}) and calls["refresh"] == 0


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"device_id": "dev-other"}, "another device"),
        ({"expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}, "expired"),
        (
            {
                "issued_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "expires_at": (datetime.now(UTC) + timedelta(hours=1, minutes=2)).isoformat(),
            },
            "future",
        ),
        ({"expires_at": (datetime.now(UTC) + timedelta(days=2)).isoformat()}, "too long"),
        ({"action_id": "RUN_POWERSHELL"}, "unsupported action"),
        ({"action_version": 9}, "unsupported action"),
        ({"v": 2}, "unsupported envelope version"),
    ],
)
def test_signed_but_invalid_envelopes_are_rejected(tmp_path: Path, over: dict[str, Any], reason: str) -> None:
    p = Platform()
    ex, calls = make(tmp_path, p)
    out = run(ex, p.envelope(**over))
    assert out[0][1][0] == "rejected" and reason in out[0][1][1], out
    assert calls == {"refresh": 0, "rescan": 0, "reconnect": 0}


def test_local_allowlist_cannot_be_widened_by_the_platform(tmp_path: Path) -> None:
    p = Platform()
    ex, _ = make(tmp_path, p, apps=FakeApps())  # default: only the three no-change actions
    out = run(ex, p.envelope("RESTART_KNOWN_APPLICATION", parameters={"application_id": "slack.desktop"}))
    assert out[0][1][0] == "rejected" and "local policy" in out[0][1][1]


@pytest.mark.parametrize(
    "params",
    [
        {"application_id": "C:\\Windows\\System32\\cmd.exe"},
        {"application_id": "slack.desktop", "path": "C:\\evil.exe"},
        {"application_id": "microsoft.teams"},  # valid id but not on this device's list
        {"application_id": ["slack.desktop"]},
        {},
    ],
)
def test_parameter_injection_is_rejected(tmp_path: Path, params: dict[str, Any]) -> None:
    p = Platform()
    apps = FakeApps()
    ex, _ = make(tmp_path, p, allowed_actions=("RESTART_KNOWN_APPLICATION",),
                 applications=parse_applications(["slack.desktop"]), apps=apps)  # fmt: skip
    out = run(ex, p.envelope("RESTART_KNOWN_APPLICATION", parameters=params))
    assert out[0][1][0] == "rejected"
    assert apps.closed == [] and apps.launched == []


def test_no_parameters_accepted_for_parameterless_actions(tmp_path: Path) -> None:
    p = Platform()
    ex, calls = make(tmp_path, p)
    out = run(ex, p.envelope(parameters={"cmd": "whoami"}))
    assert out[0][1][0] == "rejected" and calls["refresh"] == 0


def test_idempotency_same_execution_runs_once(tmp_path: Path) -> None:
    p = Platform()
    ex, calls = make(tmp_path, p)
    env = p.envelope()
    first = run(ex, env)
    again = run(ex, env)  # network retry / re-issued envelope with the same execution id
    assert calls["refresh"] == 1
    assert again == [(env["execution_id"], first[-1][1])]
    # also across an agent restart (ledger on disk)
    ex2, calls2 = make(tmp_path, p)
    assert run(ex2, env)[0][1][0] == "completed" and calls2["refresh"] == 0


def test_replayed_nonce_with_new_execution_is_rejected(tmp_path: Path) -> None:
    p = Platform()
    ex, calls = make(tmp_path, p)
    env = p.envelope()
    run(ex, env)
    replay = p.envelope(nonce=env["nonce"])  # new execution id, reused nonce
    out = run(ex, replay)
    assert out[0][1] == ("rejected", "replayed nonce", {}) and calls["refresh"] == 1


def test_interrupted_execution_is_not_retried(tmp_path: Path) -> None:
    p = Platform()
    env = p.envelope()
    ExecutionLedger(tmp_path / "actions.json").begin(env["execution_id"], env["nonce"], env["action_id"])
    ex, calls = make(tmp_path, p)
    out = run(ex, env)
    assert out[0][1][0] == "failed" and "not retried" in out[0][1][1] and calls["refresh"] == 0


def _restart(
    tmp_path: Path, apps: FakeApps, run_mode: str = "console", dry: bool = False
) -> list[tuple[str, Any]]:
    p = Platform()
    ex, _ = make(tmp_path, p, allowed_actions=("RESTART_KNOWN_APPLICATION",),
                 applications=parse_applications(["slack.desktop"]), apps=apps, run_mode=run_mode)  # fmt: skip
    return run(
        ex,
        p.envelope("RESTART_KNOWN_APPLICATION", parameters={"application_id": "slack.desktop"}, dry_run=dry),
    )


def test_restart_known_application_graceful_close_and_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    apps = FakeApps()
    out = _restart(tmp_path, apps)
    assert out[-1][1][0] == "completed" and out[-1][1][2]["running_after"] is True
    assert apps.closed == [[100]] and apps.launched == [r"C:\Program Files\Slack\slack.exe"]


def test_application_that_does_not_close_is_never_forced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    apps = FakeApps(closes=False)
    out = _restart(tmp_path, apps)
    assert out[-1][1][0] == "failed" and "not forced" in out[-1][1][1]
    assert apps.launched == [] and apps.killed is False


def test_restart_refused_in_service_session(tmp_path: Path) -> None:
    out = _restart(tmp_path, FakeApps(), run_mode="service")
    assert out[0][1][0] == "rejected" and "session" in out[0][1][1]


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    apps = FakeApps()
    out = _restart(tmp_path, apps, dry=True)
    assert out[-1][1][0] == "completed" and out[-1][1][2] == {"dry_run": True}
    assert "would ask slack.exe" in out[-1][1][1]
    assert apps.closed == [] and apps.launched == []


def test_no_pinned_key_refuses_everything(tmp_path: Path) -> None:
    p = Platform()
    ex = ActionExecutor(DEVICE, lambda: None, ExecutionLedger(tmp_path / "a.json"),
                        Hooks(None, None, None))  # type: ignore[arg-type]  # fmt: skip
    assert run(ex, p.envelope())[0][1][0] == "rejected"


def test_key_pinning_trust_on_first_use_never_repins(tmp_path: Path) -> None:
    a, b = Platform(), Platform()
    pin = KeyPin(tmp_path / "key.json")
    assert pin.current() is None
    assert pin.trust_on_first_use({"public_key": a.public}) is True
    assert (
        pin.trust_on_first_use({"public_key": b.public}) is False
    )  # a rotated / spoofed key is not accepted
    assert KeyPin(tmp_path / "key.json").current() == a.public
    assert KeyPin(tmp_path / "key.json", explicit=b.public).current() == b.public  # explicit pin wins
    assert KeyPin(tmp_path / "x.json").trust_on_first_use({"public_key": "not-base64!"}) is False


def test_parse_applications() -> None:
    apps = parse_applications(
        ["slack.desktop", "vendor.tool=tool.exe|tool-helper.exe", "bad id", "x.y=C:\\a.exe"]
    )
    assert apps == {"slack.desktop": ("slack.exe",), "vendor.tool": ("tool.exe", "tool-helper.exe")}
