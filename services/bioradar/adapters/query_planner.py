"""Load the installed query method; Radar owns tools, models and durable state."""

import ast
import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from bioradar.api.errors import DomainError

SCHEMA_VERSION = "query-planner.external.v1"
_DECISIONS = {"continue", "complete", "insufficient"}


def _source(settings):
    directory = getattr(settings, "query_planner_project_dir", None)
    if directory is None:
        return None
    root = Path(directory)
    try:
        entry = root / "radar_policy.py"
        data = entry.read_bytes()
        tree = ast.parse(data)
        if not any(isinstance(node, ast.FunctionDef) and node.name == "decide" for node in tree.body):
            return None
        methods = sorted((root / "src").rglob("*.py"))
        if not methods:
            return None
        digest = hashlib.sha256(data)
        for path in methods:
            digest.update(str(path.relative_to(root / "src")).encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    except (OSError, UnicodeError, SyntaxError):
        return None
    return entry, digest.hexdigest()


def provider_status(settings):
    source = _source(settings)
    return {
        "provider": "query_planner",
        "configured": getattr(settings, "query_planner_project_dir", None) is not None,
        "available": source is not None,
        "status": "ready" if source else "unavailable",
        "version": source[1] if source else None,
        "error_code": None if source else "query_planner_not_available",
        "training_status": "heuristic_untrained",
        "qualification": "本地方法可读取；实际执行、取证覆盖和训练状态以本轮决策回执为准。",
    }


def load_query_planner(settings):
    """Load by private file namespace without importing the upstream demo runner."""
    source = _source(settings)
    if source is None:
        raise DomainError("query_planner_not_available", "查询策略方法未配置或源码不可用", 422)
    entry, version = source
    name = "_bioradar_query_planner_" + version
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, entry)
        if spec is None or spec.loader is None:
            raise DomainError("query_planner_not_available", "查询策略方法无法受控加载", 422)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(name, None)
            raise DomainError("query_planner_load_failed", "查询策略依赖或方法加载失败", 502) from exc
    if (
        getattr(module, "SCHEMA_VERSION", None) != SCHEMA_VERSION
        or getattr(module, "POLICY_VERSION", None) != version
        or not callable(getattr(module, "decide", None))
    ):
        raise DomainError("query_policy_version_changed", "查询方法版本或输入契约已变化", 409)
    return module, version


def validate_decision(request, result, *, expected_version=None):
    """A selector may return one original candidate; it cannot create tool arguments."""
    try:
        json.dumps(result, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError, RecursionError):
        raise DomainError("query_policy_output_invalid", "查询策略输出不是有限JSON对象", 502) from None
    if (
        not isinstance(result, dict)
        or result.get("schema_version") != request.get("schema_version", SCHEMA_VERSION)
        or result.get("decision") not in _DECISIONS
        or "action" not in result
        or not isinstance(result.get("reason"), str)
        or not result["reason"].strip()
        or not isinstance(result.get("policy"), dict)
        or not isinstance(result.get("state_summary"), dict)
    ):
        raise DomainError("query_policy_output_invalid", "查询策略输出不符合固定契约", 502)
    policy = result["policy"]
    policy_keys = ("version", "selector", "training_status")
    if request.get("schema_version") != "query-planner.external.v2":
        policy_keys += ("stop",)
    if any(not isinstance(policy.get(key), str) or not policy[key] for key in policy_keys):
        raise DomainError("query_policy_output_invalid", "查询策略缺少实际版本与训练状态", 502)
    if expected_version is not None and policy["version"] != expected_version:
        raise DomainError("query_policy_version_changed", "查询策略返回版本与本轮固定版本不同", 409)
    action = result.get("action")
    if result["decision"] == "continue":
        if not isinstance(action, dict) or not any(
            json.dumps(action, sort_keys=True, allow_nan=False)
            == json.dumps(item, sort_keys=True, allow_nan=False)
            for item in request.get("candidates", [])
        ):
            raise DomainError("query_policy_action_invalid", "查询策略选择了当前候选以外的动作", 502)
        from bioradar.research.investigations.query_policy import semantic_key

        key = semantic_key(action["kind"], action["arguments"])
        history = request.get("history", [])
        if any(entry.get("status") in {"prepared", "sent", "unknown", "unconfirmed"} for entry in history):
            raise DomainError("query_policy_action_pending", "查询执行尚待确认，未允许发送新动作", 409)
        if any(semantic_key(entry["kind"], entry["arguments"]) == key for entry in history):
            raise DomainError("query_policy_action_repeated", "查询策略重复选择已尝试的同义动作", 409)
        costs = action.get("estimated_cost", {})
        for dimension, remaining in request.get("budget", {}).get("remaining", {}).items():
            cost = costs.get(dimension)
            if type(cost) is not int or cost < 0 or cost > remaining:
                raise DomainError("query_policy_action_budget", "查询动作费用未知或超过剩余预算", 422)
    elif action is not None:
        raise DomainError("query_policy_action_invalid", "停止决策不能附带可执行动作", 502)
    if result["decision"] == "complete":
        current = {item["id"] for item in request.get("evidence", [])}
        required = [check for check in request.get("checks", []) if check.get("required", True)]
        artifacts = {(a["id"], a["version"]) for a in request.get("artifacts", [])}

        def bound(check):
            evidence = check.get("evidence_ids") or []
            refs = check.get("artifact_refs") or []
            if evidence and set(evidence).issubset(current):
                return True
            return (
                request.get("schema_version") == "query-planner.external.v2"
                and bool(refs)
                and all(
                    isinstance(ref, dict) and (ref.get("id"), ref.get("version")) in artifacts for ref in refs
                )
            )

        if not required or any(check.get("status") != "passed" or not bound(check) for check in required):
            raise DomainError("query_policy_coverage_invalid", "尚未绑定当前证据的覆盖检查不能判为完成", 502)
    return copy.deepcopy(result)


def decide_query(settings, request, *, loaded=None, expected_version=None):
    """Call the local method only; errors remain visible instead of silently switching policy."""
    if loaded is None:
        module, version = load_query_planner(settings)
    else:
        module, version = loaded
        current = _source(settings)
        if current is None or current[1] != version:
            raise DomainError("query_policy_version_changed", "本轮查询方法源码已变化，未继续选择动作", 409)
    if expected_version is not None and version != expected_version:
        raise DomainError("query_policy_version_changed", "查询策略源码与本轮固定版本不同，未重选动作", 409)
    try:
        if request.get("schema_version") == "query-planner.external.v2":
            if not callable(getattr(module, "decide_kdd", None)):
                raise DomainError("query_policy_version_changed", "本轮路由未提供KDD能力选择", 409)
            result = module.decide_kdd(
                copy.deepcopy(request), selector=request.get("selector", "information_gain_v1")
            )
        else:
            result = module.decide(copy.deepcopy(request))
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError("query_planner_decision_failed", "本地查询策略未能产生有效决策", 502) from exc
    return validate_decision(request, result, expected_version=version)
