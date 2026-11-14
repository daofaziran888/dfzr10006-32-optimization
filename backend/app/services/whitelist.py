"""调用来源管理业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.settlement import settlement_service
from app.store import store

MODULE = "whitelist"
REQUIRED_FIELDS = ["登记编号", "应用名称", "调用标识"]
OPTIONAL_FIELDS = ["日配额上限", "优先级", "生效日期", "失效日期"]
STATUS_ORDER = ["草稿", "已登记", "已冻结", "已注销"]
# 动作 -> 目标状态：启用登记把草稿推进到已登记，冻结/注销依次推进。
ACTION_RULES = {"启用登记": "已登记", "冻结来源": "已冻结", "注销来源": "已注销"}
# 动作前置状态：不满足时拒绝，避免冻结草稿或重复冻结这类非法流转。
ACTION_PRECONDITIONS = {
    "启用登记": "草稿",
    "冻结来源": "已登记",
    "注销来源": "已冻结",
}
NEGATIVE_ACTIONS = ["冻结来源", "注销来源"]
# 冻结或注销后必须立即退出次日额度的状态。
INVALID_STATUSES = ["已冻结", "已注销"]


class WhitelistService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("登记编号", ""))
                or keyword in str(row.get("应用名称", ""))
                or keyword in str(row.get("调用标识", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            if values.get(field) is not None:
                entry[field] = values.get(field)
        entry.setdefault("日配额上限", 1000)
        entry["status"] = STATUS_ORDER[0]
        entry["登记状态"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"来源登记 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于调用来源管理可执行范围"
        target = ACTION_RULES[action]
        precondition = ACTION_PRECONDITIONS[action]
        current = str(entry.get("status") or "")
        if current != precondition:
            return None, f"来源当前为「{current}」，仅「{precondition}」状态可执行{action}"
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["登记状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        # 冻结/注销立即失效：断点标记失效、退出次日额度并同步调用限流降级。
        if target in INVALID_STATUSES:
            settlement_service.invalidate_entry(entry, reason=f"来源已{target}，立即退出次日额度")
            return entry, f"来源登记已{action}，配额立即失效并已同步限流降级"
        return entry, f"来源登记已{action}"
