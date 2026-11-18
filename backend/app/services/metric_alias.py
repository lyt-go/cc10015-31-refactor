"""口径注册业务规则：状态流转、字段校验与筛选口径都收在这里。

口径版本不再自行解析公式和精度：确认生效前先读 ``metric_ledger`` 的 V031
留档，用留档里的公式、精度和同一份历史样例复算；留档缺失时给出可读提示，
但仍允许把版本状态推进，避免历史流程被卡死。
"""
from __future__ import annotations

from typing import Any

from app.services import metric_ledger
from app.services.metric_formula import (
    POLICY_HISTORICAL,
    check_against_archive,
    resolve_versions,
)

from app.store import store

MODULE = "metric_alias"
LIST_FIELDS = ["版本编号", "指标编号", "变更说明", "旧口径", "新口径", "生效时间", "审批状态", "变更人"]
REQUIRED_FIELDS = ["版本编号", "指标编号", "变更说明"]
STATUS_ORDER = ["草稿", "待审批", "已生效", "已回滚"]
ACTION_RULES = {"提交审批": "待审批", "确认生效": "已生效", "执行回滚": "已回滚"}
NEGATIVE_ACTIONS = []


class MetricAliasService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("版本编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        page_rows = rows[start:start + size]
        return [self._with_confirmation(row) for row in page_rows], total

    def _with_confirmation(self, row: dict[str, Any]) -> dict[str, Any]:
        """列表保持原有口径字段不变，只追加一个 V031 复算命名空间。"""
        item = dict(row)
        check = self.cross_check(row)
        item["V031核对"] = {
            "旧口径结果": check["旧口径结果"]["结果"],
            "新口径结果": check["新口径结果"]["结果"],
            "留档历史结果": check["V031历史结果"],
            "与留档一致": check["与留档一致"],
            "核对状态": check["核对状态"],
            "批次": check["留档批次"],
            "说明": check["说明"],
        }
        return item

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_detail(self, entry_id: int) -> dict[str, Any] | None:
        """详情页读取 V031 留档，用留档公式/精度和历史样例复算本版本。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        detail = dict(entry)
        detail["V031复算"] = self.cross_check(entry)
        return detail

    def cross_check(self, entry: dict[str, Any]) -> dict[str, Any]:
        """读 V031 留档里的公式和精度，用同一历史样例复算本条口径。"""
        archive = metric_ledger.archive_for(str(entry.get("指标编号") or ""))
        return check_against_archive(entry, archive).as_dict()

    def consumer_view(self) -> dict[str, Any]:
        """消费端：一次性读取全量 V031 指标定义公式版本并复算本模块口径。"""
        return metric_ledger.consumer_cross_check(store.rows(MODULE))

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        # 历史字段：口径登记时把当时的精度写进记录，迁移到共用路径后仍可回读。
        entry["旧精度位数"] = values.get("旧精度位数")
        entry["新精度位数"] = values.get("新精度位数")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"口径版本 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于口径注册可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        message = f"口径版本已{action}"
        if action == "确认生效":
            check = self.cross_check(entry)
            if check["留档批次"] is None:
                message = f"{message}；尚未完成 {metric_ledger.TASK} 核对，请先在指标定义页核对留档"
            elif not check["与留档一致"]:
                message = f"{message}；旧口径与 {metric_ledger.TASK} 留档结果不一致，已保留历史差异"
        return entry, message

    def version_conflict(self, entry_id: int, policy: str = POLICY_HISTORICAL) -> dict[str, Any] | None:
        """同一指标同时有历史/当前公式时，按策略给出选中口径与历史差异。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        definitions = store.filter_by_field("metric_def", "指标编号", entry.get("指标编号"))
        if not definitions:
            return None
        resolution = resolve_versions(definitions[0], store.rows(MODULE), policy)
        return {
            "指标编号": resolution.metric_code,
            "策略": resolution.policy,
            "选中版本": resolution.selected.as_dict(),
            "版本冲突": resolution.conflict,
            "冲突说明": resolution.conflict_reason,
            "说明": list(resolution.notes),
            "候选版本": [candidate.as_dict() for candidate in resolution.candidates],
        }
