"""All runtime settings. Environment variables use the BIORADAR_ prefix."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BIORADAR_", env_file=None, extra="ignore")

    environment: str = "development"
    database_url: str = "postgresql+psycopg://bioradar:bioradar@127.0.0.1:15436/bioradar"
    redis_url: str = "redis://127.0.0.1:16379/0"
    queue_name: str = "bioradar"
    dev_credential: str = ""  # the first admin's credential; empty and no users means nobody can sign in
    session_secret: str = ""  # signs session cookies; empty: the dev credential does
    workspace_id: str = "default"

    # Data-Fabric history API (徐子雯). Records are read at a fixed version and cutoff.
    fabric_url: str = "http://127.0.0.1:18511"
    fabric_read_token: str = ""
    fabric_admin_token: str = ""
    sources: list[str] = ["pubmed", "clinicaltrials", "who_don", "europe_pmc"]
    demo_sources: list[str] = ["external_archive"]
    # Live monitoring reads only these (default: sources). Bulk backfills such as legacy_documents
    # stay searchable for analyses but must not flood triage with old documents.
    monitor_sources: list[str] | None = None

    # WeKnora semantic search; empty URL disables it.
    weknora_url: str = ""
    weknora_token: str = ""
    weknora_knowledge_base_id: str = ""
    weknora_source_instance: str = ""  # only hits this Fabric published (the bridge's source_instance)
    graph_lookup: bool = False  # look question objects up in Tang's reviewed graph through Fabric (T-20)

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
    max_steps: int = 30
    max_reads: int = 20
    max_llm_calls: int = 60
    read_characters: int = 12000

    # Monitoring and topics.
    monitor_enabled: bool = False
    monitor_seconds: int = 30
    monitor_max_records: int = 100  # per live round (about 10 triage calls); the rest waits in the feed
    auto_analysis: bool = False
    auto_analyses_per_hour: int = 6  # live monitoring only
    # Soft ceiling on today's automatic work (live triage + topic analyses); manual questions are exempt.
    # In-flight work may overshoot by one triage batch and the analyses already running.
    daily_auto_tokens: int = 3_000_000
    quiet_hours: int = 72  # no new record for this long, and no collection status: "待核"
    topic_cool_days: int = 7
    topic_dormant_days: int = 21


@lru_cache
def settings() -> Settings:
    return Settings()
