"""端到端验收：启动真实 uvicorn 进程，走 HTTP 验证跨日批量结算全链路。

只依赖标准库，放在独立脚本目录里运行：
    python3 scripts/e2e_check.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
BASE = "http://127.0.0.1:8011"


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ✓ {message}")


def main() -> int:
    server = subprocess.Popen(
        [str(BACKEND_DIR / ".venv/bin/python"), "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", "8011"],
        cwd=BACKEND_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        for _ in range(50):
            try:
                status, body = call("GET", "/api/health")
                if status == 200:
                    break
            except OSError:
                time.sleep(0.2)
        else:
            out = server.stdout.read() if server.stdout else ""
            raise RuntimeError(f"后端未启动：{out[-2000:]}")

        print("1) 来源登记清单核对 WHIT-0001/2/3 状态")
        status, body = call("GET", "/api/whitelist?size=200")
        entries = {row["登记编号"]: row for row in body["items"]}
        assert_true(entries["WHIT-0001"]["status"] == "已登记", "WHIT-0001 为有效（已登记）来源")
        assert_true(entries["WHIT-0002"]["status"] == "已冻结", "WHIT-0002 为冻结来源")
        assert_true(entries["WHIT-0003"]["status"] == "已注销", "WHIT-0003 为注销来源")
        ids = [entries[code]["id"] for code in ("WHIT-0001", "WHIT-0002", "WHIT-0003")]

        print("2) 跨日批量结算 2026-11-10 ~ 2026-11-12")
        status, body = call("POST", "/api/whitelist/settlements", {
            "entry_ids": ids, "start_date": "2026-11-10", "end_date": "2026-11-12",
        })
        assert_true(status == 200, f"批量结算返回 200（实际 {status}：{body}）")
        assert_true(body["时区口径"] == "UTC+8", "批次时区口径为 UTC+8")
        groups = {g["登记编号"]: g for g in body["分组结果"]}
        active = groups["WHIT-0001"]
        active_days = {item["业务日期"]: item for item in active["配额结果"]}
        assert_true(active_days["2026-11-10"]["配额消耗"] == 460, "WHIT-0001 11-10 扣减 460（UTC 零点请求计入次日）")
        assert_true(active_days["2026-11-11"]["配额消耗"] == 440, "WHIT-0001 11-11 扣减 440")
        assert_true(active_days["2026-11-12"]["配额消耗"] == 270, "WHIT-0001 11-12 扣减 270")
        for code in ("WHIT-0002", "WHIT-0003"):
            zeros = {item["业务日期"]: item["配额消耗"] for item in groups[code]["配额结果"]}
            assert_true(all(value == 0 for value in zeros.values()), f"{code} 配额消耗全部归零：{zeros}")

        print("3) 重复结算幂等：同范围再跑一次，全部跳过且台账不增加")
        status, before = call("GET", "/api/whitelist/quota-ledger")
        status, body = call("POST", "/api/whitelist/settlements", {
            "entry_ids": ids, "start_date": "2026-11-10", "end_date": "2026-11-12",
        })
        assert_true(status == 200 and all(g["处理日期"] == [] for g in body["分组结果"]), "重复批次无新增处理日期")
        status, after = call("GET", "/api/whitelist/quota-ledger")
        assert_true(before["total"] == after["total"], f"台账条数不变（{before['total']} 条）")

        print("4) 断点水位查询")
        status, body = call("GET", "/api/whitelist/checkpoints")
        cp = {row["调用标识"]: row for row in body["items"]}
        assert_true(cp["app-key-active"]["上一结算日"] == "2026-11-12", "有效来源断点停在 11-12")
        assert_true(cp["app-key-active"]["上一日配额水位"] == 270, "上一日配额水位为 270")
        assert_true(cp["app-key-frozen"]["已失效"] is True, "冻结来源断点标记已失效")
        assert_true(cp["app-key-revoked"]["已失效"] is True, "注销来源断点标记已失效")

        print("5) 调用明细：有效来源每条只扣一次，失效来源扣减为 0")
        status, body = call("GET", "/api/whitelist/usage")
        active_used = sum(r["本次扣减"] for r in body["items"] if r["登记编号"] == "WHIT-0001")
        frozen_used = sum(r["本次扣减"] for r in body["items"] if r["登记编号"] == "WHIT-0002")
        revoked_used = sum(r["本次扣减"] for r in body["items"] if r["登记编号"] == "WHIT-0003")
        assert_true(active_used == 1470, f"WHIT-0001 累计扣减 1470（实际 {active_used}），未翻倍")
        assert_true(frozen_used == 0 and revoked_used == 0, "冻结/注销来源累计扣减为 0")
        assert_true(all(r["settled"] for r in body["items"]), "全部明细已结算")

        print("6) 限流降级同步结果")
        status, body = call("GET", "/api/limiter?size=200")
        limiter = {row["限流对象"]: row for row in body["items"] if row.get("来源登记编号")}
        assert_true(limiter["app-key-active"]["规则状态"] == "生效中", "有效来源限流规则生效中")
        assert_true(limiter["app-key-active"]["配额消耗"] == 270, "限流规则同步最新水位 270")
        assert_true(limiter["app-key-frozen"]["规则状态"] == "已关闭", "冻结来源限流规则已关闭")
        assert_true(limiter["app-key-revoked"]["规则状态"] == "已关闭", "注销来源限流规则已关闭")

        print("7) 入参校验")
        status, body = call("POST", "/api/whitelist/settlements", {
            "entry_ids": [], "start_date": "2026-11-10", "end_date": "2026-11-12",
        })
        assert_true(status == 400, "空勾选返回 400")
        status, body = call("POST", "/api/whitelist/settlements", {
            "entry_ids": ids, "start_date": "2026-11-13", "end_date": "2026-11-10",
        })
        assert_true(status == 400, "倒置日期范围返回 400")

        print("8) 冻结动作立即失效并同步限流降级")
        # 新建一条有效来源登记并先结算，再执行冻结，验证次日额度立即关闭
        status, body = call("POST", "/api/whitelist", {"values": {
            "登记编号": "WHIT-0099",
            "应用名称": "实时结算演示应用",
            "调用标识": "app-key-freeze-e2e",
            "日配额上限": 500,
            "优先级": 99,
            "生效日期": "2026-11-01",
            "失效日期": "2026-12-31",
        }})
        assert_true(status == 200 and body["ok"], "新建来源登记成功")
        new_id = body["entry"]["id"]
        status, body = call("POST", f"/api/whitelist/{new_id}/actions", {"values": {"action": "启用登记"}})
        assert_true(body["entry"]["status"] == "已登记", "新登记启用为已登记")
        status, body = call("POST", f"/api/whitelist/{new_id}/actions", {"values": {"action": "冻结来源"}})
        assert_true(status == 200 and body["entry"]["status"] == "已冻结", "冻结来源动作生效")
        assert_true("限流降级" in body["message"], f"返回消息说明已同步限流：{body['message']}")
        status, body = call("GET", "/api/limiter?size=200")
        synced = next(row for row in body["items"] if row.get("限流对象") == "app-key-freeze-e2e")
        assert_true(synced["规则状态"] == "已关闭", "冻结后限流规则立即关闭")
        assert_true(synced["配额消耗"] == 0 and synced["剩余额度"] == 0, "冻结后额度归零、退出次日额度")
        status, body = call("GET", "/api/whitelist/checkpoints")
        assert_true(
            any(row["调用标识"] == "app-key-freeze-e2e" and row["已失效"] for row in body["items"]),
            "冻结后断点水位标记已失效",
        )

        print("\n全部端到端验收通过 ✅")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
