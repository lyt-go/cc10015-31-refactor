"""指标定义业务规则：状态流转、字段校验与筛选口径都收在这里。

公式解析/精度/版本选择统一走 ``app.services.metric_formula``，核对留档统一走
``app.services.metric_ledger``；本服务只负责把指标定义接进这条共用路径，
不重复实现计算。
"""
from __future__ import annotations

from typing import Any

from app.services import metric_ledger
from app.services.metric_formula import (
    POLICY_HISTORICAL,
    evaluate_versions,
    resolve_versions,
)
from app.store import store

MODULE = "metric_def"
LIST_FIELDS = ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "基准取值", "精度位数", "口径版本"]
REQUIRED_FIELDS = ["指标编号", "指标名称", "计算公式"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
NEGATIVE_ACTIONS = []


class MetricDefService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("指标编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        page_rows = rows[start:start + size]
        confirmed = {str(row.get("指标编号")): row for row in metric_ledger.archive_rows()}
        return [self._with_confirmation(row, confirmed.get(str(row.get("指标编号")))) for row in page_rows], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_detail(self, entry_id: int) -> dict[str, Any] | None:
        """详情页：指标定义原始字段 + 统一计算路径 + V031 留档核对结果。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        detail = dict(entry)
        detail["公式核对"] = self.check_entry(entry_id, policy=metric_ledger.latest_policy())
        detail["V031留档"] = metric_ledger.read_check(str(entry.get("指标编号", "")))
        return detail

    def check_entry(self, entry_id: int, variables: dict[str, Any] | None = None,
                    policy: str = POLICY_HISTORICAL) -> dict[str, Any] | None:
        """按共用路径解析单条指标定义，返回历史/当前口径与选中结果。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        resolution = resolve_versions(entry, store.rows(metric_ledger.ALIAS_MODULE), policy)
        return evaluate_versions(resolution, variables).as_dict()

    def confirm_versions(self, policy: str = POLICY_HISTORICAL) -> dict[str, Any]:
        """执行 V031 核对并整批留档；由指标定义页触发，口径注册消费同一份留档。"""
        return metric_ledger.confirm(policy)

    def list_confirmation(self) -> dict[str, Any]:
        return metric_ledger.list_checks()

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"指标定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于指标定义可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"指标定义已{action}"

    @staticmethod
    def _with_confirmation(row: dict[str, Any], archive: dict[str, Any] | None) -> dict[str, Any]:
        """列表保持原有字段不变，只追加一个 V031 核对结果命名空间。"""
        item = dict(row)
        if archive is None:
            item["V031核对"] = {
                "选口径": None,
                "公式结果": None,
                "历史结果": None,
                "当前结果": None,
                "核对状态": "未核对",
                "策略": None,
                "批次": None,
            }
            return item
        item["V031核对"] = {
            "选口径": archive.get("选中公式"),
            "公式结果": archive.get("选中结果"),
            "历史结果": archive.get("历史结果"),
            "当前结果": archive.get("当前结果"),
            "核对状态": "版本冲突" if archive.get("版本冲突") else "已核对",
            "策略": archive.get("策略"),
            "批次": archive.get("batch"),
        }
        return item
