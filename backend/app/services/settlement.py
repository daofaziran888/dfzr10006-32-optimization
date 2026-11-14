"""跨日批量结算引擎。

设计口径（与需求逐条对应）：

1. 分段：按「应用名称 + 调用标识」把来源登记与调用明细分成独立结算段。
2. 断点续算：每段维护一条 checkpoint，记录已完成位置（明细最大 id）、
   上一结算日与上一日配额水位；每个业务日期处理完才推进水位，
   中途失败后重跑同一日期范围会从断点继续，已结算明细不会重复扣减。
3. 幂等：明细行自带 settled 标记，即使水位丢失，已结算请求也会被跳过，
   重复结算不会让配额消耗翻倍。
4. 并发：每个分段一把进程内互斥锁，同一调用标识的并发批次只有一个能拿到锁，
   其余批次直接失败（409），由调用方稍后从断点重试。
5. 冻结/注销立即失效：来源状态不是「已登记」时，配额消耗一律归零，
   不再占用次日额度；冻结、注销动作会同步调用本模块的失效入口并联动限流降级。
6. 时区：统一 UTC+8（Asia/Shanghai）切日，带偏移的时间戳先换算到 +08:00，
   不带时区的时间戳按 +08:00 解释。
7. 结果同步：每个业务日期结算后把配额结果 upsert 到调用限流降级（limiter），
   超限触发降级、失效来源关闭限流额度。
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.store import store

WHITELIST_MODULE = "whitelist"
USAGE_TABLE = "whitelist_usage"
CHECKPOINT_TABLE = "whitelist_checkpoint"
LEDGER_TABLE = "whitelist_quota_ledger"
BATCH_TABLE = "whitelist_settlement_batch"
LIMITER_MODULE = "limiter"

# 固定 UTC+8：不依赖系统 tzdata，容器内也能稳定切日。
SHANGHAI_TZ = timezone(timedelta(hours=8))

STATUS_ACTIVE = "已登记"
STATUS_FROZEN = "已冻结"
STATUS_REVOKED = "已注销"
INVALID_STATUSES = (STATUS_FROZEN, STATUS_REVOKED)

# 单次批量结算允许覆盖的最大日期跨度，防止误选超大范围。
MAX_DATE_SPAN_DAYS = 93


class SettlementConflictError(RuntimeError):
    """同一调用标识已有批次在结算，并发批次必须退出并从断点重试。"""


class SettlementError(RuntimeError):
    """结算过程中某一分段失败：水位未推进，可安全重跑。"""


def _group_key(app_name: str, call_key: str) -> str:
    return f"{app_name}||{call_key}"


def parse_business_day(value: str) -> date:
    """把入参日期字符串解析成业务日期，非法格式直接抛出可读错误。"""
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise SettlementError(f"日期「{value}」格式不正确，应为 YYYY-MM-DD") from exc


def to_business_day(timestamp: str) -> date:
    """请求时间戳 -> UTC+8 业务日期。

    带偏移的 ISO 时间先换算到 +08:00；不带时区的按 +08:00 解释，
    保证 UTC 零点附近的请求落到正确的自然日。
    """
    text = str(timestamp).strip()
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SettlementError(f"请求时间「{timestamp}」不是合法的 ISO 时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(SHANGHAI_TZ).date()


def _iter_days(start: date, end: date) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


class SettlementService:
    """跨日批量结算服务：分段加锁、断点水位、幂等扣减、限流联动。"""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # 测试钩子：{(应用名称, 调用标识, 业务日期)}，命中即在扣减前抛错，
        # 用来验证“失败从断点继续且不重复扣减”。生产代码不会写入它。
        self.fail_before_day: set[tuple[str, str, str]] = set()

    # ------------------------------------------------------------------ 基础查询
    def list_usage(self, *, settled: bool | None = None) -> list[dict[str, Any]]:
        rows = store.rows(USAGE_TABLE)
        if settled is not None:
            rows = [row for row in rows if bool(row.get("settled")) == settled]
        return rows

    def list_checkpoints(self) -> list[dict[str, Any]]:
        return store.rows(CHECKPOINT_TABLE)

    def list_ledger(self, *, entry_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(LEDGER_TABLE)
        if entry_code:
            rows = [row for row in rows if row.get("登记编号") == entry_code]
        return rows

    def list_batches(self) -> list[dict[str, Any]]:
        return store.rows(BATCH_TABLE)

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_TABLE):
            if row.get("批次编号") == batch_id:
                return row
        return None

    # ------------------------------------------------------------------ 并发控制
    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    # ------------------------------------------------------------------ 断点水位
    def _get_checkpoint(self, app_name: str, call_key: str) -> dict[str, Any] | None:
        for row in store.rows(CHECKPOINT_TABLE):
            if row.get("应用名称") == app_name and row.get("调用标识") == call_key:
                return row
        return None

    def _upsert_checkpoint(
        self,
        *,
        app_name: str,
        call_key: str,
        entry_code: str,
        position: int,
        last_day: str,
        watermark: int,
        invalidated: bool,
    ) -> dict[str, Any]:
        row = self._get_checkpoint(app_name, call_key)
        now = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
        if row is None:
            row = {"id": store.next_id(CHECKPOINT_TABLE)}
            store.rows(CHECKPOINT_TABLE).append(row)
        row.update({
            "应用名称": app_name,
            "调用标识": call_key,
            "登记编号": entry_code,
            "已完成位置": position,
            "上一结算日": last_day,
            "上一日配额水位": watermark,
            "最后结算时间": now,
            "已失效": invalidated,
        })
        return row

    # ------------------------------------------------- 冻结/注销立即失效入口
    def invalidate_entry(self, entry: dict[str, Any], reason: str) -> None:
        """冻结或注销来源时调用：立即退出次日额度并同步限流降级。

        已推进的断点水位保留用于审计，但标记为已失效，后续批次不再扣减。
        """
        app_name = str(entry.get("应用名称") or "")
        call_key = str(entry.get("调用标识") or "")
        entry_code = str(entry.get("登记编号") or "")
        checkpoint = self._get_checkpoint(app_name, call_key)
        watermark = int(checkpoint.get("上一日配额水位", 0)) if checkpoint else 0
        self._upsert_checkpoint(
            app_name=app_name,
            call_key=call_key,
            entry_code=entry_code,
            position=int(checkpoint.get("已完成位置", 0)) if checkpoint else 0,
            last_day=str(checkpoint.get("上一结算日") or "") if checkpoint else "",
            watermark=watermark,
            invalidated=True,
        )
        self._sync_limiter(
            entry=entry,
            business_day="",
            quota_used=0,
            remaining=0,
            requested_total=0,
            over_limit=False,
            reason=reason,
        )

    # ------------------------------------------------------------------ 限流联动
    def _sync_limiter(
        self,
        *,
        entry: dict[str, Any],
        business_day: str,
        quota_used: int,
        remaining: int,
        requested_total: int,
        over_limit: bool,
        reason: str,
    ) -> dict[str, Any]:
        """把配额结算结果 upsert 到调用限流降级模块。

        匹配键为调用标识（限流对象），同一条来源登记只维护一条限流规则，
        每次结算覆盖最新的配额水位，而不是新增重复规则。
        """
        call_key = str(entry.get("调用标识") or "")
        entry_code = str(entry.get("登记编号") or "")
        target: dict[str, Any] | None = None
        for row in store.rows(LIMITER_MODULE):
            if row.get("限流对象") == call_key:
                target = row
                break

        status = str(entry.get("status") or "")
        if status in INVALID_STATUSES:
            rule_status = "已关闭"
            degrade_action = "停止放行并关闭次日额度"
            pending = False
        elif over_limit:
            rule_status = "已触发"
            degrade_action = "超限请求快速失败"
            pending = True
        else:
            rule_status = "生效中"
            degrade_action = "正常放行"
            pending = True

        now_text = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
        if target is None:
            target = {"id": store.next_id(LIMITER_MODULE)}
            store.rows(LIMITER_MODULE).append(target)
        target.update({
            "规则编号": target.get("规则编号") or f"LIMI-SYNC-{entry_code}",
            "限流对象": call_key,
            "限流算法": "来源日配额",
            "每秒上限": entry.get("日配额上限"),
            "突发容量": requested_total,
            "降级动作": degrade_action,
            "恢复时间": business_day or now_text[:10],
            "规则状态": rule_status,
            "status": rule_status,
            "pending": pending,
            "abnormal": bool(over_limit) or status in INVALID_STATUSES,
            "来源登记编号": entry_code,
            "业务日期": business_day,
            "配额消耗": quota_used,
            "剩余额度": remaining,
            "是否超限": over_limit,
            "同步时间": now_text,
            "同步说明": reason,
        })
        return target

    # ------------------------------------------------------------- 单段单日结算
    def _settle_one_day(
        self,
        *,
        entry: dict[str, Any],
        app_name: str,
        call_key: str,
        business_day: date,
        checkpoint: dict[str, Any] | None,
        batch_ref: str,
    ) -> dict[str, Any] | None:
        """结算一个分段在一个业务日期内的全部新请求。

        扣减与水位推进在同一步完成：先把当日所有未结算明细筛出并逐条标记，
        再写台账、推进断点；任何一步抛错都不会推进水位，重跑时靠
        settled 标记保证不重复扣减。
        """
        day_text = business_day.isoformat()
        entry_code = str(entry.get("登记编号") or "")
        status = str(entry.get("status") or "")

        if (app_name, call_key, day_text) in self.fail_before_day:
            raise SettlementError(
                f"分段 {app_name}/{call_key} 在 {day_text} 注入失败，水位保持断点"
            )

        position = int(checkpoint.get("已完成位置", 0)) if checkpoint else 0
        day_rows: list[dict[str, Any]] = []
        for row in store.rows(USAGE_TABLE):
            if row.get("应用名称") != app_name or row.get("调用标识") != call_key:
                continue
            if bool(row.get("settled")):
                continue  # 幂等底座：已结算明细绝不二次扣减
            if to_business_day(str(row.get("请求时间"))) != business_day:
                continue
            day_rows.append(row)
        day_rows.sort(key=lambda item: int(item.get("id", 0)))

        # 当天没有新进入的请求：不落台账、不动水位，重跑时自然再次跳过。
        if not day_rows:
            return None

        requested_total = sum(int(item.get("请求量", 0)) for item in day_rows)
        daily_cap_raw = entry.get("日配额上限")
        try:
            daily_cap = int(daily_cap_raw)
        except (TypeError, ValueError):
            daily_cap = 1000

        if status == STATUS_ACTIVE:
            quota_used = requested_total
            over_limit = quota_used > daily_cap
            remaining = max(daily_cap - quota_used, 0)
            reason = "有效来源按日配额扣减一次"
            if over_limit:
                reason = "当日请求超过日配额上限，超限部分触发限流降级"
        else:
            # 冻结/注销立即失效：请求仍逐条登记结果，但配额消耗归零。
            quota_used = 0
            over_limit = False
            remaining = 0
            reason = f"来源状态为{status}，配额消耗归零并退出次日额度"

        now_text = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")

        # 先逐条标记明细（幂等键），再落台账与水位。
        # 每条明细记录自身的本次扣减量（有效来源=该请求量，失效来源=0），
        # 台账上的配额消耗才是当日汇总，避免逐条汇总时重复计算。
        for row in day_rows:
            row_deduction = int(row.get("请求量", 0)) if status == STATUS_ACTIVE else 0
            row["settled"] = True
            row["结算批次"] = batch_ref
            row["结算时间"] = now_text
            row["本次扣减"] = row_deduction
            row["结算说明"] = reason
            position = max(position, int(row.get("id", 0)))

        ledger_row = {
            "id": store.next_id(LEDGER_TABLE),
            "登记编号": entry_code,
            "应用名称": app_name,
            "调用标识": call_key,
            "业务日期": day_text,
            "日配额上限": daily_cap,
            "请求总量": requested_total,
            "配额消耗": quota_used,
            "剩余额度": remaining,
            "是否超限": over_limit,
            "来源状态": status,
            "结算批次": batch_ref,
            "结算时间": now_text,
            "明细条数": len(day_rows),
        }
        store.rows(LEDGER_TABLE).append(ledger_row)

        self._upsert_checkpoint(
            app_name=app_name,
            call_key=call_key,
            entry_code=entry_code,
            position=position,
            last_day=day_text,
            watermark=quota_used,
            invalidated=status in INVALID_STATUSES,
        )
        self._sync_limiter(
            entry=entry,
            business_day=day_text,
            quota_used=quota_used,
            remaining=remaining,
            requested_total=requested_total,
            over_limit=over_limit,
            reason=reason,
        )

        return {
            "业务日期": day_text,
            "请求量": requested_total,
            "配额消耗": quota_used,
            "剩余额度": remaining,
            "是否超限": over_limit,
            "来源状态": status,
            "明细条数": len(day_rows),
            "说明": reason,
        }

    # ------------------------------------------------------------------ 批量入口
    def settle_batch(
        self,
        *,
        entry_ids: list[int] | None = None,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        """对多选来源登记在日期范围内逐组结算，返回逐组逐条配额结果。"""
        start = parse_business_day(start_date)
        end = parse_business_day(end_date)
        if start > end:
            raise SettlementError("开始日期不能晚于结束日期")
        if (end - start).days + 1 > MAX_DATE_SPAN_DAYS:
            raise SettlementError(f"日期范围最多覆盖 {MAX_DATE_SPAN_DAYS} 天，请缩小范围")

        entries = store.rows(WHITELIST_MODULE)
        if entry_ids:
            wanted = set(entry_ids)
            entries = [row for row in entries if int(row.get("id", 0)) in wanted]
        if not entries:
            raise SettlementError("没有选中任何来源登记，无法发起批量结算")

        # 同应用同调用标识视为同一分段，去重后逐组处理。
        groups: dict[str, dict[str, Any]] = {}
        for entry in entries:
            key = _group_key(str(entry.get("应用名称") or ""), str(entry.get("调用标识") or ""))
            groups.setdefault(key, entry)

        batch_id = "BATCH-" + datetime.now(SHANGHAI_TZ).strftime("%Y%m%d%H%M%S%f")
        batch_row: dict[str, Any] = {
            "批次编号": batch_id,
            "开始日期": start.isoformat(),
            "结束日期": end.isoformat(),
            "时区口径": "UTC+8",
            "状态": "处理中",
            "创建时间": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
            "完成时间": None,
            "分组结果": [],
        }
        store.rows(BATCH_TABLE).append(batch_row)

        days = _iter_days(start, end)
        conflict_groups: list[dict[str, str]] = []
        for key, entry in groups.items():
            app_name = str(entry.get("应用名称") or "")
            call_key = str(entry.get("调用标识") or "")
            lock = self._lock_for(key)
            if not lock.acquire(blocking=False):
                conflict_groups.append({
                    "应用名称": app_name,
                    "调用标识": call_key,
                    "登记编号": str(entry.get("登记编号") or ""),
                })
                continue
            try:
                group_result = self._settle_group(
                    entry=entry,
                    app_name=app_name,
                    call_key=call_key,
                    days=days,
                    batch_ref=batch_id,
                )
                batch_row["分组结果"].append(group_result)
            finally:
                lock.release()

        if conflict_groups:
            batch_row["状态"] = "冲突"
            batch_row["冲突分组"] = conflict_groups
            batch_row["完成时间"] = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
            labels = "、".join(f"{item['应用名称']}/{item['调用标识']}" for item in conflict_groups)
            raise SettlementConflictError(
                f"同一调用标识已有结算批次在执行：{labels}，请稍后从断点重试"
            )

        # 任一分组失败都标记部分成功：已完成分组的扣减有效，失败分组保留断点等待重跑。
        any_failed = any(not group["成功"] for group in batch_row["分组结果"])
        batch_row["状态"] = "部分成功" if any_failed else "成功"
        batch_row["完成时间"] = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
        return self.get_batch(batch_id) or batch_row

    def _settle_group(
        self,
        *,
        entry: dict[str, Any],
        app_name: str,
        call_key: str,
        days: list[date],
        batch_ref: str,
    ) -> dict[str, Any]:
        """单个分段在整段日期范围内按日推进断点。"""
        checkpoint = self._get_checkpoint(app_name, call_key)
        invalidated = bool(checkpoint and checkpoint.get("已失效"))
        status = str(entry.get("status") or "")

        processed_days: list[str] = []
        skipped_days: list[str] = []
        quota_results: list[dict[str, Any]] = []
        failure: str | None = None

        # 失效分段（冻结/注销后）不再扣减，但仍对新日期登记零消耗结果，
        # 保证来源登记清单与配额台账可逐组逐条核对。
        for business_day in days:
            day_text = business_day.isoformat()
            if checkpoint and checkpoint.get("上一结算日") and day_text <= str(checkpoint["上一结算日"]):
                skipped_days.append(day_text)
                continue
            try:
                result = self._settle_one_day(
                    entry=entry,
                    app_name=app_name,
                    call_key=call_key,
                    business_day=business_day,
                    checkpoint=self._get_checkpoint(app_name, call_key),
                    batch_ref=batch_ref,
                )
            except SettlementError as exc:
                failure = str(exc)
                break
            if result is None:
                # 当天无新请求：既不算处理也不算跳过，留待后续批次自然覆盖。
                continue
            quota_results.append(result)
            processed_days.append(day_text)
            checkpoint = self._get_checkpoint(app_name, call_key)

        return {
            "应用名称": app_name,
            "调用标识": call_key,
            "登记编号": str(entry.get("登记编号") or ""),
            "来源状态": status,
            "已失效": invalidated or status in INVALID_STATUSES,
            "成功": failure is None,
            "失败原因": failure,
            "处理日期": processed_days,
            "跳过日期": skipped_days,
            "配额结果": quota_results,
        }


settlement_service = SettlementService()
