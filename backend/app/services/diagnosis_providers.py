"""Diagnosis reasoning providers (Phase 7).

    DiagnosisModel.diagnose(context, evidence, hypotheses) -> ModelResult | None

* ``RuleBasedProvider``  deterministic only (returns no model output; the rule pipeline stands alone)
* ``OllamaProvider``     a local model over Ollama's HTTP API (``/api/tags`` health, ``/api/chat`` with
                         ``format: json``); no model name is hard-coded - candidates come from settings
                         and the first *installed* one that fits the available memory is used
* ``MockProvider``       canned output for tests

Endpoint policy (checked when the provider is built, and again before every request):
    LOCAL_ONLY       loopback or the Docker host alias only (data never leaves this machine)
    CENTRAL_PRIVATE  loopback or private (RFC 1918 / ULA) addresses only
    DISABLED         no model at all
Public endpoints are always refused: nothing is sent to an external AI API.
Models only produce text that is validated afterwards; they have no tools and cannot act.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog

from app.domain.diagnosis import prompts
from app.domain.diagnosis.context import DiagnosticContext
from app.domain.diagnosis.evidence import EvidenceIndex
from app.domain.diagnosis.models import Hypothesis

log = structlog.get_logger("diagnosis.providers")

LOCAL_HOST_ALIASES = {"localhost", "host.docker.internal", "ollama"}  # "ollama": a sidecar container
FAILURES_BEFORE_COOLDOWN = 3
COOLDOWN_S = 300.0


class EndpointNotAllowedError(ValueError):
    pass


class ModelUnavailableError(RuntimeError):
    pass


@dataclass
class ModelResult:
    raw: str
    model: str
    model_version: str
    latency_ms: float
    prompt_tokens: int | None = None
    output_tokens: int | None = None


def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    return [ipaddress.ip_address(i[4][0]) for i in infos]


def check_endpoint(url: str, mode: str) -> None:
    """Raise unless ``url`` is allowed for ``mode`` (never a public address)."""
    if mode == "DISABLED":
        raise EndpointNotAllowedError("diagnosis model is disabled")
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise EndpointNotAllowedError("model endpoint must be an http(s) URL")
    if u.username or u.password:
        raise EndpointNotAllowedError("credentials in the model URL are not allowed")
    host = u.hostname.lower()
    addrs = _resolve(host)
    if mode == "LOCAL_ONLY":
        if host in LOCAL_HOST_ALIASES:
            return
        if addrs and all(a.is_loopback for a in addrs):
            return
        raise EndpointNotAllowedError("LOCAL_ONLY allows only a loopback model endpoint")
    if mode == "CENTRAL_PRIVATE":
        if host in LOCAL_HOST_ALIASES:
            return
        if addrs and all((a.is_private or a.is_loopback) and not a.is_link_local for a in addrs):
            return
        raise EndpointNotAllowedError("CENTRAL_PRIVATE allows only private-network model endpoints")
    raise EndpointNotAllowedError(f"unknown diagnosis mode {mode}")


def host_memory_available_mb() -> float | None:
    """Available memory where this backend runs (Linux /proc, else psutil when installed)."""
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    try:
        import psutil  # type: ignore[import-untyped]

        return float(psutil.virtual_memory().available) / 1024**2
    except Exception:
        return None


class DiagnosisModel(ABC):
    name = "abstract"

    @abstractmethod
    async def diagnose(
        self, ctx: DiagnosticContext, ix: EvidenceIndex, hyps: list[Hypothesis]
    ) -> ModelResult | None: ...

    async def health(self) -> dict[str, Any]:
        return {"provider": self.name, "available": True}

    @property
    def uses_model(self) -> bool:
        return False


class RuleBasedProvider(DiagnosisModel):
    name = "rules"

    async def diagnose(
        self, ctx: DiagnosticContext, ix: EvidenceIndex, hyps: list[Hypothesis]
    ) -> ModelResult | None:
        return None


class MockProvider(DiagnosisModel):
    """Test double: returns ``output`` (str or dict), raises ``error``, or sleeps ``delay_s``."""

    name = "mock"

    def __init__(self, output: Any = None, error: Exception | None = None, delay_s: float = 0.0) -> None:
        self.output = output
        self.error = error
        self.delay_s = delay_s
        self.calls: list[list[dict[str, str]]] = []

    @property
    def uses_model(self) -> bool:
        return True

    async def diagnose(
        self, ctx: DiagnosticContext, ix: EvidenceIndex, hyps: list[Hypothesis]
    ) -> ModelResult | None:
        import asyncio
        import json

        self.calls.append(prompts.messages(ctx, ix, hyps))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        out = self.output if isinstance(self.output, str) else json.dumps(self.output or {})
        return ModelResult(out, "mock", "1", 1.0)


@dataclass
class _Health:
    checked_at: float = 0.0
    installed: dict[str, int] = field(default_factory=dict)  # model name -> size bytes
    reachable: bool = False
    error: str | None = None
    failures: int = 0
    cooldown_until: float = 0.0
    last_latency_ms: float | None = None


class OllamaProvider(DiagnosisModel):
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        candidates: list[str],
        mode: str,
        timeout_s: float = 90.0,
        transport: httpx.AsyncBaseTransport | None = None,
        memory_probe: Any = host_memory_available_mb,
        keep_alive: str = "5m",
    ) -> None:
        check_endpoint(base_url, mode)
        self.base_url = base_url.rstrip("/")
        self.candidates = [c for c in candidates if c]
        self.mode = mode
        self.timeout_s = timeout_s
        self._transport = transport
        self._memory = memory_probe
        self.keep_alive = keep_alive
        self._h = _Health()

    def set_memory_probe(self, probe: Any) -> None:
        """Where the model really runs decides: e.g. Ollama on the Windows host while this backend runs in a
        container, whose /proc/meminfo describes the container VM, not the host."""
        self._memory = probe

    @property
    def uses_model(self) -> bool:
        return True

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, transport=self._transport, follow_redirects=False
        )

    async def refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._h.checked_at < 60:
            return
        self._h.checked_at = now
        try:
            check_endpoint(self.base_url, self.mode)
            async with self._client(5.0) as c:
                r = await c.get("/api/tags")
                r.raise_for_status()
                models = r.json().get("models") or []
            self._h.installed = {str(m.get("name")): int(m.get("size") or 0) for m in models if m.get("name")}
            self._h.reachable, self._h.error = True, None
        except Exception as exc:
            self._h.reachable, self._h.error = False, f"{type(exc).__name__}: {str(exc)[:120]}"

    def select_model(self) -> tuple[str | None, str]:
        """First configured candidate that is installed and fits the available memory."""
        if not self.candidates:
            return None, "no local model configured"
        if not self._h.reachable:
            return None, "model server not reachable"
        avail = self._memory()
        for name in self.candidates:
            size = self._h.installed.get(name)
            if size is None and ":" not in name:
                size = self._h.installed.get(name + ":latest")
            if size is None:
                continue
            need_mb = size / 1024**2 * 1.2 + 512
            if avail is not None and avail < need_mb:
                continue
            return name, "selected"
        if any(c in self._h.installed for c in self.candidates):
            return None, "not enough free memory for any configured model"
        return None, "none of the configured models is installed"

    async def health(self) -> dict[str, Any]:
        await self.refresh()
        model, why = self.select_model()
        cooling = time.monotonic() < self._h.cooldown_until
        return {
            "provider": self.name,
            "endpoint_mode": self.mode,
            "reachable": self._h.reachable,
            "error": self._h.error,
            "installed": sorted(self._h.installed),
            "candidates": self.candidates,
            "selected_model": model,
            "selection": why,
            "available": model is not None and not cooling,
            "cooldown_remaining_s": round(max(0.0, self._h.cooldown_until - time.monotonic())),
            "consecutive_failures": self._h.failures,
            "last_latency_ms": self._h.last_latency_ms,
        }

    async def diagnose(
        self, ctx: DiagnosticContext, ix: EvidenceIndex, hyps: list[Hypothesis]
    ) -> ModelResult | None:
        if time.monotonic() < self._h.cooldown_until:
            raise ModelUnavailableError("model in cooldown after repeated failures")
        await self.refresh()
        model, why = self.select_model()
        if model is None:
            raise ModelUnavailableError(why)
        check_endpoint(self.base_url, self.mode)
        body = {
            "model": model,
            "messages": prompts.messages(ctx, ix, hyps),
            "format": "json",
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0.1, "num_ctx": 4096, "num_predict": 700},
        }
        started = time.perf_counter()
        try:
            async with self._client(self.timeout_s) as c:
                r = await c.post("/api/chat", json=body)
                r.raise_for_status()
                data = r.json()
        except Exception as exc:
            self._failed()
            raise ModelUnavailableError(f"{type(exc).__name__}: {str(exc)[:160]}") from exc
        latency = (time.perf_counter() - started) * 1000
        self._h.failures = 0
        self._h.last_latency_ms = round(latency)
        content = ((data.get("message") or {}).get("content")) or ""
        return ModelResult(
            content,
            model,
            str(data.get("model") or model),
            latency,
            data.get("prompt_eval_count"),
            data.get("eval_count"),
        )

    def _failed(self) -> None:
        self._h.failures += 1
        if self._h.failures >= FAILURES_BEFORE_COOLDOWN:
            self._h.cooldown_until = time.monotonic() + COOLDOWN_S
            log.warning("diagnosis_model_cooldown", failures=self._h.failures, cooldown_s=COOLDOWN_S)

    def record_timeout(self) -> None:
        self._failed()


def build_provider(settings: Any) -> DiagnosisModel:
    if settings.diagnosis_mode == "DISABLED" or not settings.diagnosis_models:
        return RuleBasedProvider()
    try:
        return OllamaProvider(
            settings.diagnosis_llm_url,
            list(settings.diagnosis_models),
            settings.diagnosis_mode,
            settings.diagnosis_timeout_s,
            keep_alive=settings.diagnosis_model_keep_alive,
        )
    except EndpointNotAllowedError as exc:
        log.error("diagnosis_model_endpoint_refused", reason=str(exc))
        return RuleBasedProvider()
