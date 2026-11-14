"""跨日批量结算验收用例（标准库 unittest，不依赖第三方测试框架）。

覆盖需求点：
1. 多选来源登记 + 日期范围逐组结算，逐条返回配额结果；
2. WHIT-0001/2/3 分别为有效、冻结、注销，有效来源只扣减一次，冻结/注销归零；
3. 断点水位（已完成位置、上一结算日、上一日配额水位）推进正确；
4. 重复结算幂等，配额消耗不翻倍；
5. 中途失败后从断点继续，已结算明细不重复扣减；
6. 同一调用标识并发只允许一个批次成功，其余冲突失败；
7. 冻结/注销立即失效并退出次日额度，结果同步到调用限流降级；
8. UTC+8 时区切日：UTC 16:30 的请求计入 +08:00 次日。
"""
from __future__ import annotations

import threading
import unittest

from app.services.settlement import (
    SettlementConflictError,
    SettlementService,
    to_business_day,
)
from app.store import store


class SettlementTestCase(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.service = SettlementService()

    def _entry(self, code: str) -> dict:
        for row in store.rows("whitelist"):
            if row.get("登记编号") == code:
                return row
        raise AssertionError(f"种子数据缺少 {code}")

    def _ledger_map(self) -> dict[tuple[str, str], dict]:
        return {(row["登记编号"], row["业务日期"]): row for row in store.rows("whitelist_quota_ledger")}

    # ------------------------------------------------------------ 时区口径
    def test_timezone_utc_plus_8_cross_midnight(self) -> None:
        # UTC 11-10 16:30 == UTC+8 11-11 00:30
        self.assertEqual(str(to_business_day("2026-11-10T16:30:00+00:00")), "2026-11-11")
        # 不带时区的时间戳按 UTC+8 解释
        self.assertEqual(str(to_business_day("2026-11-11T00:30:00")), "2026-11-11")
        self.assertEqual(str(to_business_day("2026-11-10T09:05:00+08:00")), "2026-11-10")

    # ------------------------------------------------- 主流程：三状态来源批量结算
    def test_batch_settlement_active_deducted_once_frozen_revoked_zero(self) -> None:
        result = self.service.settle_batch(
            entry_ids=[1, 2, 3], start_date="2026-11-10", end_date="2026-11-12"
        )
        self.assertEqual(result["状态"], "成功")
        self.assertEqual(len(result["分组结果"]), 3)

        by_code = {group["登记编号"]: group for group in result["分组结果"]}
        active = by_code["WHIT-0001"]
        frozen = by_code["WHIT-0002"]
        revoked = by_code["WHIT-0003"]

        # 有效来源：11-10=460（200+260，UTC 零点那条算到 11-11），11-11=440，11-12=270
        active_by_day = {item["业务日期"]: item for item in active["配额结果"]}
        self.assertEqual(active_by_day["2026-11-10"]["配额消耗"], 460)
        self.assertEqual(active_by_day["2026-11-10"]["明细条数"], 2)
        self.assertEqual(active_by_day["2026-11-11"]["配额消耗"], 440)
        self.assertEqual(active_by_day["2026-11-11"]["明细条数"], 2)
        self.assertEqual(active_by_day["2026-11-12"]["配额消耗"], 270)
        self.assertEqual(active_by_day["2026-11-12"]["明细条数"], 2)
        self.assertEqual(active_by_day["2026-11-12"]["剩余额度"], 730)

        # 冻结/注销：有请求的日期保留可审计记录，但配额消耗一律归零
        frozen_days = {item["业务日期"] for item in frozen["配额结果"]}
        revoked_days = {item["业务日期"] for item in revoked["配额结果"]}
        self.assertEqual(frozen_days, {"2026-11-10", "2026-11-11", "2026-11-12"})
        self.assertEqual(revoked_days, {"2026-11-10", "2026-11-12"})
        for group in (frozen, revoked):
            self.assertTrue(group["已失效"])
            for item in group["配额结果"]:
                self.assertEqual(item["配额消耗"], 0)
                self.assertEqual(item["剩余额度"], 0)
                self.assertGreater(item["请求量"], 0)

        ledger = self._ledger_map()
        # 种子已结算日 1 条 + 有效来源 3 条 + 冻结 3 条 + 注销 2 条（无请求的日期不落台账）
        self.assertEqual(len(ledger), 1 + 3 + 3 + 2)
        self.assertEqual(ledger[("WHIT-0002", "2026-11-10")]["配额消耗"], 0)
        self.assertEqual(ledger[("WHIT-0003", "2026-11-12")]["配额消耗"], 0)

    # ------------------------------------------------------------ 幂等：重复结算
    def test_repeat_settlement_is_idempotent(self) -> None:
        self.service.settle_batch(entry_ids=[1], start_date="2026-11-10", end_date="2026-11-12")
        first_ledger = [dict(row) for row in store.rows("whitelist_quota_ledger")]

        # 再跑一次同样的范围：所有日期都不晚于断点日，应全部跳过，不新增台账
        result = self.service.settle_batch(
            entry_ids=[1], start_date="2026-11-10", end_date="2026-11-12"
        )
        group = result["分组结果"][0]
        self.assertEqual(group["处理日期"], [])
        self.assertEqual(group["跳过日期"], ["2026-11-10", "2026-11-11", "2026-11-12"])
        self.assertEqual(len(store.rows("whitelist_quota_ledger")), len(first_ledger))

        # 已结算明细的 settled 标记与扣减额没有被改写
        for row in store.rows("whitelist_usage"):
            if row.get("登记编号") == "WHIT-0001":
                self.assertTrue(row["settled"])
        total_used = sum(
            row["本次扣减"]
            for row in store.rows("whitelist_usage")
            if row.get("登记编号") == "WHIT-0001"
        )
        # 300（种子已结算日）+ 460 + 440 + 270 = 1470，而不是翻倍的 2940
        self.assertEqual(total_used, 1470)

    # ------------------------------------------------------------ 断点续算
    def test_resume_from_checkpoint_without_double_deduction(self) -> None:
        # 在处理 11-11 前注入失败：11-10 已落账并推进水位，11-11/11-12 未处理
        self.service.fail_before_day.add(
            ("实时结算演示应用", "app-key-active", "2026-11-11")
        )
        result = self.service.settle_batch(
            entry_ids=[1], start_date="2026-11-10", end_date="2026-11-12"
        )
        group = result["分组结果"][0]
        self.assertFalse(group["成功"])
        self.assertEqual(group["处理日期"], ["2026-11-10"])
        self.assertIn("注入失败", group["失败原因"])

        checkpoint = next(
            row for row in store.rows("whitelist_checkpoint")
            if row["调用标识"] == "app-key-active"
        )
        self.assertEqual(checkpoint["上一结算日"], "2026-11-10")
        self.assertEqual(checkpoint["上一日配额水位"], 460)
        self.assertEqual(checkpoint["已完成位置"], 3)

        # 移除故障后重跑同一范围：11-10 跳过，11-11/11-12 从断点继续
        self.service.fail_before_day.clear()
        resumed = self.service.settle_batch(
            entry_ids=[1], start_date="2026-11-10", end_date="2026-11-12"
        )
        group2 = resumed["分组结果"][0]
        self.assertTrue(group2["成功"])
        self.assertEqual(group2["跳过日期"], ["2026-11-10"])
        self.assertEqual(group2["处理日期"], ["2026-11-11", "2026-11-12"])

        ledger = self._ledger_map()
        self.assertEqual(ledger[("WHIT-0001", "2026-11-10")]["配额消耗"], 460)
        self.assertEqual(ledger[("WHIT-0001", "2026-11-11")]["配额消耗"], 440)
        self.assertEqual(ledger[("WHIT-0001", "2026-11-12")]["配额消耗"], 270)
        # 11-10 只落过一次账
        day_1110 = [
            row for row in store.rows("whitelist_quota_ledger")
            if row["登记编号"] == "WHIT-0001" and row["业务日期"] == "2026-11-10"
        ]
        self.assertEqual(len(day_1110), 1)

    # ------------------------------------------------------------ 并发互斥
    def test_concurrent_same_call_key_only_one_batch_succeeds(self) -> None:
        # 生产环境只有 settlement_service 一个单例，分段锁在单例内按调用标识互斥；
        # 这里用同一实例的两个线程模拟并发批次。
        holder_acquired = threading.Event()
        release = threading.Event()
        outcomes: list[str] = []

        def hold_lock() -> None:
            lock = self.service._lock_for("实时结算演示应用||app-key-active")
            lock.acquire()
            holder_acquired.set()
            release.wait(timeout=5)
            lock.release()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        self.assertTrue(holder_acquired.wait(timeout=5))

        def conflicting_batch() -> None:
            try:
                self.service.settle_batch(
                    entry_ids=[1], start_date="2026-11-10", end_date="2026-11-10"
                )
                outcomes.append("ok")
            except SettlementConflictError:
                outcomes.append("conflict")

        worker = threading.Thread(target=conflicting_batch)
        worker.start()
        worker.join(timeout=5)
        release.set()
        holder.join(timeout=5)

        self.assertEqual(outcomes, ["conflict"])
        # 冲突批次没有留下任何扣减
        self.assertNotIn(
            ("WHIT-0001", "2026-11-10"),
            {(row["登记编号"], row["业务日期"]) for row in store.rows("whitelist_quota_ledger")},
        )

        # 锁释放后新批次可以正常成功
        ok = self.service.settle_batch(entry_ids=[1], start_date="2026-11-10", end_date="2026-11-10")
        self.assertEqual(ok["状态"], "成功")

    # --------------------------------------------- 冻结/注销立即失效并联动限流
    def test_freeze_invalidates_quota_and_syncs_limiter(self) -> None:
        # 先结算 11-10，再冻结 WHIT-0001，随后结算 11-11：新日期必须归零
        self.service.settle_batch(entry_ids=[1], start_date="2026-11-10", end_date="2026-11-10")
        entry = self._entry("WHIT-0001")
        entry["status"] = "已冻结"
        entry["登记状态"] = "已冻结"
        self.service.invalidate_entry(entry, reason="测试冻结")

        checkpoint = next(
            row for row in store.rows("whitelist_checkpoint")
            if row["调用标识"] == "app-key-active"
        )
        self.assertTrue(checkpoint["已失效"])

        result = self.service.settle_batch(entry_ids=[1], start_date="2026-11-11", end_date="2026-11-12")
        for item in result["分组结果"][0]["配额结果"]:
            self.assertEqual(item["配额消耗"], 0)
            self.assertEqual(item["来源状态"], "已冻结")

        limiter_row = next(
            row for row in store.rows("limiter") if row.get("限流对象") == "app-key-active"
        )
        self.assertEqual(limiter_row["规则状态"], "已关闭")
        self.assertEqual(limiter_row["配额消耗"], 0)
        self.assertEqual(limiter_row["剩余额度"], 0)
        self.assertIn("关闭次日额度", limiter_row["降级动作"])

    def test_limiter_sync_marks_over_limit_degradation(self) -> None:
        # 把有效来源的日配额上限调到低于当日请求量（460），应触发超限降级
        entry = self._entry("WHIT-0001")
        entry["日配额上限"] = 400
        result = self.service.settle_batch(
            entry_ids=[1], start_date="2026-11-10", end_date="2026-11-10"
        )
        item = result["分组结果"][0]["配额结果"][0]
        self.assertTrue(item["是否超限"])
        limiter_row = next(
            row for row in store.rows("limiter") if row.get("限流对象") == "app-key-active"
        )
        self.assertEqual(limiter_row["规则状态"], "已触发")
        self.assertEqual(limiter_row["配额消耗"], 460)
        self.assertEqual(limiter_row["剩余额度"], 0)
        self.assertTrue(limiter_row["是否超限"])

    # ------------------------------------------------------------ 入参校验
    def test_invalid_date_range_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.service.settle_batch(entry_ids=[1], start_date="2026-11-12", end_date="2026-11-10")
        with self.assertRaises(Exception):
            self.service.settle_batch(entry_ids=[1], start_date="bad-date", end_date="2026-11-10")

    # ------------------------------------- 不同调用标识的批次互不阻塞，可并行成功
    def test_parallel_batches_on_different_call_keys_both_succeed(self) -> None:
        outcomes: list[str] = []

        def run(entry_id: int) -> None:
            try:
                result = self.service.settle_batch(
                    entry_ids=[entry_id], start_date="2026-11-10", end_date="2026-11-10"
                )
                outcomes.append(result["状态"])
            except SettlementConflictError:
                outcomes.append("conflict")

        threads = [
            threading.Thread(target=run, args=(1,)),
            threading.Thread(target=run, args=(2,)),
            threading.Thread(target=run, args=(3,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(sorted(outcomes), ["成功", "成功", "成功"])

    # ------------------------------------- 状态机：非法流转被拦截，冻结联动失效
    def test_whitelist_action_preconditions_and_freeze_invalidation(self) -> None:
        from app.services.whitelist import WhitelistService

        whitelist_service = WhitelistService()
        # WHIT-0001 已登记：启用登记是非法流转
        entry, message = whitelist_service.run_action(1, "启用登记")
        self.assertIsNone(entry)
        self.assertIn("已登记", message)

        # WHIT-0002 已冻结：冻结来源是重复流转，应拦截
        entry, message = whitelist_service.run_action(2, "冻结来源")
        self.assertIsNone(entry)
        self.assertIn("已冻结", message)

        # WHIT-0002 已冻结 -> 注销来源合法，且立即同步限流失效
        entry, message = whitelist_service.run_action(2, "注销来源")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "已注销")
        self.assertEqual(entry["登记状态"], "已注销")
        limiter_row = next(
            row for row in store.rows("limiter") if row.get("限流对象") == "app-key-frozen"
        )
        self.assertEqual(limiter_row["规则状态"], "已关闭")
        checkpoint = next(
            row for row in store.rows("whitelist_checkpoint")
            if row["调用标识"] == "app-key-frozen"
        )
        self.assertTrue(checkpoint["已失效"])

    # ------------------------------------- 限流降级状态机：非法流转拦截 + 同步规则可关闭
    def test_limiter_action_preconditions(self) -> None:
        from app.services.limiter import LimiterService

        limiter_service = LimiterService()
        # 种子数据 id=1 是草稿：启用限流合法
        entry, _ = limiter_service.run_action(1, "启用限流")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "生效中")
        self.assertEqual(entry["规则状态"], "生效中")
        # 重复启用应被拦截
        entry, message = limiter_service.run_action(1, "启用限流")
        self.assertIsNone(entry)
        self.assertIn("生效中", message)
        # 生效中 -> 触发降级 -> 关闭限流
        entry, _ = limiter_service.run_action(1, "触发降级")
        self.assertEqual(entry["status"], "已触发")
        entry, _ = limiter_service.run_action(1, "关闭限流")
        self.assertEqual(entry["status"], "已关闭")
        self.assertFalse(entry["pending"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
