from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BIORADAR_", extra="ignore")

    # Production identity/access is A01; a BOOT build must not masquerade as production.
    environment: Literal["development", "test"] = "development"
    database_url: SecretStr | None = None
    redis_url: SecretStr | None = None
    queue_prefix: str = "bioradar:dev"
    contract_dir: Path = Path("contracts")
    data_fabric_url: str | None = None
    data_fabric_research_url: str | None = None
    data_fabric_history_read_token: SecretStr | None = None
    data_fabric_graph_query_token: SecretStr | None = None
    data_fabric_graph_query_url: str | None = None

    biocheck_project_dir: Path | None = None
    mech_evidence_project_dir: Path | None = None
    biocheck_transport: Literal["local", "http"] = "local"
    biocheck_service_url: str | None = None
    biocheck_service_token: SecretStr | None = None
    mech_evidence_transport: Literal["local", "http"] = "local"
    mech_evidence_service_url: str | None = None
    mech_evidence_service_token: SecretStr | None = None
    professional_http_timeout_seconds: float = Field(default=30, gt=0, le=120)
    professional_http_max_bytes: int = Field(default=8388608, ge=4096, le=16777216)
    professional_max_steps: int = Field(default=24, ge=1, le=64)
    weknora_kdd_url: str | None = None
    weknora_kdd_token: SecretStr | None = None
    weknora_kdd_knowledge_base_id: str | None = Field(default=None, min_length=1, max_length=200)
    analysis_kdd_enabled: bool = False
    analysis_kdd_selector: Literal["information_gain_v1", "coverage_cost_baseline_v1"] = "information_gain_v1"
    monitor_kdd_directions: dict[str, Literal["research", "pipeline", "business", "public_health"]] = Field(
        default_factory=dict
    )
    data_fabric_source_instance: str = Field(default="datafabric-test", min_length=1, max_length=200)
    weknora_url: str | None = None
    weknora_api_key: SecretStr | None = None
    weknora_knowledge_base_ids: list[str] = Field(default_factory=list, max_length=8)

    # A local development-user credential, never an upstream/service API key.
    engine_dev_credential: SecretStr | None = None
    engine_workspace_id: str = "development"
    engine_source_allowlist: list[str] = Field(default_factory=lambda: ["fixture", "datafabric-test"])
    engine_source_id_allowlist: dict[str, list[str]] = Field(
        default_factory=lambda: {"fixture": ["fixture-source"], "datafabric-test": ["clinicaltrials"]}
    )
    engine_allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:15176",
            "http://localhost:15176",
            "http://127.0.0.1:18100",
            "http://localhost:18100",
        ]
    )
    engine_session_seconds: int = Field(default=1800, ge=60, le=3600)
    engine_job_timeout: int = Field(default=600, ge=30, le=3600)
    engine_max_snapshot_bytes: int = Field(default=8_000_000, ge=1024, le=32_000_000)
    engine_max_result_bytes: int = Field(default=16_000_000, ge=1024, le=64_000_000)
    engine_max_decision_bytes: int = Field(default=2_000_000, ge=1024, le=16_000_000)
    engine_max_records: int = Field(default=5000, ge=1, le=20000)
    engine_retry_limit: int = Field(default=3, ge=1, le=5)
    monitor_enabled: bool = False
    monitor_is_demo: bool = False
    monitor_poll_seconds: int = Field(default=30, ge=1, le=3600)
    monitor_batch_records: int = Field(default=50, ge=1, le=100)
    monitor_context_records: int = Field(default=2000, ge=10, le=20000)
    monitor_pending_runs: int = Field(default=2, ge=1, le=8)
    monitor_hourly_investigations: int = Field(default=6, ge=0, le=100)
    monitor_cooldown_minutes: int = Field(default=60, ge=1, le=1440)
    monitor_investigations_per_batch: int = Field(default=2, ge=1, le=5)

    # Server-only model access. These credentials never enter a browser request.
    semantic_llm_enabled: bool = False
    semantic_llm_provider: Literal["vllm", "deepseek"] = "vllm"
    semantic_llm_reasoning_effort: Literal["high", "max"] = "high"
    semantic_llm_base_url: str | None = None
    semantic_llm_api_key: SecretStr | None = None
    semantic_llm_model: str | None = Field(default=None, max_length=200)
    semantic_llm_model_revision: str | None = Field(default=None, max_length=200)
    semantic_llm_timeout_seconds: int = Field(default=45, ge=5, le=180)
    semantic_llm_max_input_bytes: int = Field(default=65_536, ge=1024, le=131_072)
    semantic_llm_max_output_tokens: int = Field(default=4096, ge=128, le=32_768)
    semantic_llm_enable_thinking: bool = False
    semantic_llm_temperature: float = Field(default=0, ge=0, le=2, allow_inf_nan=False)
    semantic_llm_top_p: float = Field(default=1, gt=0, le=1, allow_inf_nan=False)
    semantic_llm_top_k: int | None = Field(default=None, ge=1, le=1000)
    analysis_search_source_ids: list[str] = Field(default_factory=list, max_length=8)
    analysis_graph_source_ids: list[str] = Field(default_factory=lambda: ["primekg_graph"], max_length=8)
    analysis_manual_per_hour: int = Field(default=2, ge=0, le=100)
    analysis_auto_enabled: bool = False
    analysis_max_calls: int = Field(default=16, ge=1, le=32)
    analysis_max_tokens: int = Field(default=262144, ge=1, le=10_000_000)
    analysis_max_seconds: int = Field(default=1800, ge=1, le=1800)
    analysis_min_call_seconds: int = Field(default=60, ge=1, le=180)
    analysis_max_queries: int = Field(default=6, ge=0, le=32)
    analysis_max_tool_calls: int = Field(default=64, ge=1, le=256)
    analysis_query_policy: Literal["baseline", "query_planner"] = "baseline"
    query_planner_project_dir: Path | None = None
    analysis_query_policy_max_steps: int = Field(default=12, ge=1, le=256)

    @model_validator(mode="after")
    def flash_thinking_sampling(self):
        if (
            self.semantic_llm_provider == "deepseek"
            and self.semantic_llm_model == "deepseek-flash"
            and self.semantic_llm_enable_thinking
            and self.semantic_llm_top_p < 0.95
        ):
            raise ValueError("DeepSeek Flash thinking requires top_p between 0.95 and 1")
        return self

    @field_validator("semantic_llm_base_url")
    @classmethod
    def model_endpoint(cls, value: str | None) -> str | None:
        from urllib.parse import urlsplit

        if value in (None, ""):
            return None
        parts = urlsplit(value)
        parts.port  # Validate a configured numeric port before any request is attempted.
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or any(ord(char) <= 32 for char in value)
        ):
            raise ValueError("Use an explicit server model endpoint without embedded credentials")
        return value.rstrip("/")

    @field_validator("semantic_llm_api_key")
    @classmethod
    def empty_model_key(cls, value: SecretStr | None) -> SecretStr | None:
        return value if value and value.get_secret_value() else None

    @field_validator("semantic_llm_model", "semantic_llm_model_revision")
    @classmethod
    def model_identifier(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("analysis_search_source_ids", "analysis_graph_source_ids")
    @classmethod
    def analysis_sources(cls, value: list[str]) -> list[str]:
        import re

        if any(not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", source) for source in value):
            raise ValueError("Analysis search requires explicit source identifiers")
        return sorted(set(value))

    @field_validator("data_fabric_url", "data_fabric_research_url", "data_fabric_graph_query_url")
    @classmethod
    def local_fabric(cls, value: str | None) -> str | None:
        from urllib.parse import urlsplit

        if value in (None, ""):
            return None
        parts = urlsplit(value)
        parts.port
        if (
            parts.scheme not in {"http", "https"}
            or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parts.username is not None
            or parts.password is not None
            or any(ord(char) <= 32 for char in value)
            or parts.query
            or parts.fragment
            or parts.path not in {"", "/"}
        ):
            raise ValueError("History reader requires a configured loopback base URL")
        return value.rstrip("/")

    @field_validator("weknora_url", "weknora_kdd_url", "biocheck_service_url", "mech_evidence_service_url")
    @classmethod
    def configured_knowledge_endpoint(cls, value: str | None) -> str | None:
        from urllib.parse import urlsplit

        if value in (None, ""):
            return None
        parts = urlsplit(value)
        parts.port
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or parts.path not in {"", "/"}
            or any(ord(char) <= 32 for char in value)
        ):
            raise ValueError("Use an explicit server-configured WeKnora root URL without credentials")
        return value.rstrip("/")

    @field_validator(
        "data_fabric_history_read_token",
        "weknora_api_key",
        "weknora_kdd_token",
        "biocheck_service_token",
        "mech_evidence_service_token",
    )
    @classmethod
    def empty_history_token(cls, value: SecretStr | None) -> SecretStr | None:
        return value if value and value.get_secret_value() else None

    @field_validator("biocheck_project_dir", "mech_evidence_project_dir")
    @classmethod
    def installed_provider_directory(cls, value: Path | None) -> Path | None:
        if value is not None and (not value.is_absolute() or ".." in value.parts):
            raise ValueError("Shared provider source requires an absolute server-configured directory")
        return value

    @field_validator("weknora_knowledge_base_ids")
    @classmethod
    def configured_knowledge_bases(cls, value: list[str]) -> list[str]:
        import re

        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", item) for item in value):
            raise ValueError("WeKnora retrieval requires explicit knowledge base identifiers")
        return sorted(set(value))

    @field_validator("engine_dev_credential")
    @classmethod
    def development_credential(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 24:
            raise ValueError("Use a development-user credential of at least 24 characters")
        return value

    @field_validator("engine_workspace_id")
    @classmethod
    def single_workspace(cls, value: str) -> str:
        if not value or len(value) > 100 or not all(c.isalnum() or c in "-_" for c in value):
            raise ValueError("Use a bounded development workspace identifier")
        return value

    @field_validator("engine_source_allowlist")
    @classmethod
    def source_scope(cls, value: list[str]) -> list[str]:
        if not value or any(not item or len(item) > 200 for item in value):
            raise ValueError("Explicit source-instance permissions are required")
        return sorted(set(value))

    @field_validator("engine_source_id_allowlist")
    @classmethod
    def source_id_scope(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        if any(
            not source or not ids or any(not item or len(item) > 200 for item in ids)
            for source, ids in value.items()
        ):
            raise ValueError("Explicit source-ID permissions are required")
        return {source: sorted(set(ids)) for source, ids in value.items()}

    @field_validator("engine_allowed_origins")
    @classmethod
    def local_origins(cls, value: list[str]) -> list[str]:
        from urllib.parse import urlsplit

        for origin in value:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
            ):
                raise ValueError("Engine origins must be explicit loopback origins")
        return sorted(set(value))

    @field_validator("queue_prefix")
    @classmethod
    def isolated_queue(cls, value: str) -> str:
        if not value.startswith("bioradar:") or not value.strip(":"):
            raise ValueError("Use a bioradar: namespace, never an upstream extraction queue")
        return value

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: SecretStr | None) -> SecretStr | None:
        if value and not value.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("Use PostgreSQL with psycopg; SQLite is not a runtime substitute")
        return value
