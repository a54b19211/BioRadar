"""A second fixed pure computation uses the existing analysis-run authority."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import combinations
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from bioradar.api.errors import DomainError
from bioradar.research.decision.models import (
    DECISION_COMPUTE_VERSION,
    DecisionInput,
    DecisionPolicy,
    DecisionResult,
)
from bioradar.research.engine.auth import DevelopmentAccess, EngineActor
from bioradar.research.engine.models import EngineRun
from bioradar.research.engine.schemas import (
    DecisionRunCreate,
    DecisionRunList,
    DecisionRunView,
    DecisionSnapshotBinding,
    RunAction,
    RunError,
)
from bioradar.research.mining.models import NormalizedSnapshot, canonical_hash

if TYPE_CHECKING:
    from bioradar.research.engine.service import EngineService


@dataclass(frozen=True)
class DecisionExecution:
    input: DecisionInput
    policy: DecisionPolicy
    snapshots: tuple[NormalizedSnapshot, ...]


class DecisionRuntime:
    def __init__(self, engine: "EngineService"):
        self.engine = engine

    @staticmethod
    def _versions() -> dict[str, str]:
        return {"__decision_compute__": DECISION_COMPUTE_VERSION}

    @staticmethod
    def _current(model, raw):
        try:
            parsed = model.model_validate(raw)
        except ValidationError:
            return None
        return parsed if canonical_hash(parsed) == canonical_hash(raw) else None

    def _sources(self, session: Session, actor: EngineActor, bindings: list[dict]):
        snapshots = []
        for binding in bindings:
            row = self.engine._snapshot(session, actor, binding["id"])
            if row.snapshot_hash != binding["snapshot_hash"]:
                raise DomainError("decision_source_changed", "决策绑定的固定来源版本不一致", 502)
            snapshots.append(NormalizedSnapshot.model_validate(row.payload))
        return tuple(snapshots)

    def _run(self, session: Session, actor: EngineActor, identifier: str, *, lock=False):
        if actor != DevelopmentAccess(self.engine.settings).actor():
            raise DomainError("permission_denied", "开发授权上下文不匹配", 403)
        statement = select(EngineRun).where(
            EngineRun.id == identifier,
            EngineRun.workspace_id == actor.workspace_id,
            EngineRun.run_kind == "decision",
        )
        if lock:
            statement = statement.with_for_update()
        row = session.scalar(statement)
        if row is None:
            raise DomainError("not_found", "未找到可访问的固定决策分析", 404)
        self._sources(session, actor, row.decision_input["snapshot_bindings"])
        return row

    def _view(self, row: EngineRun, *, detail=True) -> DecisionRunView:
        envelope = row.decision_input
        raw = envelope["input"]
        current_input = self._current(DecisionInput, raw)
        current_policy = self._current(DecisionPolicy, row.policy)
        return DecisionRunView(
            id=row.id,
            title=raw["brief"]["title"],
            audience=raw["brief"]["audience"],
            is_demo=raw["is_demo"],
            input_hash=row.input_hash,
            evidence_basis_hash=envelope["evidence_basis_hash"],
            source_snapshots=envelope["snapshot_bindings"],
            submitted_by=envelope["submitted_by"],
            accepted_at=envelope["accepted_at"],
            submitted_request_hash=envelope["request_hash"],
            decision_input=current_input if detail else None,
            original_input=raw if detail else None,
            input_view_status="current" if current_input is not None else "historical_requires_new_run",
            policy=current_policy,
            original_policy=row.policy,
            policy_view_status="current" if current_policy is not None else "historical_requires_new_run",
            state=row.state,
            revision=row.revision,
            attempt=row.attempt,
            dispatch_pending=row.dispatch_pending,
            dependency_note="决策分析已持久受理，RQ尚未确认投递" if row.dispatch_pending else None,
            created_at=row.created_at,
            completed_at=row.completed_at,
            error=RunError(code=row.error_code, message=row.error_message or "分析执行失败")
            if row.error_code
            else None,
            result=DecisionResult.model_validate(row.result) if detail and row.result else None,
            attempt_diagnostics=row.attempt_diagnostics if detail else [],
        )

    @staticmethod
    def _check_authored_at(value: Any, accepted_at: datetime) -> None:
        if isinstance(value, dict):
            if {"kind", "claimed_author", "authored_at", "rationale"} <= value.keys():
                if datetime.fromisoformat(value["authored_at"].replace("Z", "+00:00")) > accepted_at:
                    raise DomainError(
                        "decision_future_declaration", "署名声明时间晚于服务器当前采用时间", 422
                    )
            for child in value.values():
                DecisionRuntime._check_authored_at(child, accepted_at)
        elif isinstance(value, list):
            for child in value:
                DecisionRuntime._check_authored_at(child, accepted_at)

    @staticmethod
    def _evidence(input: DecisionInput, snapshots: tuple[NormalizedSnapshot, ...]) -> str:
        from bioradar.research.decision.evidence import evidence_basis_hash, validate_evidence

        try:
            validate_evidence(input, snapshots)
            return evidence_basis_hash(input, snapshots)
        except ValueError:
            raise DomainError(
                "decision_evidence_invalid", "决策引用、数值字段或固定来源不符合采用契约", 422
            ) from None

    def create(self, actor: EngineActor, body: DecisionRunCreate, key: str) -> tuple[int, dict]:
        from bioradar.research.decision.hashing import decision_input_hash, decision_policy_hash

        if actor != DevelopmentAccess(self.engine.settings).actor():
            raise DomainError("permission_denied", "开发授权上下文不匹配", 403)
        if len(set(body.snapshot_ids)) != len(body.snapshot_ids) or any(
            not item or len(item) > 36 for item in body.snapshot_ids
        ):
            raise DomainError("decision_snapshot_invalid", "快照ID必须唯一且符合固定采用记录格式", 422)
        if len(body.model_dump_json().encode("utf-8")) > self.engine.settings.engine_max_decision_bytes:
            raise DomainError("engine_budget_exceeded", "决策输入超过服务器运行预算", 422)
        if body.policy.max_questions > 48:
            raise DomainError("engine_budget_exceeded", "补证问题数超过服务器运行预算", 422)
        metric_ids = {metric.metric_id for metric in body.input.metrics}
        if not set(body.policy.pareto_metric_ids) <= metric_ids or (
            body.policy.regret_metric_id is not None and body.policy.regret_metric_id not in metric_ids
        ):
            raise DomainError("decision_policy_invalid", "策略只能引用本次委托已声明的指标", 422)
        with Session(self.engine.database) as session, session.begin():
            accepted_at = datetime.now(UTC)
            if body.input.brief.as_of > accepted_at:
                raise DomainError(
                    "decision_future_cutoff", "当前决策分析的证据截止不能晚于服务器采用时间", 422
                )
            rows = [self.engine._snapshot(session, actor, identifier) for identifier in body.snapshot_ids]
            bindings = sorted(
                [
                    DecisionSnapshotBinding(
                        id=row.id,
                        snapshot_hash=row.snapshot_hash,
                        source_instance=row.source_instance,
                        is_demo=row.payload["is_demo"],
                    ).model_dump(mode="json")
                    for row in rows
                ],
                key=lambda item: item["snapshot_hash"],
            )
            snapshots = self._sources(session, actor, bindings)
            if len({snapshot.is_demo for snapshot in snapshots}) > 1:
                raise DomainError("fixture_scope_mismatch", "真实来源与测试夹具不能混合决策采用", 422)
            if any(snapshot.is_demo for snapshot in snapshots) and not body.input.is_demo:
                raise DomainError("fixture_scope_mismatch", "测试来源的决策输入必须明确标注演示", 422)
            if set(body.input.evidence_snapshot_hashes) != {binding["snapshot_hash"] for binding in bindings}:
                raise DomainError("decision_snapshot_mismatch", "决策声明的来源hash与已采用快照不一致", 422)
            if (
                sum(len(snapshot.records) for snapshot in snapshots) > self.engine.settings.engine_max_records
                or sum(len(snapshot.model_dump_json().encode("utf-8")) for snapshot in snapshots)
                > self.engine.settings.engine_max_snapshot_bytes
            ):
                raise DomainError("engine_budget_exceeded", "决策采用范围超过服务器运行预算", 422)
            evidence_hash = self._evidence(body.input, snapshots)
            raw_input = body.input.model_dump(mode="json")
            self._check_authored_at(raw_input, accepted_at)
            input_hash = canonical_hash(
                {
                    "run_kind": "decision",
                    "decision_input_hash": decision_input_hash(body.input),
                    "policy_hash": decision_policy_hash(body.policy),
                    "evidence_basis_hash": evidence_hash,
                    "compute_version": DECISION_COMPUTE_VERSION,
                }
            )

            def mutate():
                identifier = str(uuid4())
                session.execute(
                    insert(EngineRun)
                    .values(
                        id=identifier,
                        workspace_id=actor.workspace_id,
                        run_kind="decision",
                        snapshot_id=None,
                        decision_input={
                            "input": raw_input,
                            "snapshot_bindings": bindings,
                            "evidence_basis_hash": evidence_hash,
                            "submitted_by": actor.actor_id,
                            "accepted_at": accepted_at.isoformat(),
                            "request_hash": canonical_hash(body),
                        },
                        input_hash=input_hash,
                        policy=body.policy.model_dump(mode="json"),
                        method_versions=self._versions(),
                        state="queued",
                        revision=1,
                        attempt=1,
                        accept_token=secrets.token_hex(32),
                        dispatch_pending=True,
                        rq_job_id=f"engine-{identifier}-a1",
                    )
                    .on_conflict_do_nothing(index_elements=["workspace_id", "input_hash"])
                )
                row = session.scalar(
                    select(EngineRun).where(
                        EngineRun.workspace_id == actor.workspace_id, EngineRun.input_hash == input_hash
                    )
                )
                assert row is not None
                return 202, self._view(row, detail=False).model_dump(mode="json")

            return self.engine._idempotent(session, actor, "decision.create", key, body, mutate)

    def get(self, actor: EngineActor, identifier: str) -> DecisionRunView:
        with Session(self.engine.database) as session:
            return self._view(self._run(session, actor, identifier))

    def list(self, actor: EngineActor, cursor=None, limit=30) -> DecisionRunList:
        with Session(self.engine.database) as session:
            statement = select(EngineRun).where(
                EngineRun.workspace_id == actor.workspace_id, EngineRun.run_kind == "decision"
            )
            rows = list(session.scalars(self.engine._page(statement, EngineRun, cursor, limit)))
            visible = []
            for row in rows[:limit]:
                try:
                    self._run(session, actor, row.id)
                    visible.append(self._view(row, detail=False))
                except DomainError as exc:
                    if exc.status_code != 404:
                        raise
            return DecisionRunList(items=visible, next_cursor=self.engine._next_cursor(rows, limit))

    def start(self, session: Session, row: EngineRun):
        actor = DevelopmentAccess(self.engine.settings).actor()
        snapshots = self._sources(session, actor, row.decision_input["snapshot_bindings"])
        input = self._current(DecisionInput, row.decision_input["input"])
        policy = self._current(DecisionPolicy, row.policy)
        if input is None or policy is None or row.method_versions != self._versions():
            row.state, row.error_code = "failed", "decision_version_changed"
            row.error_message = "决策Schema或计算版本已变化，请保留原记录并创建新分析"
            row.completed_at, row.dispatch_pending = datetime.now(UTC), False
            row.revision += 1
            return None
        if self._evidence(input, snapshots) != row.decision_input["evidence_basis_hash"]:
            raise DomainError("decision_source_changed", "决策固定证据基准发生变化", 502)
        row.state, row.started_at, row.dispatch_pending = "running", datetime.now(UTC), False
        row.revision += 1
        return DecisionExecution(input, policy, snapshots)

    def finish(self, session: Session, row: EngineRun, result: DecisionResult) -> bool:
        from bioradar.research.decision.hashing import decision_input_hash, decision_policy_hash
        from bioradar.research.engine.service import RESULT_BUDGET_MESSAGE

        if not isinstance(result, DecisionResult):
            raise DomainError("result_kind_mismatch", "计算结果种类与冻结运行不一致", 502)
        actor = DevelopmentAccess(self.engine.settings).actor()
        snapshots = self._sources(session, actor, row.decision_input["snapshot_bindings"])
        input = self._current(DecisionInput, row.decision_input["input"])
        policy = self._current(DecisionPolicy, row.policy)
        if input is None or policy is None:
            raise DomainError("decision_version_changed", "决策输入Schema已变化，请创建新分析", 502)
        if result.compute_version != DECISION_COMPUTE_VERSION or row.method_versions != self._versions():
            raise DomainError("decision_version_changed", "决策计算版本与固定运行不一致", 502)
        evidence_hash = self._evidence(input, snapshots)
        if (
            result.input_hash != decision_input_hash(input)
            or result.policy_hash != decision_policy_hash(policy)
            or result.evidence_basis_hash != evidence_hash
            or evidence_hash != row.decision_input["evidence_basis_hash"]
            or result.is_demo != input.is_demo
        ):
            raise DomainError("result_input_mismatch", "决策结果与固定输入或证据基准不匹配", 502)
        payload = result.model_dump(mode="json")
        if len(result.model_dump_json().encode("utf-8")) > self.engine.settings.engine_max_result_bytes:
            raise DomainError("result_budget_exceeded", RESULT_BUDGET_MESSAGE, 502)
        if canonical_hash({key: value for key, value in payload.items() if key != "result_hash"}) != (
            result.result_hash
        ):
            raise DomainError("result_hash_mismatch", "决策结果内容哈希不匹配", 502)
        self._validate_output_ids(input, policy, result)
        row.result, row.result_hash = payload, result.result_hash
        row.state = "partial" if result.execution_status == "partial" else "completed"
        row.error_code = row.error_message = None
        row.completed_at, row.dispatch_pending = datetime.now(UTC), False
        row.revision += 1
        return True

    @staticmethod
    def _validate_output_ids(input: DecisionInput, policy: DecisionPolicy, result: DecisionResult) -> None:
        options = {option.option_id for option in input.options}
        scenarios = {scenario.scenario_id for scenario in input.scenarios}
        metrics = {metric.metric_id for metric in input.metrics}
        constraints = {constraint.constraint_id: constraint for constraint in input.constraints}
        cells = {(cell.option_id, cell.scenario_id, cell.metric_id): cell for cell in input.outcomes}

        def check(option_ids=(), scenario_id=None, metric_ids=(), constraint_id=None):
            if (
                not set(option_ids) <= options
                or (scenario_id is not None and scenario_id not in scenarios)
                or not set(metric_ids) <= metrics
                or (constraint_id is not None and constraint_id not in constraints)
            ):
                raise DomainError(
                    "decision_result_binding_invalid", "决策结果引用了未声明的选项或比较条件", 502
                )

        feasibility_ids = [item.option_id for item in result.feasibility]
        if len(set(feasibility_ids)) != len(feasibility_ids) or (
            result.execution_status != "unsupported" and set(feasibility_ids) != options
        ):
            raise DomainError("decision_result_binding_invalid", "门槛结果的选项集合缺失或重复", 502)
        expected_gates = {(scenario, constraint) for scenario in scenarios for constraint in constraints}
        for feasibility in result.feasibility:
            check((feasibility.option_id,))
            gate_keys = [(gate.scenario_id, gate.constraint_id) for gate in feasibility.checks]
            if len(set(gate_keys)) != len(gate_keys) or set(gate_keys) != expected_gates:
                raise DomainError("decision_result_binding_invalid", "选项门槛结果缺失或重复", 502)
            for gate in feasibility.checks:
                check((gate.option_id,), gate.scenario_id, (gate.metric_id,), gate.constraint_id)
                if gate.option_id != feasibility.option_id:
                    raise DomainError("decision_result_binding_invalid", "门槛结果与所属选项不匹配", 502)
                constraint = constraints[gate.constraint_id]
                cell = cells.get((gate.option_id, gate.scenario_id, gate.metric_id))
                if (
                    gate.metric_id != constraint.metric_id
                    or gate.threshold != constraint.threshold
                    or gate.value != (cell.value if cell else None)
                ):
                    raise DomainError(
                        "decision_result_binding_invalid", "门槛数值或条件不是冻结输入的固定值", 502
                    )
        difference_keys = [
            (item.option_id, item.scenario_id, item.metric_id) for item in result.baseline_differences
        ]
        expected_differences = {
            (option, scenario, metric) for option in options for scenario in scenarios for metric in metrics
        }
        if len(set(difference_keys)) != len(difference_keys) or (
            result.execution_status != "unsupported" and set(difference_keys) != expected_differences
        ):
            raise DomainError("decision_result_binding_invalid", "基线比较的条件集合缺失或重复", 502)
        for difference in result.baseline_differences:
            check(
                (difference.option_id, difference.baseline_option_id),
                difference.scenario_id,
                (difference.metric_id,),
            )
            if difference.baseline_option_id != input.brief.baseline_option_id:
                raise DomainError("decision_result_binding_invalid", "结果的现状基线与委托不匹配", 502)
        for comparison in result.pareto_comparisons:
            check((comparison.option_a, comparison.option_b), comparison.scenario_id, comparison.metric_ids)
            if set(comparison.metric_ids) != set(policy.pareto_metric_ids):
                raise DomainError("decision_result_binding_invalid", "结果的Pareto指标与固定策略不匹配", 502)
        for question in result.investigation_questions:
            check(
                question.option_ids,
                question.scenario_id,
                (question.metric_id,) if question.metric_id else (),
                question.constraint_id,
            )
        regret = result.regret
        check(result.conditional_comparison_set)
        check(tuple(result.excluded_from_comparison))
        comparison_set = set(result.conditional_comparison_set)
        if result.execution_status == "unsupported":
            if (
                result.feasibility
                or comparison_set
                or result.excluded_from_comparison
                or result.baseline_differences
                or result.pareto_comparisons
            ):
                raise DomainError(
                    "decision_result_binding_invalid", "未计算结果不能带可行或选项比较产物", 502
                )
        else:
            passed = {item.option_id for item in result.feasibility if item.status == "pass"}
            if (
                len(comparison_set) != len(result.conditional_comparison_set)
                or comparison_set != passed
                or set(result.excluded_from_comparison) != options - passed
            ):
                raise DomainError(
                    "decision_result_binding_invalid", "可比与排除集合没有完整对应固定门槛结果", 502
                )
            pair_keys = [
                (*sorted((item.option_a, item.option_b)), item.scenario_id)
                for item in result.pareto_comparisons
            ]
            expected_pairs = {
                (a, b, scenario)
                for a, b in combinations(sorted(comparison_set), 2)
                for scenario in (*scenarios, None)
            }
            if len(set(pair_keys)) != len(pair_keys) or set(pair_keys) != expected_pairs:
                raise DomainError(
                    "decision_result_binding_invalid", "Pareto组合缺失、重复或包含不可比选项", 502
                )
        check(
            (*regret.conditional_comparison_set, *regret.excluded_options, *regret.least_upper_bound_options),
            metric_ids=(regret.metric_id,) if regret.metric_id else (),
        )
        if regret.metric_id is not None and regret.metric_id != policy.regret_metric_id:
            raise DomainError("decision_result_binding_invalid", "结果的后悔指标与固定策略不匹配", 502)
        if (
            len(set(regret.conditional_comparison_set)) != len(regret.conditional_comparison_set)
            or len(set(regret.excluded_options)) != len(regret.excluded_options)
            or set(regret.conditional_comparison_set) != comparison_set
            or set(regret.excluded_options) != set(result.excluded_from_comparison)
            or set(regret.exclusion_reasons) != set(result.excluded_from_comparison)
            or not set(regret.least_upper_bound_options) <= comparison_set
            or not set(regret.worst_case_bounds) <= comparison_set
            or any(case.option_id not in comparison_set for case in regret.cases)
        ):
            raise DomainError("decision_result_binding_invalid", "后悔比较集合与固定可行范围不匹配", 502)
        if regret.metric_id is not None and regret.unit != next(
            metric.unit for metric in input.metrics if metric.metric_id == regret.metric_id
        ):
            raise DomainError("decision_result_binding_invalid", "后悔结果单位与固定指标不匹配", 502)
        case_keys = [(case.option_id, case.scenario_id) for case in regret.cases]
        if len(set(case_keys)) != len(case_keys) or (
            regret.status == "complete"
            and set(case_keys) != {(option, scenario) for option in comparison_set for scenario in scenarios}
        ):
            raise DomainError("decision_result_binding_invalid", "后悔情景结果缺失或重复", 502)
        for case in regret.cases:
            check((case.option_id,), case.scenario_id)
        check(tuple(regret.worst_case_bounds))
        check(tuple(regret.exclusion_reasons))

    def retry(self, actor: EngineActor, identifier: str, action: RunAction, key: str) -> tuple[int, dict]:
        from bioradar.research.engine.service import RESULT_BUDGET_MESSAGE

        with Session(self.engine.database) as session, session.begin():
            self._run(session, actor, identifier)

            def mutate():
                row = self._run(session, actor, identifier, lock=True)
                if row.revision != action.expected_revision or row.state != "failed":
                    raise DomainError("run_state_conflict", "运行版本或状态不允许重试", 409)
                if row.attempt >= self.engine.settings.engine_retry_limit:
                    raise DomainError("retry_limit_exceeded", "已达到本次固定输入重试上限", 409)
                if row.error_code == "result_budget_exceeded":
                    raise DomainError("result_budget_exceeded", RESULT_BUDGET_MESSAGE, 409)
                if (
                    self._current(DecisionInput, row.decision_input["input"]) is None
                    or self._current(DecisionPolicy, row.policy) is None
                    or row.method_versions != self._versions()
                ):
                    raise DomainError("decision_version_changed", "决策输入或方法已升级，请创建新分析", 409)
                self.engine._requeue(row)
                return 202, self._view(row, detail=False).model_dump(mode="json")

            return self.engine._idempotent(
                session, actor, f"decision.retry:{identifier}", key, action, mutate
            )
