"""Run one bounded investigation without starting the Web application."""

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bioradar.adapters.data_fabric_research import DataFabricResearchReader
from bioradar.api.config import Settings
from bioradar.data.database import create_database
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.engine.service import EngineService
from bioradar.research.investigations.digging import run_bounded_investigation


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=4000)
    seed_terms: list[str] = Field(min_length=1, max_length=3)


def local_settings(path: Path) -> Settings:
    values = dict(
        line.split("=", 1) for line in path.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    # Use only BioRadar's own local database; do not inherit an upstream DB URL.
    return Settings(
        environment="development",
        database_url="postgresql+psycopg://bioradar:"
        + quote(values["POSTGRES_PASSWORD"], safe="")
        + f"@127.0.0.1:{int(values.get('POSTGRES_PORT', '15436'))}/bioradar",
        engine_dev_credential=values["ENGINE_DEV_CREDENTIAL"],
        engine_workspace_id=values.get("ENGINE_WORKSPACE_ID", "development"),
        engine_source_allowlist=json.loads(values["ENGINE_SOURCE_ALLOWLIST"]),
        engine_source_id_allowlist=json.loads(values["ENGINE_SOURCE_ID_ALLOWLIST"]),
    )


def save_report(path, report):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("ops/.env"))
    args = parser.parse_args()
    root_url = os.environ.get("BIORADAR_DATA_FABRIC_URL")
    if not root_url:
        parser.error("请用 BIORADAR_DATA_FABRIC_URL 配置已核实的数据服务地址。")
    try:
        if args.request.stat().st_size > 16_000:
            raise ValueError
        request = ResearchRequest.model_validate_json(args.request.read_text())
        request.seed_terms = [term.strip() for term in request.seed_terms]
        if any(not 2 <= len(term) <= 120 for term in request.seed_terms):
            raise ValueError
        settings = local_settings(args.config)
    except (OSError, ValueError, KeyError, ValidationError):
        parser.error("研究参数或本地配置不完整；请检查参数例和 ops/.env，未执行调查。")
    actor = DevelopmentAccess(settings).actor()
    if "datafabric-test" not in actor.source_allowlist:
        parser.error("当前研究空间没有 datafabric-test 来源许可。")
    receipt = {
        "execution_id": str(uuid4()), "started_at": datetime.now(UTC).isoformat(),
        "status": "started", "request": request.model_dump(),
        "scope": "bounded_current_registry_investigation_not_professional_report",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        parser.error("输出文件已存在。先核对上次调查；本命令不会覆盖回执或自动重跑。")
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    database = None
    try:
        database = create_database(settings)
        with httpx.Client(base_url=root_url, trust_env=False) as client:
            reader = DataFabricResearchReader(
                client, source_instance="datafabric-test", authorization_ref=actor.grant_ref,
                allowed_source_sites=set(settings.engine_source_id_allowlist.get("datafabric-test", [])),
            )
            result = run_bounded_investigation(
                EngineService(database, settings), actor, reader,
                title=request.title, question=request.question, seed_terms=tuple(request.seed_terms),
                execution_id=receipt["execution_id"],
            )
        receipt.update(status=result["completion_status"], result=result)
    except Exception:
        receipt.update(
            status="write_or_execution_outcome_unconfirmed",
            next_step="先按 execution_id 核对本项目保存记录；不要自动重发，也不要删除原回执。",
        )
        save_report(args.output, receipt)
        print("调查未完整结束，回执已保留。先核对已有记录，不自动重跑。", file=sys.stderr)
        return 1
    finally:
        if database is not None:
            database.dispose()
    receipt["finished_at"] = datetime.now(UTC).isoformat()
    save_report(args.output, receipt)
    print(json.dumps({
        "status": receipt["status"], "investigation_id": result["investigation_id"],
        "report": str(args.output), "queries": len(result["queries"]),
        "adopted_materials": len(result["material_entry_ids"]),
    }, ensure_ascii=False))
    return 0 if receipt["status"] == "completed_with_gaps" else 1


if __name__ == "__main__":
    raise SystemExit(main())
