"""调用来源管理接口：维护来源登记，覆盖启用登记、冻结来源、注销来源等动作，
并提供跨日批量结算、断点水位与配额台账的查询入口。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult, SettlementBatchRequest
from app.services.settlement import (
    SettlementConflictError,
    SettlementError,
    settlement_service,
)
from app.services.whitelist import WhitelistService

router = APIRouter(prefix="/api/whitelist", tags=["调用来源管理"])

service = WhitelistService()

LIST_FIELDS = ["登记编号", "应用名称", "调用标识", "日配额上限", "优先级", "生效日期", "失效日期", "登记状态"]
STATUSES = ["草稿", "已登记", "已冻结", "已注销"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按登记编号、应用名称或调用标识检索"),
    status: str | None = Query(default=None, description="草稿、已登记、已冻结、已注销"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按登记编号与状态过滤调用来源管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/settlements", response_model=dict)
def run_batch_settlement(payload: SettlementBatchRequest) -> dict[str, Any]:
    """跨日批量结算：多选来源登记、选定日期范围后按应用名称+调用标识分段结算。

    同一调用标识并发只允许一个批次成功（其余 409），失败可凭断点重跑且不重复扣减。
    """
    if not payload.entry_ids:
        raise HTTPException(status_code=400, detail="请至少勾选一条来源登记再发起结算")
    try:
        return settlement_service.settle_batch(
            entry_ids=payload.entry_ids,
            start_date=payload.start_date,
            end_date=payload.end_date,
        )
    except SettlementConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SettlementError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/settlements", response_model=dict)
def list_settlements() -> dict[str, Any]:
    """结算批次列表：按创建顺序返回，用于前端对账与断点排查。"""
    return {
        "module": "whitelist",
        "timezone": "UTC+8",
        "total": len(settlement_service.list_batches()),
        "items": settlement_service.list_batches(),
    }


@router.get("/settlements/{batch_id}", response_model=dict)
def get_settlement(batch_id: str) -> dict[str, Any]:
    """读取单个结算批次的逐组逐条配额结果。"""
    batch = settlement_service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"结算批次 {batch_id} 不存在")
    return batch


@router.get("/checkpoints", response_model=dict)
def list_checkpoints() -> dict[str, Any]:
    """断点水位清单：已完成位置、上一结算日与上一日配额水位一目了然。"""
    return {"items": settlement_service.list_checkpoints(), "total": len(settlement_service.list_checkpoints())}


@router.get("/quota-ledger", response_model=dict)
def list_quota_ledger(
    entry_code: str | None = Query(default=None, description="按登记编号过滤配额台账"),
) -> dict[str, Any]:
    """配额消耗台账：结合来源登记清单逐条核对每日扣减结果。"""
    items = settlement_service.list_ledger(entry_code=entry_code)
    return {"items": items, "total": len(items)}


@router.get("/usage", response_model=dict)
def list_usage(
    settled: bool | None = Query(default=None, description="只看已结算/未结算调用明细"),
) -> dict[str, Any]:
    """调用明细查询：验证每条请求的结算标记与本次扣减，防止重复扣减。"""
    items = settlement_service.list_usage(settled=settled)
    return {"items": items, "total": len(items)}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出调用来源管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "whitelist", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条来源登记明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"来源登记 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条来源登记，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="来源登记已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条来源登记执行启用登记、冻结来源、注销来源；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
