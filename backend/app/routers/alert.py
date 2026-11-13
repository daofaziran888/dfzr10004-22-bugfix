"""采集告警中心接口：维护告警规则，覆盖校验、启停、暂停/恢复升级与处置记录。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.alert import AlertService

router = APIRouter(prefix="/api/alert", tags=["采集告警中心"])

service = AlertService()

LIST_FIELDS = ["规则编号", "监控指标", "告警级别", "触发条件", "通知渠道", "静默时间", "升级策略", "是否启用"]
STATUSES = ["草稿", "待校验", "已启用", "已停用", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号或监控指标检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤采集告警中心列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集告警中心清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "alert", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条告警规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"告警规则 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/history")
def get_history(entry_id: int) -> dict[str, Any]:
    """读取告警规则的完整状态流转历史；详情页与列表页使用同一份记录。"""
    history = service.get_history(entry_id)
    if history is None:
        raise HTTPException(status_code=404, detail=f"告警规则 {entry_id} 不存在或已归档")
    return {"entry_id": entry_id, "items": history, "total": len(history)}


@router.get("/{entry_id}/disposals")
def get_disposals(entry_id: int) -> dict[str, Any]:
    """读取告警规则的全部处置记录；切换处理窗口后历史仍然保留。"""
    disposals = service.get_disposals(entry_id)
    if disposals is None:
        raise HTTPException(status_code=404, detail=f"告警规则 {entry_id} 不存在或已归档")
    return {"entry_id": entry_id, "items": disposals, "total": len(disposals)}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条告警规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.merged())
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="告警规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条告警规则执行校验、启用、停用、暂停/恢复与废弃；非法流转会被拦下。"""
    action = str(payload.merged().get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/events", response_model=ActionResult)
def report_event(entry_id: int, payload: EntryPayload) -> ActionResult:
    """上报一次异常事件：只有已启用且窗口开启时才累计升级次数。"""
    entry, message = service.report_event(entry_id, payload.merged())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/disposals", response_model=ActionResult)
def add_disposal(entry_id: int, payload: EntryPayload) -> ActionResult:
    """登记一条人工处置记录，处置历史只追加、不清空。"""
    entry, message = service.add_disposal(entry_id, payload.merged())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
