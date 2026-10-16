#!/usr/bin/env python3
"""Run: python scripts/validate_contracts.py after installing requirements-validation.txt."""
import copy
import argparse
import importlib.metadata
import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate_spec

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def read_json(name):
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def collect_refs(document):
    refs = []

    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value:
                reference = value["$ref"]
                assert reference.startswith("#/"), reference
                target = document
                for part in reference[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
                refs.append(reference)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document)
    return refs


def core_validator(document, schema_name):
    return Draft202012Validator(
        {**document, "$ref": f"#/components/schemas/{schema_name}"},
        format_checker=FormatChecker(),
    )


def validate_core():
    document = read_json("bioradar-core.openapi.json")
    examples = read_json("bioradar-core.examples.json")["examples"]
    refs = collect_refs(document)
    schemas = document["components"]["schemas"]
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)

    operation_ids = []
    for path, methods in document["paths"].items():
        for method, operation in methods.items():
            operation_ids.append(operation["operationId"])
            parameters = operation.get("parameters", [])
            path_names = {p["name"] for p in parameters if p["in"] == "path"}
            assert path_names == set(re.findall(r"{([^}]+)}", path)), path
            if method == "post":
                assert any(p["name"] == "Idempotency-Key" and p["required"] for p in parameters), path
    assert len(set(operation_ids)) == len(operation_ids)
    validate_spec(document)
    for example in examples:
        core_validator(document, example["schema"]).validate(example["value"])

    budget = next(e["value"]["budget"] for e in examples if e["schema"] == "CreateInvestigation")
    invalid_examples = [
        ("CreateInvestigation", {"mode": "new_topic", "question": "x", "trigger": "user", "priority": "routine", "budget": budget}),
        ("ImpactAction", {"decision": "maintained", "reason": "x", "expected_state": "resolved"}),
        ("SignalAction", {"action": "verify", "reason": "x", "expected_revision": 1}),
    ]
    for schema_name, value in invalid_examples:
        assert not core_validator(document, schema_name).is_valid(value), schema_name
    return {
        "operations": len(operation_ids),
        "schemas": len(schemas),
        "resolved_refs": len(refs),
        "validated_examples": len(examples),
        "rejected_negative_examples": len(invalid_examples),
        "checks": ["JSON语法", "Draft2020-12 Schema结构", "内部引用", "路径参数一致性", "操作ID唯一", "POST幂等头", "示例请求符合Schema和格式", "关键负例拒绝", "OpenAPI3.1专用规范验证(openapi-spec-validator)"],
    }


def validate_providers():
    schema = read_json("provider-exchange.schema.json")
    examples = read_json("provider-exchange.examples.json")["examples"]
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for example in examples:
        assert example["is_demo"] is True
        validator.validate(example)
    for capability in ["execution.status", "provider.request.status"]:
        example = copy.deepcopy(next(e for e in examples if e["capability"] == capability))
        example["request"]["arguments"] = {"original_call_id": "fictional-original-call"}
        if capability == "execution.status":
            example["request"]["arguments"]["execution_request_id"] = "fictional-exec"
        validator.validate(example)

    bad_credential = copy.deepcopy(examples[0])
    bad_credential["credential"] = "forbidden"
    bad_ref = copy.deepcopy(examples[0])
    bad_ref["request"]["input_refs"] = [{"provider": "x", "object_type": "doc", "object_id": "x"}]
    bad_payload = copy.deepcopy(next(e for e in examples if e["capability"] == "answer.compose"))
    bad_payload["result"]["payload"] = {"external_status": "completed"}
    for invalid in [bad_credential, bad_ref, bad_payload]:
        assert not validator.is_valid(invalid)
    return {
        "capabilities": len(schema["properties"]["capability"]["enum"]),
        "validated_examples": len(examples),
        "rejected_negative_examples": 3,
        "validated_original_call_id_queries": 2,
        "checks": ["Draft2020-12结构与正例格式", "拒绝额外credential", "拒绝无版本引用", "拒绝错能力payload"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=CONTRACTS / "validation-report.json")
    args = parser.parse_args()
    report = {
        "scope": "仅静态JSON Schema/引用/示例验证；不证明业务API、队列执行或上游接通",
        "version": "1.0.0",
        **validate_core(),
        "provider_exchange": validate_providers(),
        "validator_dependencies": {name: importlib.metadata.version(name) for name in ["jsonschema", "openapi-spec-validator"]},
        "limitations": ["本记录未实测业务任务恢复、权限传播、预算竞争、数据库备份、来源覆盖或CPUBrain执行"],
        "archive": "archive/v0.1/",
    }
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
