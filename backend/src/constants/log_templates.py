"""审计动作 -> 中文标签。"""
from __future__ import annotations

ACTIONS: dict[str, str] = {
    "auth.login": "登录",
    "team.update": "调整团队预算",
    "run.create": "发起成本运行",
    "run.complete": "成本运行完成",
    "alert.review": "处置预算告警",
    "transfer.create": "提交预算借用申请",
    "transfer.approve": "审批通过预算借用",
    "transfer.reject": "驳回预算借用",
    "transfer.withdraw": "撤回预算借用",
    "transfer.expire": "预算借用过期释放",
    "transfer.reconcile": "预算调拨对账快照",
    "export.charges": "导出分摊账单",
    "http.mutation": "写请求",
}
