"""异常重试管理业务规则：状态流转、字段校验与筛选口径都收在这里。

重试规则通过 ``关联告警`` 跟随告警规则的生命周期：告警停用或废弃后，
关联重试规则不再作为「可升级规则」，重复事件也不会继续累计升级次数。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services import alert as alert_service_module

MODULE = "retry"
REQUIRED_FIELDS = ["规则编号", "异常类型", "重试上限"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用规则": "已启用", "停用规则": "已停用"}
NEGATIVE_ACTIONS = ["停用规则"]


def _alert_service() -> alert_service_module.AlertService:
    return alert_service_module.AlertService()


class RetryService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for row in rows:
            self.refresh_view(row)
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("规则编号", ""))
                or keyword in str(row.get("异常类型", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            self.refresh_view(entry)
        return entry

    def refresh_view(self, row: dict[str, Any]) -> dict[str, Any]:
        """按关联告警的实时状态刷新可升级标记，避免继续把已停用规则当可升级规则。"""
        linked_no = str(row.get("关联告警") or row.get("alert_no") or "")
        linked = _alert_service().get_by_no(linked_no) if linked_no else None
        if linked is not None:
            row["alert_status"] = linked.get("status")
            row["告警状态"] = linked.get("status")
            row["window_open"] = bool(linked.get("window_open"))
            row["window_id"] = linked.get("window_id", 0)
        alert_ok = linked is None or (
            linked.get("status") == "已启用" and not linked.get("discarded")
        )
        upgradable = (
            alert_ok
            and str(row.get("status")) == "已启用"
            and bool(row.get("window_open"))
        )
        row["可升级"] = upgradable
        row["upgradable"] = upgradable
        row["处理窗口"] = (
            "启用" if upgradable
            else "暂停" if alert_ok and str(row.get("status")) == "已启用"
            else "未开启"
        )
        if linked is not None and not alert_ok:
            # 告警已停用/废弃，重试规则清单同样不能显示为启用中的可升级规则。
            row["是否启用"] = "否"
        else:
            row["是否启用"] = "是" if str(row.get("status")) == "已启用" else "否"
        row["升级次数"] = int(row.get("升级次数") or 0)
        return row

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["关联告警"] = str(values.get("关联告警") or values.get("alert_no") or "")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["是否启用"] = "否"
        entry["可升级"] = False
        entry["升级次数"] = 0
        entry["处理窗口"] = "未开启"
        entry["window_open"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"重试规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于异常重试管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if entry.get("status") == target:
            self.refresh_view(entry)
            return entry, f"重试规则已{action}，本次不重复流转"
        if target == "已启用":
            linked_no = str(entry.get("关联告警") or entry.get("alert_no") or "")
            linked = _alert_service().get_by_no(linked_no) if linked_no else None
            if linked is not None and (
                linked.get("status") != "已启用" or linked.get("discarded")
            ):
                return None, f"关联告警 {linked_no} 未启用，重试规则不能作为可升级规则"
        source = str(entry.get("status"))
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        # 人工停用是状态结论，不等于发生故障，不写异常标记。
        entry.setdefault("history", []).append(
            {"from": source, "to": target, "action": action}
        )
        self.refresh_view(entry)
        return entry, f"重试规则已{action}"
