"""Read-only integration probe for the existing Data-Fabric API.

This reports production-pipeline statistics. It is NOT data.batch, a verified
entity mapping, evidence adoption, or an intelligence signal. No write endpoints
or LLM extraction/review calls are exposed by this adapter.
"""

from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bioradar.api.errors import DomainError


class PipelineStage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: str
    label: str
    total: int = Field(ge=0, strict=True)
    by_status: dict[str, int]


class PipelineOverview(BaseModel):
    model_config = ConfigDict(extra="ignore")
    stages: list[PipelineStage]


class DataFabricReader:
    def __init__(self, client: httpx.Client):
        self.client = client

    def inspect_pipeline(self) -> dict:
        path = "/api/agent/pipeline/overview"
        try:
            response = self.client.get(path)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise DomainError("provider_timeout", "数据服务读取超时，未取得统计", 503, True) from exc
        except httpx.HTTPError as exc:
            raise DomainError("provider_unavailable", "数据服务读取失败，未取得统计", 503, True) from exc
        try:
            result = PipelineOverview.model_validate(response.json())
            if not result.stages:
                raise ValueError("missing stages")
        except (ValueError, ValidationError) as exc:
            raise DomainError("provider_protocol_error", "数据服务返回格式不符合统计接口", 502) from exc
        return {
            "observed_at": datetime.now(UTC).isoformat(),
            "scope": "pipeline_statistics_only",
            "source_path": path,
            "adoptable_as_evidence": False,
            "pipeline": result.model_dump(),
        }
