"""指标定义公式统一计算路径与 V031 留档的回归测试。

覆盖点：
* 重构前后同一批指标定义输入 → 相同计算结果（Decimal 精度、四舍五入）；
* 历史精度原文/历史字段迁移到共用路径后仍可回读；
* 同一指标历史公式 + 当前公式的版本冲突两种策略；
* V031 核对写到一半失败时整批回滚，不留残留；
* 口径注册消费端读取 V031 留档并用同一历史样例复算。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest

from app.services import metric_ledger
from app.services.metric_alias import MetricAliasService
from app.services.metric_def import MetricDefService
from app.services.metric_formula import (
    POLICY_HISTORICAL,
    POLICY_TIME,
    calculate_formula,
    evaluate_versions,
    normalize_formula,
    parse_formula,
    resolve_precision,
    resolve_versions,
)
from app.store import store


def _reset(rows: dict[str, list[dict]]) -> None:
    for key in ("metric_def", "metric_alias", metric_ledger.TABLE):
        store._tables[key] = []
    for key, value in rows.items():
        store._tables[key] = value


def _seed_definition(**overrides):
    row = {
        "id": 1, "status": "已生效", "pending": False, "abnormal": False,
        "指标编号": "METR-0001", "指标名称": "点击率",
        "计算公式": "clicks / impressions", "统计粒度": "日", "时间窗口": "7",
        "基准取值": "0.10", "精度位数": "4", "口径版本": "v1",
    }
    row.update(overrides)
    return row


def _seed_alias(**overrides):
    row = {
        "id": 1, "status": "已生效", "pending": False, "abnormal": False,
        "版本编号": "ALIAS-0001", "指标编号": "METR-0001", "变更说明": "改为有效曝光",
        "旧口径": "clicks / impressions", "新口径": "clicks / valid_impressions",
        "生效时间": "2026-09-01", "审批状态": "已生效", "变更人": "admin",
        "旧精度位数": "4", "新精度位数": "3",
    }
    row.update(overrides)
    return row


def _legacy_round_half_up(value: float, digits: int) -> float:
    """重构前平台里通行的口径：按 10 的幂放大后四舍五入，而不是银行家舍入。"""
    scale = 10 ** digits
    return float(int(abs(value) * scale + 0.5)) / scale * (1 if value >= 0 else -1)


@pytest.mark.parametrize("formula,precision,variables", [
    ("clicks / impressions", "4", {"clicks": 25, "impressions": 100}),
    ("clicks / valid_impressions", "3", {"clicks": 25, "valid_impressions": 80}),
    ("(orders / visits) * 100", "2", {"orders": 40, "visits": 200}),
    ("revenue / users", "2", {"revenue": 1200, "users": 20}),
    ("clicks / impressions", "6", {"clicks": 7, "impressions": 9}),
])
def test_refactor_matches_legacy_results(formula, precision, variables):
    """重构前后同一批指标定义输入必须得到相同计算结果。"""
    outcome = calculate_formula(formula, precision, variables)
    assert outcome.status == "ok"
    expected = _legacy_round_half_up(eval(formula, {}, dict(variables)), int(precision))
    assert abs(outcome.value - expected) < 1e-12


def test_rounding_is_half_up_not_bankers():
    """精度位数按四舍五入（0.3125 → 0.313），与原口径一致，不受二进制浮点影响。"""
    assert calculate_formula("clicks / valid_impressions", "3",
                             {"clicks": 25, "valid_impressions": 80}).display == "0.313"
    assert calculate_formula("a / b", "1", {"a": 1, "b": 4}).display == "0.3"


def test_refactor_keeps_legacy_fields_and_precision():
    """旧记录字段原样可读：精度原文不会被改写成积分数。"""
    definition = _seed_definition(精度位数="精度4", 口径版本="v1")
    resolution = resolve_versions(definition, [], POLICY_HISTORICAL)
    evaluation = evaluate_versions(resolution)
    assert evaluation.definition.precision.raw == "精度4"
    assert evaluation.definition.precision.digits == 4
    assert evaluation.definition.display == "0.2500"


def test_formula_calculation_is_stable_with_decimal_rounding():
    """同一输入多次计算得到同样结果，避免浮点误差改变指标口径。"""
    first = calculate_formula("clicks / impressions", "4", {"clicks": 25, "impressions": 100})
    second = calculate_formula("clicks / impressions", "4", {"clicks": 25, "impressions": 100})
    assert first.status == "ok"
    assert first.display == "0.2500"
    assert first.value == second.value == 0.25
    assert first.display == second.display


def test_formula_rounding_matches_historical_half_up():
    """0.3125 在 3 位精度下按四舍五入得到 0.313，与旧实现一致。"""
    outcome = calculate_formula("clicks / valid_impressions", "3", {"clicks": 25, "valid_impressions": 80})
    assert outcome.display == "0.313"


def test_formula_rejects_unsafe_expression_and_keeps_raw():
    parsed = parse_formula("__import__('os').system('id')")
    assert parsed.status == "legacy-unparsed"
    assert parsed.raw == "__import__('os').system('id')"
    outcome = calculate_formula(parsed.raw, "4", {})
    assert outcome.status == "legacy-unparsed"
    assert outcome.display is None


def test_formula_missing_variable_is_readable_not_crash():
    outcome = calculate_formula("clicks / impressions", "4", {"clicks": 10})
    assert outcome.status == "missing-variable"
    assert "impressions" in outcome.message


def test_division_by_zero_is_reported():
    outcome = calculate_formula("clicks / impressions", "4", {"clicks": 10, "impressions": 0})
    assert outcome.status == "error"
    assert "除数为 0" in outcome.message


@pytest.mark.parametrize("formula,precision,variables,expected", [
    ("round(a / b, 2)", "2", {"a": 1, "b": 3}, "0.33"),
    ("min(a, b) + max(a, b)", "1", {"a": 2, "b": 5}, "7.0"),
    ("abs(a) / b", "2", {"a": -1, "b": 8}, "0.13"),
    ("a ** 2 % 7", "0", {"a": 5}, "4"),
    ("a / b", "4", {"a": "10", "b": "4"}, "2.5000"),
])
def test_formula_functions_and_numeric_strings(formula, precision, variables, expected):
    """函数名不能被误当成变量；历史记录里以字符串保存的数值仍可计算。"""
    outcome = calculate_formula(formula, precision, variables)
    assert outcome.status == "ok", outcome.message
    assert outcome.display == expected


def test_formula_function_name_is_not_a_variable():
    parsed = parse_formula("round(clicks / impressions, 2)")
    assert parsed.variables == ("clicks", "impressions")


def test_normalize_fullwidth_formula_keeps_original_archive():
    assert normalize_formula("（clicks × 2）÷ impressions") == "(clicks * 2) / impressions"
    parsed = parse_formula("（clicks × 2）÷ impressions")
    assert parsed.raw == "（clicks × 2）÷ impressions"
    # ast 只去掉语义等价的括号，运算顺序不变
    assert parsed.expression == "clicks * 2 / impressions"
    assert calculate_formula(parsed.raw, "2", {"clicks": 5, "impressions": 10}).display == "1.00"


@pytest.mark.parametrize("raw,digits", [
    ("4", 4), ("4.0", 4), ("精度4", 4), ("精度：4", 4), ("4位", 4), (3, 3), (None, 4), ("", 4),
])
def test_precision_accepts_historical_formats(raw, digits):
    assert resolve_precision(raw).digits == digits


def test_precision_falls_back_but_keeps_raw_for_legacy_row():
    info = resolve_precision("指标定义样例1")
    assert info.digits == 4
    assert info.status == "legacy-fallback"
    assert info.raw == "指标定义样例1"


def test_historical_policy_keeps_original_metric_result():
    definition = _seed_definition()
    alias = _seed_alias()
    resolution = resolve_versions(definition, [alias], POLICY_HISTORICAL)
    assert resolution.conflict is True
    assert resolution.selected.formula == "clicks / impressions"
    assert resolve_precision(resolution.selected.precision_raw).digits == 4


def test_time_policy_uses_current_formula_and_keeps_history_diff():
    definition = _seed_definition()
    alias = _seed_alias()
    resolution = resolve_versions(definition, [alias], POLICY_TIME)
    assert resolution.selected.formula == "clicks / valid_impressions"
    assert resolve_precision(resolution.selected.precision_raw).digits == 3
    assert resolution.historical.formula == "clicks / impressions"
    assert any("历史差异" in note for note in resolution.notes)


def test_history_field_survives_when_alias_has_no_precision_archive():
    """旧版本记录没有旧精度位数时，回退到指标定义精度，历史字段仍保留。"""
    alias = {"版本编号": "OLD-1", "指标编号": "METR-0001", "旧口径": "clicks / impressions",
             "新口径": "clicks / valid_impressions", "生效时间": "2026-08-01", "legacy_note": "历史留档"}
    resolution = resolve_versions(_seed_definition(), [alias], POLICY_HISTORICAL)
    assert resolve_precision(resolution.historical.precision_raw).digits == 4
    assert resolution.historical.historical_fields["legacy_note"] == "历史留档"


def test_unknown_policy_is_rejected():
    with pytest.raises(ValueError):
        resolve_versions(_seed_definition(), [], "随便一种")


def test_v031_confirm_archives_and_is_repeatable():
    _reset({"metric_def": [_seed_definition()], "metric_alias": [_seed_alias()]})
    first = metric_ledger.confirm(POLICY_HISTORICAL)
    second = metric_ledger.confirm(POLICY_HISTORICAL)
    assert first["batch"] == 1 and second["batch"] == 2
    assert first["items"][0]["历史结果"] == second["items"][0]["历史结果"] == "0.2500"
    assert first["items"][0]["当前结果"] == second["items"][0]["当前结果"] == "0.313"
    # 历史留档仍可按批次回查
    assert metric_ledger.archive_for("METR-0001", 1)["历史公式"] == "clicks / impressions"


def test_v031_failure_rolls_back_whole_batch_without_residue(monkeypatch):
    """写档途中任何一条出错，整批回滚，不留半批残留。"""
    _reset({
        "metric_def": [_seed_definition(), _seed_definition(id=2, 指标编号="METR-0002")],
        "metric_alias": [_seed_alias()],
    })
    original = metric_ledger._build_archive
    calls = {"count": 0}

    def flaky(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise metric_ledger.VersionCheckError("模拟第二条留档失败")
        return original(*args, **kwargs)

    monkeypatch.setattr(metric_ledger, "_build_archive", flaky)
    with pytest.raises(metric_ledger.VersionCheckError):
        metric_ledger.confirm()
    assert store.rows(metric_ledger.TABLE) == []
    assert metric_ledger.latest_batch() == 0


def test_v031_legacy_text_is_archived_but_runtime_error_rolls_back():
    """历史文本按原文留档；真实表达式算不出来（除零）必须整批回滚。"""
    _reset({
        "metric_def": [
            _seed_definition(),
            _seed_definition(id=2, 指标编号="METR-LEGACY", 计算公式="指标定义样例1",
                             精度位数="指标定义样例1"),
        ],
        "metric_alias": [],
    })
    result = metric_ledger.confirm()
    assert result["total"] == 2
    legacy = metric_ledger.archive_for("METR-LEGACY")
    assert legacy["定义公式"] == "指标定义样例1"
    assert legacy["定义精度原文"] == "指标定义样例1"
    assert legacy["解析状态"] != "ok"
    assert legacy["解析异常"]
    assert legacy["历史结果"] is None
    assert metric_ledger.archive_for("METR-0001")["历史结果"] == "0.2500"

    # 真实表达式但计算失败 → 整批回滚，旧批次不受影响
    _reset({"metric_def": [_seed_definition()], "metric_alias": []})
    first = metric_ledger.confirm()
    store.rows("metric_def").append(_seed_definition(
        id=3, 指标编号="METR-ZERO", 计算公式="clicks / (impressions - 100)"))
    with pytest.raises(metric_ledger.VersionCheckError):
        metric_ledger.confirm()
    assert metric_ledger.latest_batch() == first["batch"]
    assert len(metric_ledger.archive_rows(first["batch"])) == 1
    assert metric_ledger.archive_for("METR-ZERO") is None


def test_v031_rerun_with_changed_precision_rolls_back_and_keeps_old_batch():
    """口径版本变更导致复算不一致时整批回滚，历史批次仍可回查。"""
    _reset({"metric_def": [_seed_definition()], "metric_alias": [_seed_alias()]})
    first = metric_ledger.confirm()
    assert first["items"][0]["历史结果"] == "0.2500"
    # 改动历史口径精度，让新批次与旧批次结果不同
    store.rows("metric_alias")[0]["旧精度位数"] = "1"
    data = metric_ledger.archive_for("METR-0001", 1)
    assert data["历史结果"] == "0.2500"
    second = metric_ledger.confirm()
    assert second["batch"] == 2
    assert second["items"][0]["历史结果"] == "0.3"
    assert metric_ledger.archive_for("METR-0001", 1)["历史结果"] == "0.2500"


def test_v031_transaction_rolls_back_pending_rows():
    """事务异常时，已追加的行和版本号一起回滚。"""
    _reset({})
    before = store.version(metric_ledger.TABLE)
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.rows(metric_ledger.TABLE).append({"id": 1, "batch": 9})
            store.bump_version(metric_ledger.TABLE)
            raise RuntimeError("模拟中途失败")
    assert store.rows(metric_ledger.TABLE) == []
    assert store.version(metric_ledger.TABLE) == before


def test_metric_def_list_and_detail_read_same_confirmation():
    _reset({"metric_def": [_seed_definition()], "metric_alias": [_seed_alias()]})
    metric_ledger.confirm(POLICY_HISTORICAL)
    service = MetricDefService()
    rows, total = service.list_entries()
    assert total == 1
    listed = rows[0]
    detail = service.get_detail(1)
    assert listed["V031核对"]["公式结果"] == detail["V031留档"]["选中结果"] == "0.2500"
    assert listed["V031核对"]["核对状态"] == "版本冲突"
    assert detail["V031留档"]["持久化一致"] is True
    # 原始字段保持不变，核对结果只放在独立命名空间里
    assert detail["计算公式"] == "clicks / impressions"
    assert detail["精度位数"] == "4"
    assert "公式结果" not in listed
    assert set(listed) - {"V031核对"} == set(_seed_definition())


def test_alias_details_use_their_own_archived_precision():
    """口径详情复算时，旧口径用历史精度、新口径用当前精度，不互相串位。"""
    _reset({"metric_def": [_seed_definition()], "metric_alias": [_seed_alias()]})
    metric_ledger.confirm(POLICY_HISTORICAL)
    detail = MetricAliasService().get_detail(1)
    check = detail["V031复算"]
    assert check["旧口径结果"]["结果"] == "0.2500"
    assert check["新口径结果"]["结果"] == "0.313"
    assert check["留档精度位数"] == 4
    assert check["留档当前精度位数"] == 3


def test_alias_consumer_reads_full_v031_and_recalculates():
    _reset({"metric_def": [_seed_definition()], "metric_alias": [_seed_alias()]})
    metric_ledger.confirm(POLICY_HISTORICAL)
    view = MetricAliasService().consumer_view()
    assert view["ok"] is True
    assert view["mismatches"] == 0
    assert view["items"][0]["旧口径结果"] == "0.2500"
    assert view["items"][0]["V031历史结果"] == "0.2500"


def test_alias_consumer_without_confirmation_is_readable():
    _reset({"metric_def": [_seed_definition()], "metric_alias": [_seed_alias()]})
    view = MetricAliasService().consumer_view()
    assert view["ok"] is False
    assert "尚未完成" in view["message"]


def test_alias_create_keeps_new_persisted_fields():
    _reset({"metric_alias": []})
    entry, missing = MetricAliasService().create_entry({
        "版本编号": "ALIAS-9", "指标编号": "METR-0009", "变更说明": "新增",
        "旧口径": "a / b", "新口径": "a / c", "旧精度位数": "2", "新精度位数": "4",
    })
    assert entry is not None and missing == []
    assert entry["旧精度位数"] == "2"
    assert entry["新精度位数"] == "4"
    # 历史字段读回后仍按原文返回
    assert MetricAliasService().get_entry(entry["id"])["旧精度位数"] == "2"


def test_alias_list_keeps_original_fields():
    """口径列表新增核对结果时不改动原有口径字段。"""
    _reset({"metric_def": [_seed_definition()], "metric_alias": [_seed_alias()]})
    metric_ledger.confirm(POLICY_HISTORICAL)
    rows, _ = MetricAliasService().list_entries()
    item = rows[0]
    assert item["旧口径"] == "clicks / impressions"
    assert item["旧精度位数"] == "4"
    assert item["V031核对"]["旧口径结果"] == "0.2500"
    assert item["V031核对"]["核对状态"] == "与留档一致"
    assert set(item) - {"V031核对"} == set(_seed_alias())
