"""V031 指标定义公式版本留档。

指标定义与口径注册原本各自读一遍公式、精度和版本字段。这里把「核对」收成
一份可持久化的留档：

* 每个指标定义按统一计算路径（``metric_formula``）生成历史样例、历史口径和
  当前口径结果，连同指标定义原始字段一并写入 ``_metric_def_v031`` 表；
* 写档过程整体包在 ``store.transaction()`` 里，任何一条公式解析失败或留档
  复算不一致都会让整批回滚，不会留下半批残留；
* 列表页、详情页和口径注册消费端都只读这里的留档，不再各自解析公式。

表名前缀 ``_`` 表示内部留档表，不计入业务模块概览。
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from app.services.metric_formula import (
    POLICY_HISTORICAL,
    STATUS_EMPTY,
    STATUS_LEGACY,
    STATUS_OK,
    calculate_formula,
    check_against_archive,
    evaluate_versions,
    normalize_formula,
    resolve_versions,
)
from app.store import store

TABLE = "_metric_def_v031"
TASK = "V031"
DEFINITION_MODULE = "metric_def"
ALIAS_MODULE = "metric_alias"


class VersionCheckError(ValueError):
    """核对无法完成：整批留档必须回滚。"""


def latest_batch() -> int:
    rows = store.rows(TABLE)
    return max((int(row.get("batch", 0)) for row in rows), default=0)


def archive_rows(batch: int | None = None) -> list[dict[str, Any]]:
    rows = store.rows(TABLE)
    target = latest_batch() if batch is None else batch
    if target <= 0:
        return []
    return [row for row in rows if int(row.get("batch", 0)) == target]


def archive_for(metric_code: str, batch: int | None = None) -> dict[str, Any] | None:
    for row in archive_rows(batch):
        if str(row.get("指标编号")) == str(metric_code):
            return row
    return None


def latest_policy() -> str:
    """详情页沿用列表页最近一次核对时选的策略，避免两页口径不一致。"""
    rows = archive_rows()
    if not rows:
        return POLICY_HISTORICAL
    return str(rows[0].get("策略") or POLICY_HISTORICAL)


def confirm(policy: str = POLICY_HISTORICAL) -> dict[str, Any]:
    """按策略核对全部指标定义并留档；失败整批回滚。"""
    definitions = [dict(row) for row in store.rows(DEFINITION_MODULE)]
    aliases = [dict(row) for row in store.rows(ALIAS_MODULE)]
    if not definitions:
        raise VersionCheckError("没有可核对的指标定义")

    # 先算出一个批次号，写入仍在事务内完成，保证并发/异常时不会跳号残留。
    batch = latest_batch() + 1
    created_at = time.time()
    written: list[dict[str, Any]] = []
    with store.transaction():
        for definition in definitions:
            row = _build_archive(definition, aliases, policy, batch, created_at)
            # 逐条落库：后续任何一条失败，事务会把已写入的行一并回滚。
            store.rows(TABLE).append(row)
            _verify_archive_row(row)
            written.append(row)
        if len(written) != len(definitions):
            raise VersionCheckError("留档数量与指标定义数量不一致，整批回滚")
        store.bump_version(TABLE)
    return {
        "ok": True,
        "task": TASK,
        "batch": batch,
        "policy": policy,
        "total": len(written),
        "conflicts": sum(1 for row in written if row.get("版本冲突")),
        "message": f"{TASK} 核对完成，留档 {len(written)} 条指标定义公式版本",
        "items": written,
    }


def _build_archive(
    definition: dict[str, Any],
    aliases: list[dict[str, Any]],
    policy: str,
    batch: int,
    created_at: float,
) -> dict[str, Any]:
    metric_code = str(definition.get("指标编号") or "")
    resolution = resolve_versions(definition, aliases, policy)
    evaluation = evaluate_versions(resolution)
    sample = evaluation.sample
    definition_outcome = evaluation.definition
    historical_outcome = evaluation.historical
    current_outcome = evaluation.current
    selected_outcome = evaluation.selected
    definition_precision = definition_outcome.precision
    historical_precision = historical_outcome.precision
    current_precision = current_outcome.precision
    # 历史脏数据（例如公式栏误填「指标定义样例1」）按原文留档并标记解析状态，
    # 保证旧记录仍可读取、可回查；但真实表达式却算不出结果（除零等）说明口径
    # 会漂移，必须整批回滚。
    legacy_notes: list[str] = []
    for label, outcome in (
        ("指标定义公式", definition_outcome),
        ("历史公式", historical_outcome),
        ("当前公式", current_outcome),
    ):
        if outcome.status in (STATUS_LEGACY, STATUS_EMPTY):
            legacy_notes.append(f"{label}按原文留档：{outcome.message}")
        elif outcome.status != STATUS_OK:
            raise VersionCheckError(
                f"指标 {metric_code} 的{label}无法继续得到原输入输出：{outcome.message}，整批回滚"
            )

    return {
        "id": len(store.rows(TABLE)) + 1,
        "batch": batch,
        "task": TASK,
        "created_at": created_at,
        "策略": policy,
        "metric_id": definition.get("id"),
        "指标编号": metric_code,
        "指标名称": definition.get("指标名称"),
        "定义口径版本": definition.get("口径版本"),
        "定义公式": definition_outcome.formula_raw,
        "定义公式规范化": normalize_formula(definition_outcome.formula_raw),
        "定义精度原文": definition_precision.raw,
        "定义精度位数": definition_precision.digits,
        "解析状态": definition_outcome.status,
        "解析表达式": definition_outcome.parsed.expression,
        "定义结果": definition_outcome.display,
        "定义数值": definition_outcome.value if definition_outcome.status == STATUS_OK else None,
        "变量": list(definition_outcome.parsed.variables),
        "历史样例": dict(sample),
        "历史公式": resolution.historical.formula,
        "历史精度原文": historical_precision.raw,
        "历史精度位数": historical_precision.digits,
        "历史结果": historical_outcome.display,
        "历史数值": historical_outcome.value if historical_outcome.status == STATUS_OK else None,
        "当前公式": resolution.current.formula,
        "当前精度原文": current_precision.raw,
        "当前精度位数": current_precision.digits,
        "当前结果": current_outcome.display,
        "当前数值": current_outcome.value if current_outcome.status == STATUS_OK else None,
        "选中公式": selected_outcome.formula_raw,
        "选中精度位数": selected_outcome.precision.digits,
        "选中结果": selected_outcome.display,
        "选中数值": selected_outcome.value if selected_outcome.status == STATUS_OK else None,
        "选中角色": resolution.selected.role,
        "版本冲突": resolution.conflict,
        "冲突说明": resolution.conflict_reason,
        "版本说明": list(resolution.notes) + legacy_notes,
        "解析异常": legacy_notes,
        "候选版本": [candidate.as_dict() for candidate in resolution.candidates],
        "指标定义字段": _json_safe(definition),
    }


def _verify_archive_row(row: dict[str, Any]) -> None:
    """留档读回后按原输入再算一次，确认持久化结果没有漂移。

    解析失败的历史记录不参与比较：它们本来就保留原文、不产出结果。
    """
    sample = row.get("历史样例") or {}
    for label, formula_key, precision_key, result_key in (
        ("历史", "历史公式", "历史精度位数", "历史结果"),
        ("当前", "当前公式", "当前精度位数", "当前结果"),
        ("指标定义", "定义公式", "定义精度位数", "定义结果"),
    ):
        formula = row.get(formula_key)
        precision = row.get(precision_key)
        stored = row.get(result_key)
        outcome = calculate_formula(formula, precision, sample)
        if outcome.status in (STATUS_LEGACY, STATUS_EMPTY):
            if stored is not None:
                raise VersionCheckError(
                    f"指标 {row.get('指标编号')} 的{label}结果为不可计算却留了值，整批回滚"
                )
            continue
        if outcome.status != STATUS_OK or outcome.display != stored:
            raise VersionCheckError(
                f"指标 {row.get('指标编号')} 的{label}留档复算不一致："
                f"留档 {stored}，复算 {outcome.display}，整批回滚"
            )


def read_check(metric_code: str, batch: int | None = None) -> dict[str, Any] | None:
    """读取单指标留档，并返回实时复算的「历史/当前结果」用于详情页核对。"""
    row = archive_for(metric_code, batch)
    if row is None:
        return None
    sample = dict(row.get("历史样例") or {})
    recomputed = {
        key: calculate_formula(row.get(formula_key), row.get(precision_key), sample).as_dict()
        for key, formula_key, precision_key in (
            ("历史", "历史公式", "历史精度位数"),
            ("当前", "当前公式", "当前精度位数"),
            ("指标定义", "定义公式", "定义精度位数"),
        )
    }
    consistent = all(
        recomputed[key].get("结果") == row.get(result_key)
        for key, result_key in (("历史", "历史结果"), ("当前", "当前结果"), ("指标定义", "定义结果"))
        if result_key in row
    )
    detail = dict(row)
    detail["实时复算"] = recomputed
    detail["持久化一致"] = consistent
    detail["留档批次"] = int(row.get("batch", 0))
    return _json_safe(detail)


def list_checks(metric_code: str | None = None) -> dict[str, Any]:
    batch = latest_batch()
    rows = archive_rows(batch)
    if metric_code is not None:
        rows = [row for row in rows if str(row.get("指标编号")) == str(metric_code)]
    return {
        "task": TASK,
        "batch": batch,
        "confirmed": batch > 0,
        "total": len(rows),
        "policy": rows[0].get("策略") if rows else None,
        "items": [_json_safe(row) for row in rows],
        "message": (
            f"{TASK} 留档批次 {batch} 共 {len(rows)} 条指标定义公式版本"
            if batch > 0 else f"尚未完成 {TASK} 核对，请先在指标定义页执行核对"
        ),
    }


def history(metric_code: str | None = None) -> list[dict[str, Any]]:
    """回查全部留档批次，按批次倒序。"""
    rows = store.rows(TABLE)
    if metric_code is not None:
        rows = [row for row in rows if str(row.get("指标编号")) == str(metric_code)]
    rows = sorted(rows, key=lambda row: (int(row.get("batch", 0)), int(row.get("id", 0))), reverse=True)
    return [_json_safe(row) for row in rows]


def consumer_cross_check(alias_rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """口径注册消费端：读 V031 留档里的公式和精度，用同一历史样例复算。"""
    batch = latest_batch()
    if batch <= 0:
        return {"ok": False, "message": f"尚未完成 {TASK} 核对，口径注册暂不能复算", "items": []}
    archives = {str(row.get("指标编号")): row for row in archive_rows(batch)}
    items: list[dict[str, Any]] = []
    mismatches = 0
    for alias in alias_rows:
        metric_code = str(alias.get("指标编号") or "")
        check = check_against_archive(alias, archives.get(metric_code))
        detail = check.as_dict()
        if not check.matches:
            mismatches += 1
        items.append({
            "版本编号": alias.get("版本编号"),
            "指标编号": metric_code,
            "旧口径": alias.get("旧口径"),
            "新口径": alias.get("新口径"),
            "V031批次": batch,
            "核对状态": check.status,
            "留档历史公式": detail["留档历史公式"],
            "留档精度位数": check.old_precision,
            "留档当前精度位数": check.new_precision,
            "历史样例": detail["历史样例"],
            "旧口径结果": check.old.display,
            "新口径结果": check.new.display,
            "V031历史结果": check.archive_result,
            "与留档一致": check.matches,
            "说明": check.old.message if check.old.status != STATUS_OK else (
                "与 V031 留档口径一致" if check.matches else "留档缺失或结果不一致，请重新核对"
            ),
        })
    return {
        "ok": True,
        "task": TASK,
        "batch": batch,
        "total": len(items),
        "mismatches": mismatches,
        "items": items,
        "message": f"读取 {TASK} 留档 {len(archives)} 条指标定义公式版本，复算 {len(items)} 条口径记录",
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
