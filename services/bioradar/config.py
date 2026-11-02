"""All runtime settings. Environment variables use the BIORADAR_ prefix."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BIORADAR_", env_file=None, extra="ignore")

    environment: str = "development"
    database_url: str = "postgresql+psycopg://bioradar:bioradar@127.0.0.1:15436/bioradar"
    redis_url: str = "redis://127.0.0.1:16379/0"
    queue_name: str = "bioradar"
    dev_credential: str = "dev"
    workspace_id: str = "default"

    # Data-Fabric history API (徐子雯). Records are read at a fixed version and cutoff.
    fabric_url: str = "http://127.0.0.1:18511"
    fabric_read_token: str = ""
    fabric_admin_token: str = ""
    sources: list[str] = ["pubmed", "clinicaltrials", "who_don", "europe_pmc"]
    demo_sources: list[str] = ["external_archive"]

    # WeKnora semantic search; empty URL disables it.
    weknora_url: str = ""
    weknora_token: str = ""
    weknora_knowledge_base_id: str = ""

    # Professional services. Empty URL means the step is skipped and reported as not run.
    planner_url: str = ""
    planner_token: str = ""
    biocheck_url: str = ""
    biocheck_token: str = ""
    mechevidence_url: str = ""
    mechevidence_token: str = ""
    service_timeout_seconds: float = 900

    # The radar's own model calls (planning, reading, calculation, monitoring triage).
    llm_provider: str = "deepseek"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 180

    # Per-analysis limits.
    max_steps: int = 24
    max_reads: int = 16
    max_llm_calls: int = 60
    read_characters: int = 12000

    # Monitoring and topics.
    monitor_enabled: bool = False
    monitor_seconds: int = 30
    auto_analysis: bool = False
    topic_cool_days: int = 7
    topic_dormant_days: int = 21


@lru_cache
def settings() -> Settings:
    return Settings()
