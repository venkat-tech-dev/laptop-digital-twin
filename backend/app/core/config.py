from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class AuthMode(StrEnum):
    NONE = "none"  # local-only development; refused when APP_ENV=production
    API_KEY = "api_key"  # X-API-Key header (or Bearer JWT)
    JWT = "jwt"  # Bearer JWT only (exchange an API key at /api/v1/auth/token)
    ACCOUNTS = "accounts"  # user accounts with roles; Bearer JWT from /api/v1/auth/login


_INSECURE_KEYS = {"", "change-me", "changeme", "dev-agent-key"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    app_env: AppEnv = Field(default=AppEnv.DEVELOPMENT, alias="APP_ENV")
    api_host: str = Field(default="127.0.0.1", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    database_url: str = Field(default="", alias="DATABASE_URL")
    redis_url: str = Field(default="", alias="REDIS_URL")
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default=["http://localhost:5173", "http://127.0.0.1:5173"], alias="CORS_ORIGINS"
    )

    auth_mode: AuthMode = Field(default=AuthMode.NONE, alias="AUTH_MODE")
    api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list, alias="API_KEYS")
    jwt_secret: str = Field(default="", alias="JWT_SECRET")
    jwt_ttl_minutes: int = Field(default=60, ge=1, alias="JWT_TTL_MINUTES")
    #: previous signing secrets still accepted for verification during rotation (comma separated)
    jwt_secret_previous: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="JWT_SECRET_PREVIOUS"
    )
    # ---- Phase 9: tenancy, identity, governance
    secrets_dir: str = Field(default="", alias="SECRETS_DIR")
    allow_legacy_enrollment: bool = Field(default=True, alias="ALLOW_LEGACY_ENROLLMENT")
    oidc_allow_http: bool = Field(default=False, alias="OIDC_ALLOW_HTTP")
    public_base_url: str = Field(default="", alias="PUBLIC_BASE_URL")
    hsts_enabled: bool = Field(default=False, alias="HSTS_ENABLED")
    feature_oidc: bool = Field(default=True, alias="FEATURE_OIDC")
    feature_saml: bool = Field(default=True, alias="FEATURE_SAML")
    feature_scim: bool = Field(default=True, alias="FEATURE_SCIM")
    agent_ingest_key: str = Field(default="", alias="AGENT_INGEST_KEY")
    # Accounts mode: if set, creating the first administrator requires this one-time token.
    setup_token: str = Field(default="", alias="SETUP_TOKEN")
    # Outbound sync (off unless an operator configures a target). Key comes from the environment only.
    sync_target_key: str = Field(default="", alias="SYNC_TARGET_KEY")
    rate_limit_per_minute: int = Field(default=600, ge=10, alias="RATE_LIMIT_PER_MINUTE")

    telemetry_interval_ms: int = Field(default=1000, alias="TELEMETRY_INTERVAL_MS")
    hardware_sensor_provider: str = Field(default="auto", alias="HARDWARE_SENSOR_PROVIDER")
    degraded_after_s: float = Field(default=3.0, alias="TWIN_DEGRADED_AFTER_S")
    stale_after_s: float = Field(default=10.0, alias="TWIN_STALE_AFTER_S")
    offline_after_s: float = Field(default=30.0, alias="TWIN_OFFLINE_AFTER_S")

    persist_sample_interval_s: float = Field(default=5.0, ge=1.0, alias="PERSIST_SAMPLE_INTERVAL_S")
    persist_flush_interval_s: float = Field(default=2.0, ge=0.5, alias="PERSIST_FLUSH_INTERVAL_S")
    persist_queue_max: int = Field(default=50_000, ge=1000, alias="PERSIST_QUEUE_MAX")
    persist_exclude_prefixes: Annotated[list[str], NoDecode] = Field(
        default=["gpu.engine_usage_percent", "agent.collect_duration_ms"], alias="PERSIST_EXCLUDE_PREFIXES"
    )
    # Phase 10: one active instance (PostgreSQL advisory lock); others wait as hot standby
    leader_election: bool = Field(default=True, alias="LEADER_ELECTION")
    # Phase 10: connection pool and query guardrails
    db_pool_size: int = Field(default=5, ge=1, le=100, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=5, ge=0, le=100, alias="DB_MAX_OVERFLOW")
    db_pool_timeout_s: float = Field(default=10.0, gt=0, le=120, alias="DB_POOL_TIMEOUT_S")
    db_statement_timeout_ms: int = Field(default=60_000, ge=0, le=3_600_000, alias="DB_STATEMENT_TIMEOUT_MS")
    retention_days: int = Field(default=30, ge=1, alias="RETENTION_DAYS")  # raw samples
    aggregate_retention_days: int = Field(default=365, ge=1, alias="AGGREGATE_RETENTION_DAYS")
    event_retention_days: int = Field(default=365, ge=1, alias="EVENT_RETENTION_DAYS")
    receipt_retention_hours: int = Field(default=168, ge=1, alias="RECEIPT_RETENTION_HOURS")

    # ---- Phase-2 ingestion pipeline
    ingest_max_body_bytes: int = Field(default=5_000_000, ge=10_000, alias="INGEST_MAX_BODY_BYTES")
    ingest_max_decompressed_bytes: int = Field(
        default=40_000_000, ge=10_000, alias="INGEST_MAX_DECOMPRESSED_BYTES"
    )
    ingest_rate_per_device_per_min: int = Field(default=240, ge=1, alias="INGEST_RATE_PER_DEVICE_PER_MIN")
    ingest_rate_burst: int = Field(default=60, ge=1, alias="INGEST_RATE_BURST")
    ingest_max_future_skew_s: float = Field(default=86_400.0, ge=60, alias="INGEST_MAX_FUTURE_SKEW_S")
    clock_drift_warn_s: float = Field(default=120.0, ge=1, alias="CLOCK_DRIFT_WARN_S")
    # The shared enrollment key may only register devices; ingest needs the per-device token.
    allow_enrollment_key_ingest: bool = Field(default=False, alias="ALLOW_ENROLLMENT_KEY_INGEST")
    presence_stale_after_s: float = Field(default=90.0, ge=5, alias="PRESENCE_STALE_AFTER_S")
    presence_offline_after_s: float = Field(default=300.0, ge=10, alias="PRESENCE_OFFLINE_AFTER_S")
    primary_device_id: str = Field(default="", alias="PRIMARY_DEVICE_ID")
    ws_max_subscriptions: int = Field(default=100, ge=1, alias="WS_MAX_SUBSCRIPTIONS")
    hot_state_interval_s: float = Field(default=10.0, ge=1.0, alias="HOT_STATE_INTERVAL_S")
    device_token_cache_s: float = Field(default=300.0, ge=0, alias="DEVICE_TOKEN_CACHE_S")

    # ---- Phase-3 digital twin state engine (see app/domain/twin/rules.py)
    # Freshness = per-metric collection interval (sent by the agent) + agent batching wait + grace.
    twin_publish_wait_s: float = Field(default=5.0, ge=0, alias="TWIN_PUBLISH_WAIT_S")
    twin_freshness_grace_s: float = Field(default=5.0, ge=0, alias="TWIN_FRESHNESS_GRACE_S")
    twin_default_interval_s: float = Field(default=5.0, gt=0, alias="TWIN_DEFAULT_INTERVAL_S")
    twin_summary_interval_s: float = Field(default=10.0, ge=1, alias="TWIN_SUMMARY_INTERVAL_S")
    # Enterprise policy: whether process names appear in the twin (top CPU / memory process).
    twin_show_process_names: bool = Field(default=True, alias="TWIN_SHOW_PROCESS_NAMES")
    # ---- Phase 4: intelligent anomaly detection (defaults: app/domain/anomalies/policy.py)
    anomaly_intelligence_enabled: bool = Field(default=True, alias="ANOMALY_INTELLIGENCE_ENABLED")
    anomaly_eval_interval_s: float = Field(default=10.0, ge=2, le=300, alias="ANOMALY_EVAL_INTERVAL_S")
    anomaly_retrain_interval_s: float = Field(default=3600.0, ge=300, alias="ANOMALY_RETRAIN_INTERVAL_S")
    anomaly_baseline_history_days: float = Field(
        default=7.0, ge=1, le=60, alias="ANOMALY_BASELINE_HISTORY_DAYS"
    )
    anomaly_z_trigger: float = Field(default=3.5, ge=2, le=10, alias="ANOMALY_Z_TRIGGER")
    anomaly_persistence_s: float = Field(default=180.0, ge=0, le=3600, alias="ANOMALY_PERSISTENCE_S")
    anomaly_cooldown_s: float = Field(default=900.0, ge=0, le=86400, alias="ANOMALY_COOLDOWN_S")
    anomaly_iforest_enabled: bool = Field(default=True, alias="ANOMALY_IFOREST_ENABLED")
    anomaly_model_retrain_interval_s: float = Field(
        default=86400.0, ge=3600, alias="ANOMALY_MODEL_RETRAIN_INTERVAL_S"
    )
    #: process names next to an anomaly ("associated with"); off = counts only, never names
    anomaly_process_context: bool = Field(default=True, alias="ANOMALY_PROCESS_CONTEXT")
    # ---- Phase 5: forecasting (defaults per metric: app/domain/prediction/targets.py)
    prediction_enabled: bool = Field(default=True, alias="PREDICTION_ENABLED")
    # ---- Phase 6: alerting & notifications (secrets only from the environment, never stored/returned)
    alerting_enabled: bool = Field(default=True, alias="ALERTING_ENABLED")
    notification_retention_days: int = Field(default=90, ge=1, le=3650, alias="NOTIFICATION_RETENTION_DAYS")
    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int = Field(default=587, ge=1, le=65535, alias="SMTP_PORT")
    smtp_username: str | None = Field(default=None, alias="SMTP_USERNAME")
    smtp_password: str | None = Field(default=None, alias="SMTP_PASSWORD", repr=False)
    smtp_from: str | None = Field(default=None, alias="SMTP_FROM")
    smtp_starttls: bool = Field(default=True, alias="SMTP_STARTTLS")
    webhook_signing_secret: str | None = Field(default=None, alias="WEBHOOK_SIGNING_SECRET", repr=False)
    webhook_allow_http: bool = Field(default=False, alias="WEBHOOK_ALLOW_HTTP")
    webhook_allow_private: bool = Field(default=False, alias="WEBHOOK_ALLOW_PRIVATE")
    # ---- Phase 7: diagnosis & explainability. Rules-only unless a local model is configured; the model
    # endpoint must be loopback (LOCAL_ONLY) or a private network (CENTRAL_PRIVATE) - never a public API.
    diagnosis_enabled: bool = Field(default=True, alias="DIAGNOSIS_ENABLED")
    diagnosis_mode: str = Field(
        default="LOCAL_ONLY", pattern=r"^(LOCAL_ONLY|CENTRAL_PRIVATE|DISABLED)$", alias="DIAGNOSIS_MODE"
    )
    diagnosis_llm_url: str = Field(default="http://localhost:11434", alias="DIAGNOSIS_LLM_URL")
    #: preferred local models, best first (e.g. "qwen2.5:3b,qwen2.5:1.5b,gemma2:2b"); empty = rules only
    diagnosis_models: Annotated[list[str], NoDecode] = Field(default_factory=list, alias="DIAGNOSIS_MODELS")
    diagnosis_timeout_s: float = Field(default=90.0, ge=5, le=600, alias="DIAGNOSIS_TIMEOUT_S")
    #: how long Ollama keeps the model in memory after a diagnosis (Ollama duration: "0", "30s", "5m")
    diagnosis_model_keep_alive: str = Field(
        default="5m", pattern=r"^(0|[1-9][0-9]{0,4}[smh])$", alias="DIAGNOSIS_MODEL_KEEP_ALIVE"
    )
    diagnosis_queue_max: int = Field(default=50, ge=1, le=10_000, alias="DIAGNOSIS_QUEUE_MAX")
    diagnosis_concurrency: int = Field(default=1, ge=1, le=8, alias="DIAGNOSIS_CONCURRENCY")
    diagnosis_auto_min_severity: str = Field(
        default="HIGH", pattern=r"^(LOW|MEDIUM|HIGH|CRITICAL|OFF)$", alias="DIAGNOSIS_AUTO_MIN_SEVERITY"
    )
    diagnosis_ttl_s: int = Field(default=6 * 3600, ge=300, le=30 * 86400, alias="DIAGNOSIS_TTL_S")
    diagnosis_cooldown_s: int = Field(default=300, ge=0, le=86400, alias="DIAGNOSIS_COOLDOWN_S")
    #: skip model inference (rules only) while the device hosting the model is above this memory use
    diagnosis_memory_gate_percent: float = Field(
        default=90.0, ge=50, le=100, alias="DIAGNOSIS_MEMORY_GATE_PERCENT"
    )
    #: the endpoint that also runs the local model (resource gate); empty = assume every diagnosed device
    diagnosis_model_host_device_id: str | None = Field(default=None, alias="DIAGNOSIS_MODEL_HOST_DEVICE_ID")
    diagnosis_retention_days: int = Field(default=90, ge=1, le=3650, alias="DIAGNOSIS_RETENTION_DAYS")
    # ---- Phase 8: remediation. Without a signing key nothing can be dispatched (fail closed).
    remediation_enabled: bool = Field(default=True, alias="REMEDIATION_ENABLED")
    #: base64 Ed25519 private key (32 bytes); secret, environment only, never logged or returned
    remediation_signing_key: str = Field(default="", alias="REMEDIATION_SIGNING_KEY", repr=False)
    #: global kill switch that the API cannot turn off
    remediation_kill_switch: bool = Field(default=False, alias="REMEDIATION_KILL_SWITCH")
    # Overload protection: concurrent ingest requests per backend process before answering 503 +
    # Retry-After (agents keep the data queued and back off), and the persistence-queue fill ratio
    # above which backlog (replay) uploads are deferred so live data keeps flowing.
    ingest_max_inflight: int = Field(default=32, ge=1, alias="INGEST_MAX_INFLIGHT")
    ingest_shed_replay_above: float = Field(default=0.8, gt=0, le=1, alias="INGEST_SHED_REPLAY_ABOVE")

    ws_heartbeat_s: float = Field(default=5.0, alias="WS_HEARTBEAT_S")
    ws_send_queue_max: int = Field(default=256, alias="WS_SEND_QUEUE_MAX")
    models_dir: Path = Field(default=Path(__file__).resolve().parents[3] / "models", alias="MODELS_DIR")
    expose_serial_numbers: bool = Field(default=False, alias="EXPOSE_SERIAL_NUMBERS")
    otel_exporter_otlp_endpoint: str = Field(default="", alias="OTEL_EXPORTER_OTLP_ENDPOINT")

    @field_validator(
        "cors_origins",
        "api_keys",
        "persist_exclude_prefixes",
        "diagnosis_models",
        "jwt_secret_previous",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.app_env is AppEnv.PRODUCTION:
            if self.auth_mode is AuthMode.NONE:
                raise ValueError("AUTH_MODE=none is not allowed when APP_ENV=production")
            if self.agent_ingest_key in _INSECURE_KEYS or len(self.agent_ingest_key) < 24:
                raise ValueError("AGENT_INGEST_KEY must be a random value of >= 24 characters in production")
            if self.auth_mode is AuthMode.JWT and len(self.jwt_secret) < 32:
                raise ValueError("JWT_SECRET must be >= 32 characters when AUTH_MODE=jwt")
            if "*" in self.cors_origins:
                raise ValueError("CORS_ORIGINS='*' is not allowed in production")
        if self.auth_mode is AuthMode.ACCOUNTS:
            if len(self.jwt_secret) < 32:
                raise ValueError("JWT_SECRET must be >= 32 characters when AUTH_MODE=accounts")
        elif self.auth_mode is not AuthMode.NONE and not self.api_keys:
            raise ValueError("API_KEYS must contain at least one key when AUTH_MODE is not 'none'")
        return self

    @property
    def persistence_enabled(self) -> bool:
        return bool(self.database_url)

    @property
    def redis_enabled(self) -> bool:
        return bool(self.redis_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
