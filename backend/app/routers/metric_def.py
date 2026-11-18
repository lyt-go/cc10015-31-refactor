"""指标定义接口：维护指标定义，覆盖提交校验、标记生效、标记废弃等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import metric_ledger
from app.services.metric_def import MetricDefService
from app.services.metric_formula import POLICY_HISTORICAL

router = APIRouter(prefix="/api/metric_def", tags=["指标定义"])

service = MetricDefService()

LIST_FIELDS = ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "基准取值", "精度位数", "口径版本"]
STATUSES = ["草稿", "待校验", "已生效", "已废弃"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按指标编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已生效、已废弃"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、指标编号关键字与状态过滤指标定义列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="metric_def.csv"'})


@router.get("/confirmation", response_model=dict)
def get_confirmation(metric_code: str | None = Query(default=None)) -> dict:
    """读取 V031 留档：列表页与口径注册消费端都从这里取核对结果。"""
    return metric_ledger.list_checks(metric_code)


@router.get("/confirmation/history", response_model=dict)
def get_confirmation_history(metric_code: str | None = Query(default=None)) -> dict:
    """回查全部留档批次，历史公式解析结果与当前结果都可追溯。"""
    items = metric_ledger.history(metric_code)
    return {"task": metric_ledger.TASK, "items": items, "total": len(items)}


@router.post("/confirmation", response_model=dict)
def confirm_versions(policy: str = Query(default=POLICY_HISTORICAL, description="历史优先、按版本时间")) -> dict:
    """按 V031 核对全部指标定义公式版本并整批留档；失败整批回滚。"""
    try:
        return service.confirm_versions(policy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{entry_id}/detail", response_model=dict)
def get_entry_detail(entry_id: int) -> dict:
    """指标定义详情：原始字段、统一计算路径结果与 V031 留档核对。"""
    detail = service.get_detail(entry_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"指标定义 {entry_id} 不存在或已归档")
    return detail


@router.get("/{entry_id}/check", response_model=dict)
def check_entry(
    entry_id: int,
    policy: str = Query(default=POLICY_HISTORICAL, description="历史优先、按版本时间"),
) -> dict:
    """按共用公式路径复算单条指标定义，返回历史/当前口径差异。"""
    try:
        result = service.check_entry(entry_id, policy=policy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"指标定义 {entry_id} 不存在或已归档")
    return result


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条指标定义明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"指标定义 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条指标定义，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="指标定义已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条指标定义执行提交校验、标记生效、标记废弃；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
