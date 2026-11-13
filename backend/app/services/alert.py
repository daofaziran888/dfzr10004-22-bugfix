"""采集告警中心业务规则：状态流转、处理窗口、异常口径与下游同步都收在这里。

几条硬性约束：

* 状态只能按 草稿 -> 待校验 -> 已启用 -> 已停用 -> 已废弃 的顺序向前流转，
  同一个动作重复执行是幂等的，不会重复写历史，也不会重复开新的处理窗口。
* 「人工停用」和「发生故障」是两个不同结论：停用只改状态，不写 ``abnormal``，
  异常量只由真实的异常事件维护。
* 每次启用/恢复都会新开一个处理窗口，旧窗口的历史保留在 ``window_history`` 里；
  已废弃的规则不允许再回到任何阶段。
* 状态变化后会把告警状态同步到采集计划处置清单与异常重试规则清单。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "alert"
PLAN_MODULE = "taskplan"
RETRY_MODULE = "retry"
LOG_MODULE = "audit"
REQUIRED_FIELDS = ["规则编号", "监控指标", "告警级别"]

# 状态流转顺序：只允许从左往右走，不允许跳级或回头。
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用", "已废弃"]
STATUS_DRAFT, STATUS_CHECKING, STATUS_ENABLED, STATUS_DISABLED, STATUS_DISCARDED = STATUS_ORDER

# 同一动作可能有多种叫法，统一归一化后再走状态机。
ACTION_ALIASES = {
    "提交校验": "submit",
    "重新校验": "submit",
    "重开规则": "reopen",
    "重开告警": "reopen",
    "启用规则": "enable",
    "启用告警": "enable",
    "停用规则": "disable",
    "停用告警": "disable",
    "暂停升级": "pause",
    "暂停告警": "pause",
    "暂停": "pause",
    "恢复升级": "resume",
    "恢复告警": "resume",
    "恢复": "resume",
    "废弃规则": "discard",
    "废弃告警": "discard",
    "废弃": "discard",
}

ACTION_LABELS = {
    "submit": "提交校验",
    "reopen": "重开规则",
    "enable": "启用规则",
    "disable": "停用规则",
    "pause": "暂停升级",
    "resume": "恢复升级",
    "discard": "废弃规则",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


class AlertService:
    # ------------------------------------------------------------------ 查询
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
                or keyword in str(row.get("监控指标", ""))
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

    def get_by_no(self, rule_no: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("规则编号")) == str(rule_no):
                self.refresh_view(row)
                return row
        return None

    def refresh_view(self, entry: dict[str, Any]) -> dict[str, Any]:
        """统一列表与详情共用的展示字段，避免一处显示启用、另一处显示停用。"""
        status = str(entry.get("status") or STATUS_DRAFT)
        discarded = bool(entry.get("discarded")) or status == STATUS_DISCARDED
        enabled = status == STATUS_ENABLED and not discarded
        entry["status"] = status
        entry["状态"] = status
        entry["是否启用"] = "是" if enabled else "否"
        entry["异常标记"] = bool(entry.get("abnormal"))
        entry["已废弃"] = discarded
        if enabled:
            entry["处理窗口"] = "启用" if entry.get("window_open") else "暂停"
        else:
            entry["处理窗口"] = "已废弃" if discarded else "未开启"
        entry["升级次数"] = int(entry.get("upgrade_count") or 0)
        entry["window_id"] = int(entry.get("window_id") or 0)
        entry["window_open"] = bool(entry.get("window_open"))
        # 旧触发时间与新恢复时间冲突时，以当前处理窗口的开启时间为准。
        entry["生效时间"] = (
            entry.get("window_started_at")
            or entry.get("resumed_at")
            or entry.get("triggered_at")
        )
        return entry

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": _next_id(rows)}
        # 只接收展示字段，动作与内部状态字段由服务端自己维护。
        internal_keys = {
            "action", "status", "状态", "pending", "abnormal", "异常标记", "是否启用",
            "history", "disposals", "upgrade_count", "window_open", "window_id",
            "discarded", "upgrade_history", "seen_event_keys",
        }
        for field, value in values.items():
            if field in internal_keys:
                continue
            entry[field] = value
        entry["status"] = STATUS_DRAFT
        entry["pending"] = True
        entry["abnormal"] = False
        entry["是否启用"] = "否"
        entry["upgrade_count"] = 0
        entry["window_open"] = False
        entry["window_id"] = 0
        entry["discarded"] = False
        entry["triggered_at"] = None
        entry["resumed_at"] = None
        entry["history"] = []
        entry["window_history"] = []
        entry["disposals"] = []
        entry["seen_event_keys"] = []
        rows.append(entry)
        self.refresh_view(entry)
        return entry, []

    # ------------------------------------------------------------------ 动作
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"告警规则 {entry_id} 不存在或已归档"
        kind = ACTION_ALIASES.get(str(action or "").strip())
        if kind is None:
            return None, f"动作「{action}」不属于采集告警中心可执行范围"
        status = str(entry.get("status") or STATUS_DRAFT)
        if entry.get("discarded") or status == STATUS_DISCARDED:
            return None, "告警规则已废弃，不能重新启用、恢复或再次废弃"
        handler = getattr(self, f"_action_{kind}")
        return handler(entry, ACTION_LABELS[kind], status)

    def _action_submit(
        self, entry: dict[str, Any], label: str, status: str
    ) -> tuple[dict[str, Any] | None, str]:
        if status == STATUS_CHECKING:
            self.refresh_view(entry)
            return entry, "告警规则已处于待校验状态，本次不重复流转"
        # 已停用的规则允许人工重开，但必须重新走校验，不能直接启用。
        if status in (STATUS_DRAFT, STATUS_DISABLED):
            self._transition(entry, label, STATUS_CHECKING, "告警规则已提交校验")
            return entry, "告警规则已提交校验，待校验通过后才能启用"
        return None, "已启用规则不能再提交校验，请先停用"

    def _action_reopen(
        self, entry: dict[str, Any], label: str, status: str
    ) -> tuple[dict[str, Any] | None, str]:
        if status == STATUS_CHECKING:
            self.refresh_view(entry)
            return entry, "告警规则已重新提交校验，待校验通过后才能启用"
        if status != STATUS_DISABLED:
            return None, "只有已停用的告警规则才允许人工重开"
        self._transition(entry, label, STATUS_CHECKING, "告警规则已重开并进入待校验")
        return entry, "告警规则已重开，必须重新校验后才能启用"

    def _action_enable(
        self, entry: dict[str, Any], label: str, status: str
    ) -> tuple[dict[str, Any] | None, str]:
        if status == STATUS_ENABLED:
            self.refresh_view(entry)
            return entry, "告警规则已启用，本次不重复流转"
        if status == STATUS_DISABLED:
            return None, "已停用的告警规则必须重新走校验后才能启用"
        if status == STATUS_DRAFT:
            return None, "草稿状态的告警规则需先提交校验"
        if status != STATUS_CHECKING:
            return None, f"当前状态「{status}」不允许启用"
        self._transition(entry, label, STATUS_ENABLED, "告警规则已启用")
        self._open_window(entry, "启用", status_reason="启用规则")
        return entry, "告警规则已启用，并开启新的处理窗口"

    def _action_disable(
        self, entry: dict[str, Any], label: str, status: str
    ) -> tuple[dict[str, Any] | None, str]:
        if status == STATUS_DISABLED:
            self.refresh_view(entry)
            return entry, "告警规则已停用，本次不重复流转"
        if status != STATUS_ENABLED:
            return None, f"当前状态「{status}」不允许停用"
        self._close_window(entry, "停用")
        # 人工停用只是状态结论，不写异常标记，避免污染异常量。
        self._transition(entry, label, STATUS_DISABLED, "告警规则已停用")
        return entry, "告警规则已停用（人工停用不计入异常量）"

    def _action_pause(
        self, entry: dict[str, Any], label: str, status: str
    ) -> tuple[dict[str, Any] | None, str]:
        if status != STATUS_ENABLED:
            return None, "只有已启用的告警规则才允许暂停升级窗口"
        if not entry.get("window_open"):
            self.refresh_view(entry)
            return entry, "告警规则已处于暂停升级窗口，本次不重复流转"
        entry["window_open"] = False
        entry["window_state"] = "暂停"
        entry.setdefault("window_history", []).append(
            {"window_id": entry.get("window_id", 0), "state": "暂停", "at": _now()}
        )
        self._record_disposal(entry, label, status, status, "暂停升级窗口", changed=True)
        self.refresh_view(entry)
        self._sync_dependents(entry)
        return entry, "升级窗口已暂停，后续重复事件不累计升级次数"

    def _action_resume(
        self, entry: dict[str, Any], label: str, status: str
    ) -> tuple[dict[str, Any] | None, str]:
        if status != STATUS_ENABLED:
            return None, "只有已启用的告警规则才允许恢复升级"
        if entry.get("window_open"):
            self.refresh_view(entry)
            return entry, "处理窗口已开启，本次不重复开窗"
        # 恢复只开启一个新的处理窗口，旧窗口的记录保留，升级计数重新开始。
        self._open_window(entry, "恢复", status_reason="恢复告警")
        self._record_disposal(entry, label, status, status, "恢复并开启新的处理窗口", changed=True)
        self.refresh_view(entry)
        self._sync_dependents(entry)
        return entry, "已开启新的处理窗口，旧窗口历史保留，升级次数重新累计"

    def _action_discard(
        self, entry: dict[str, Any], label: str, status: str
    ) -> tuple[dict[str, Any] | None, str]:
        if status == STATUS_DISCARDED:
            self.refresh_view(entry)
            return entry, "告警规则已废弃，本次不重复流转"
        self._close_window(entry, "废弃")
        self._transition(entry, label, STATUS_DISCARDED, "告警规则已废弃")
        entry["discarded"] = True
        self.refresh_view(entry)
        self._sync_dependents(entry)
        return entry, "告警规则已废弃，之后不能回到任何启停阶段"

    # ------------------------------------------------------------------ 异常事件
    def report_event(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """接收一次异常事件。

        只有处于「已启用且处理窗口开启」的告警才累计升级次数；暂停窗口内到达的
        同类重复事件不累计。事件编号在同一窗口内去重，恢复后新窗口重新计数。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"告警规则 {entry_id} 不存在或已归档"
        status = str(entry.get("status") or STATUS_DRAFT)
        if entry.get("discarded") or status == STATUS_DISCARDED:
            return None, "告警规则已废弃，事件不参与升级"
        event_key = str(
            values.get("事件编号")
            or values.get("event_key")
            or values.get("event_id")
            or values.get("事件类型")
            or values.get("event_type")
            or ""
        ).strip()
        entry["last_event_at"] = _now()
        if status != STATUS_ENABLED:
            entry["last_event_result"] = "ignored"
            self.refresh_view(entry)
            return entry, "告警规则未启用，事件不累计升级次数"
        if not entry.get("window_open"):
            entry["last_event_result"] = "ignored"
            entry.setdefault("ignored_events", []).append(
                {"event_key": event_key, "at": entry["last_event_at"]}
            )
            self.refresh_view(entry)
            return entry, "告警处于暂停升级窗口，重复事件不累计升级次数"
        if event_key and event_key in entry.setdefault("seen_event_keys", []):
            entry["last_event_result"] = "duplicate"
            self.refresh_view(entry)
            return entry, "同一处理窗口内的重复事件，不重复累计升级次数"
        if event_key:
            entry["seen_event_keys"].append(event_key)
        entry["upgrade_count"] = int(entry.get("upgrade_count") or 0) + 1
        entry["升级次数"] = entry["upgrade_count"]
        entry["last_event_result"] = "counted"
        if values.get("fault") or values.get("异常"):
            entry["abnormal"] = True
        entry.setdefault("upgrade_history", []).append(
            {
                "event_key": event_key or None,
                "event_type": values.get("事件类型") or values.get("event_type"),
                "window_id": entry.get("window_id", 0),
                "upgrade_count": entry["upgrade_count"],
                "at": entry["last_event_at"],
            }
        )
        self.refresh_view(entry)
        self._sync_dependents(entry)
        return entry, f"异常事件已计入升级，当前升级次数 {entry['upgrade_count']}"

    def get_history(self, entry_id: int) -> list[dict[str, Any]] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return list(entry.get("history", []))

    def get_disposals(self, entry_id: int) -> list[dict[str, Any]] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        # 处置记录只追加不清空，切换窗口、停用、废弃后仍然可追溯。
        return list(entry.get("disposals", []))

    def add_disposal(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"告警规则 {entry_id} 不存在或已归档"
        action = str(values.get("action") or values.get("动作") or "人工处置")
        result = str(values.get("result") or values.get("结果") or "已记录人工处置")
        record = self._record_disposal(
            entry, action, entry.get("status"), entry.get("status"), result, changed=True
        )
        self.refresh_view(entry)
        return entry, f"处置记录已登记：{record['处置编号']}"

    # ------------------------------------------------------------------ 内部
    def _transition(
        self, entry: dict[str, Any], action: str, target: str, message: str
    ) -> None:
        source = str(entry.get("status") or STATUS_DRAFT)
        entry["status"] = target
        entry["pending"] = target in (STATUS_DRAFT, STATUS_CHECKING)
        entry.setdefault("history", []).append(
            {
                "from": source,
                "to": target,
                "action": action,
                "previous_abnormal": bool(entry.get("abnormal")),
                "abnormal": bool(entry.get("abnormal")),
                "window_id": entry.get("window_id", 0),
                "at": _now(),
            }
        )
        if target != STATUS_ENABLED:
            entry["是否启用"] = "否"
        self.refresh_view(entry)
        self._record_disposal(entry, action, source, target, message, changed=True)
        self._sync_dependents(entry)

    def _open_window(self, entry: dict[str, Any], state: str, *, status_reason: str) -> None:
        entry["window_id"] = int(entry.get("window_id") or 0) + 1
        entry["window_open"] = True
        entry["window_state"] = state
        entry["upgrade_count"] = 0
        entry["升级次数"] = 0
        entry["seen_event_keys"] = []
        entry["triggered_at"] = _now()
        entry["window_started_at"] = entry["triggered_at"]
        if state == "恢复":
            entry["resumed_at"] = entry["triggered_at"]
        entry.setdefault("window_history", []).append(
            {
                "window_id": entry["window_id"],
                "state": state,
                "at": entry["triggered_at"],
                "reason": status_reason,
            }
        )
        self.refresh_view(entry)
        self._sync_dependents(entry)

    def _close_window(self, entry: dict[str, Any], state: str) -> None:
        if entry.get("window_open"):
            entry["window_open"] = False
            entry["window_state"] = state
            entry.setdefault("window_history", []).append(
                {"window_id": entry.get("window_id", 0), "state": state, "at": _now()}
            )

    def _record_disposal(
        self,
        entry: dict[str, Any],
        action: str,
        source: str,
        target: str,
        result: str,
        *,
        changed: bool,
    ) -> dict[str, Any]:
        disposals = entry.setdefault("disposals", [])
        record = {
            "id": _next_id(disposals),
            "处置编号": f"ALGD-{entry.get('规则编号', entry.get('id'))}-{len(disposals) + 1:02d}",
            "action": action,
            "from": source,
            "to": target,
            "result": result,
            "changed": changed,
            "operator": "值班管理员",
            "window_id": entry.get("window_id", 0),
            "at": _now(),
        }
        disposals.append(record)
        # 审计日志与详情共用同一份处置结果，信息不会只留在某一个页面。
        logs = store.rows(LOG_MODULE)
        logs.append(
            {
                "id": _next_id(logs),
                "status": "已记录",
                "pending": True,
                "abnormal": False,
                "记录编号": f"ALOG-{len(logs) + 1:04d}",
                "操作类型": action,
                "采集计划": entry.get("规则编号"),
                "执行人": record["operator"],
                "执行时间": record["at"],
                "结果摘要": result,
                "变更内容": f"{source} -> {target}",
                "复核状态": "待复核",
                "alert_no": entry.get("规则编号"),
                "action": action,
                "result": result,
                "operator": record["operator"],
                "at": record["at"],
                "template": "log",
            }
        )
        return record

    def _sync_dependents(self, entry: dict[str, Any]) -> None:
        rule_no = str(entry.get("规则编号") or "")
        status = str(entry.get("status") or STATUS_DRAFT)
        active = status in (STATUS_CHECKING, STATUS_ENABLED) and not entry.get("discarded")
        self._sync_plan(entry, rule_no, status, active)
        self._sync_retry(entry, rule_no, status)

    def _sync_plan(
        self, entry: dict[str, Any], rule_no: str, status: str, active: bool
    ) -> None:
        # 采集计划处置清单就是任务编排列表里的处置项：告警启用时生成待处理项，
        # 停用/废弃时撤掉，避免另一侧页面继续显示已停用规则触发的待办。
        rows = store.rows(PLAN_MODULE)
        existing = next(
            (
                row
                for row in rows
                if row.get("来源") == "采集告警中心" and row.get("alert_no") == rule_no
            ),
            None,
        )
        if not active:
            if existing is not None:
                rows.remove(existing)
            return
        dispose_status = "待校验" if status == STATUS_CHECKING else "待处理"
        if existing is None:
            disposal_no = f"DISP-{rule_no}-W{int(entry.get('window_id') or 0)}"
            rows.append(
                {
                    "id": _next_id(rows),
                    "status": dispose_status,
                    "pending": True,
                    "abnormal": bool(entry.get("abnormal")),
                    "计划编号": disposal_no,
                    "plan_no": disposal_no,
                    "处置单号": disposal_no,
                    "目标站点": entry.get("监控指标") or "告警处置",
                    "采集频率": "按告警触发",
                    "数据范围": "告警关联采集任务",
                    "优先级": "高",
                    "资源配额": "按需分配",
                    "预计用时": "待评估",
                    "计划状态": "待处理",
                    "来源": "采集告警中心",
                    "source": "采集告警中心",
                    "alert_no": rule_no,
                    "alert_status": status,
                    "告警状态": status,
                }
            )
            return
        existing["status"] = dispose_status
        existing["计划状态"] = dispose_status
        existing["pending"] = True
        existing["alert_status"] = status
        existing["告警状态"] = status
        existing["abnormal"] = bool(entry.get("abnormal"))

    def _sync_retry(self, entry: dict[str, Any], rule_no: str, status: str) -> None:
        enabled = status == STATUS_ENABLED and not entry.get("discarded")
        window_open = bool(entry.get("window_open")) if enabled else False
        for row in store.rows(RETRY_MODULE):
            if str(row.get("关联告警") or row.get("alert_no") or "") != rule_no:
                continue
            row["alert_status"] = status
            row["告警状态"] = status
            row["window_open"] = window_open
            row["window_id"] = entry.get("window_id", 0)
            row["升级次数"] = int(entry.get("upgrade_count") or 0)
            row["处理窗口"] = (
                "启用" if enabled and window_open
                else "暂停" if enabled
                else "未开启"
            )
            upgradable = (
                enabled
                and window_open
                and str(row.get("status")) == STATUS_ENABLED
            )
            row["可升级"] = upgradable
            row["upgradable"] = upgradable
            row["是否启用"] = (
                "是" if upgradable or (enabled and str(row.get("status")) == STATUS_ENABLED) else "否"
            )
