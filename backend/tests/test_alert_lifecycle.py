"""告警生命周期回归测试：人工停用不等于故障，窗口与下游清单保持同步。"""
from __future__ import annotations

import unittest

from app.seed import SEED_ROWS
from app.services.alert import AlertService
from app.services.retry import RetryService
from app.store import store


class AlertLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例都从一份干净的示例数据开始，避免用例之间互相污染。
        store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
        self.alerts = AlertService()
        self.retry = RetryService()

    def _retry_rows(self, rule_no: str) -> list[dict]:
        return [
            row
            for row in store.rows("retry")
            if str(row.get("关联告警") or row.get("alert_no") or "") == rule_no
        ]

    def _plan_rows(self, rule_no: str) -> list[dict]:
        return [
            row
            for row in store.rows("taskplan")
            if row.get("来源") == "采集告警中心" and row.get("alert_no") == rule_no
        ]

    def test_disable_is_not_a_fault_and_list_matches_detail(self) -> None:
        entry, message = self.alerts.run_action(3, "停用规则")
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["status"], "已停用")
        self.assertFalse(entry["abnormal"])
        self.assertEqual(entry["是否启用"], "否")

        detail = self.alerts.get_entry(3)
        listed = next(
            row for row in self.alerts.list_entries()[0] if row["规则编号"] == "ALER-0003"
        )
        self.assertEqual(detail["是否启用"], listed["是否启用"])
        self.assertEqual(detail["状态"], listed["状态"])

        # 人工停用不应污染异常量。
        alert_module = next(
            module for module in store.overview()["modules"] if module["name"] == "alert"
        )
        self.assertEqual(alert_module["abnormal"], 0)

    def test_repeated_actions_only_transition_once(self) -> None:
        self.alerts.run_action(3, "停用规则")
        history_after_first = len(self.alerts.get_history(3) or [])
        self.alerts.run_action(3, "停用规则")
        self.assertEqual(len(self.alerts.get_history(3) or []), history_after_first)

        self.alerts.run_action(3, "重开规则")
        self.alerts.run_action(3, "启用规则")
        history_after_enable = len(self.alerts.get_history(3) or [])
        self.alerts.run_action(3, "启用规则")
        self.assertEqual(len(self.alerts.get_history(3) or []), history_after_enable)

    def test_reopen_requires_revalidation(self) -> None:
        self.alerts.run_action(3, "停用规则")
        blocked, message = self.alerts.run_action(3, "启用规则")
        self.assertIsNone(blocked, message)

        reopened, _ = self.alerts.run_action(3, "重开规则")
        self.assertEqual(reopened["status"], "待校验")

        enabled, _ = self.alerts.run_action(3, "启用规则")
        self.assertEqual(enabled["status"], "已启用")

    def test_paused_window_ignores_duplicate_events_and_resume_opens_new_window(self) -> None:
        paused, _ = self.alerts.run_action(3, "暂停升级")
        before = paused["window_id"]

        entry, message = self.alerts.report_event(3, {"事件编号": "EVT-1"})
        self.assertEqual(entry["upgrade_count"], 0, message)

        resumed, message = self.alerts.run_action(3, "恢复升级")
        self.assertEqual(resumed["window_id"], before + 1, message)

        self.alerts.report_event(3, {"事件编号": "EVT-1"})
        counted, _ = self.alerts.report_event(3, {"事件编号": "EVT-1"})
        self.assertEqual(counted["upgrade_count"], 1)
        # 旧窗口历史保留，新窗口以恢复时间为准。
        self.assertGreaterEqual(len(counted["window_history"]), 2)
        self.assertIsNotNone(counted["resumed_at"])

    def test_disable_removes_plan_todos_and_downgrades_retry_rule(self) -> None:
        self.assertTrue(self._plan_rows("ALER-0003"))
        self.assertTrue(self._retry_rows("ALER-0003")[0]["可升级"])

        self.alerts.run_action(3, "停用规则")

        self.assertEqual(self._plan_rows("ALER-0003"), [])
        retry_row = self._retry_rows("ALER-0003")[0]
        self.assertFalse(retry_row["可升级"])
        self.assertEqual(retry_row["是否启用"], "否")
        self.assertEqual(retry_row["告警状态"], "已停用")

    def test_discarded_alert_cannot_reopen(self) -> None:
        self.alerts.run_action(3, "废弃规则")
        entry, message = self.alerts.run_action(3, "启用规则")
        self.assertIsNone(entry, message)
        entry, message = self.alerts.run_action(3, "重开规则")
        self.assertIsNone(entry, message)
        self.assertFalse(self._plan_rows("ALER-0003"))

    def test_disposals_are_kept_across_windows(self) -> None:
        self.alerts.run_action(3, "停用规则")
        baseline = len(self.alerts.get_disposals(3) or [])
        self.alerts.run_action(3, "重开规则")
        self.alerts.run_action(3, "启用规则")
        self.alerts.run_action(3, "暂停升级")
        self.alerts.run_action(3, "恢复升级")

        disposals = self.alerts.get_disposals(3) or []
        self.assertEqual(
            [item["action"] for item in disposals[baseline:]],
            ["重开规则", "启用规则", "暂停升级", "恢复升级"],
        )


if __name__ == "__main__":
    unittest.main()
