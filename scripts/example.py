"""Create a clearly labelled teaching investigation through the running application."""

import json
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]


def submit(client: httpx.Client, path: str, body: dict) -> dict:
    response = client.post(path, json=body, headers={"Idempotency-Key": str(uuid4())})
    response.raise_for_status()
    return response.json()


def append(client: httpx.Client, investigation: dict, entry: dict) -> dict:
    return submit(
        client,
        f"/api/engine/investigations/{investigation['id']}/entries",
        {**entry, "expected_revision": investigation["revision"]},
    )


def main() -> None:
    config_path = ROOT / "ops/.env"
    if not config_path.exists():
        raise SystemExit("请先运行 make init 和 make dev。")
    config = dict(
        line.split("=", 1)
        for line in config_path.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    credential = config.get("ENGINE_DEV_CREDENTIAL")
    if not credential:
        raise SystemExit("请先配置本地研究用户。")
    snapshot = json.loads((ROOT / "contracts/examples/engine-fixture.snapshot.json").read_text())
    updated = json.loads((ROOT / "contracts/examples/engine-fixture-v2.snapshot.json").read_text())
    policy = json.loads((ROOT / "contracts/examples/engine-fixture.policy.json").read_text())
    if any(value.get("is_demo") is not True or value.get("source_instance") != "fixture"
           for value in (snapshot, updated)):
        raise SystemExit("这个例子只接受明确标记的教学资料。")

    with httpx.Client(
        base_url=f"http://127.0.0.1:{int(config.get('API_PORT', '18100'))}",
        headers={"Authorization": "Bearer " + credential},
        timeout=30,
        trust_env=False,
    ) as client:
        adopted = submit(client, "/api/engine/snapshots", snapshot)
        second = submit(client, "/api/engine/snapshots", updated)
        response = client.get(
            "/api/engine/snapshot-comparisons",
            params={"before_id": adopted["id"], "after_id": second["id"]},
        )
        response.raise_for_status()
        comparison = response.json()
        if comparison["counts"] != {"added": 1, "content_updated": 1, "same": 14, "not_seen": 1}:
            raise SystemExit("资料更新例子的结果不符合预期，请检查所用版本。")
        run = submit(client, "/api/engine/runs", {"snapshot_id": adopted["id"], "policy": policy})
        # Submission returns a summary, including when an earlier completed run is reused.
        response = client.get(f"/api/engine/runs/{run['id']}")
        response.raise_for_status()
        run = response.json()
        deadline = time.monotonic() + 120
        while run["state"] in {"queued", "running"}:
            if time.monotonic() >= deadline:
                raise SystemExit("后台计算尚未完成，请检查 API 和 Worker，再打开已有运行记录。")
            time.sleep(1)
            response = client.get(f"/api/engine/runs/{run['id']}")
            response.raise_for_status()
            run = response.json()
        if run["state"] not in {"completed", "partial"} or not run.get("result"):
            raise SystemExit("教学资料计算未完成，未创建调查。请在雷达页面查看原因。")
        candidate = next(
            (
                item
                for item in run["result"]["candidates"]
                if item["method_id"] == "entity_combinations" and item["title"] == "规范实体组合：K"
            ),
            None,
        )
        if candidate is None:
            raise SystemExit("当前计算没有产生例子使用的线索，未编造替代结果。")

        investigation = submit(
            client,
            "/api/engine/investigations",
            {
                "title": "教学示例：技术 K 的材料应怎样继续核查",
                "question": "这些材料是否足以支持继续研究技术 K？还需要查哪些反面材料和适用条件？",
                "questions": ["确认材料之间是否独立", "查找反面结果", "核实适用条件"],
                "is_demo": True,
                "origin": {
                    "run_id": run["id"],
                    "result_hash": run["result"]["result_hash"],
                    "candidate_key": candidate["candidate_key"],
                },
            },
        )

        records = {record["record_ref"]["object_id"]: record for record in snapshot["records"]}
        basis = []
        # Classification is explicitly supplied by this teaching script, not by an expert model.
        for record_id in ("current-0", "current-1", "current-2", "baseline"):
            record = records[record_id]
            investigation = append(
                client,
                investigation,
                {
                    "kind": "material",
                    "snapshot_id": adopted["id"],
                    "record_ref": record["record_ref"],
                    "quote": record.get("normalized_text"),
                    "stance": "support" if record_id == "baseline" else "context",
                    "text": "教学分类，用于演示资料采用；不是专业证据评价。",
                },
            )
            basis.append(investigation["entries"][-1]["id"])

        investigation = append(
            client,
            investigation,
            {
                "kind": "judgment",
                "text": "教学判断第一版：材料提供了继续核查的线索，尚不能据此确认效果或建议投入。",
                "conditions": "仍需检查独立性、反面材料和研究条件。",
                "basis_entry_ids": basis.copy(),
                "assessment": "provisional",
            },
        )
        first_judgment = investigation["entries"][-1]
        record = records["negative"]
        investigation = append(
            client,
            investigation,
            {
                "kind": "material",
                "snapshot_id": adopted["id"],
                "record_ref": record["record_ref"],
                "quote": record.get("normalized_text"),
                "stance": "oppose",
                "text": "教学反面材料，要求重新检查先前判断的适用范围。",
            },
        )
        if not investigation["needs_review"]:
            raise SystemExit("新材料后的待复查提示不符合预期，请检查调查实现。")
        basis.append(investigation["entries"][-1]["id"])
        investigation = append(
            client,
            investigation,
            {
                "kind": "judgment",
                "text": "教学判断第二版：补入反面材料后保留分歧，目前仍无法作出专业结论。",
                "conditions": "继续核对各材料的实验条件，并由研究人员审阅。",
                "basis_entry_ids": basis,
                "assessment": "unable_to_determine",
            },
        )
        preserved = next(item for item in investigation["entries"] if item["id"] == first_judgment["id"])
        if preserved != first_judgment or investigation["needs_review"]:
            raise SystemExit("判断历史或当前复查状态不符合预期。")
        if investigation["professional_status"] != "not_reviewed" or not investigation["is_demo"]:
            raise SystemExit("教学记录的专业状态不符合预期。")

    url = f"http://127.0.0.1:{int(config.get('WEB_PORT', '15176'))}/investigations?id={investigation['id']}"
    report = {
        "is_demo": True,
        "run_id": run["id"],
        "investigation_id": investigation["id"],
        "url": url,
        "materials": len(basis),
        "judgments": 2,
        "snapshot_comparison": comparison["counts"],
        "snapshot_ids": [adopted["id"], second["id"]],
        "old_judgment_preserved": True,
        "new_material_triggered_review": True,
        "professional_status": investigation["professional_status"],
        "retrieval_status": investigation["retrieval_status"],
    }
    output = ROOT / "artifacts/example.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("教学例子已保存：5 条材料、2 次研究人员判断；尚未专业核验。")
    print("两批资料比较：新增 1 条、内容变更 1 条、内容相同 14 条、本批未出现 1 条。")
    print(url)


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        # Do not include request headers or response bodies, which may contain private data.
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else "连接失败"
        raise SystemExit(f"例子未完成（{status}），请确认本地服务已启动并使用当前代码。") from None
