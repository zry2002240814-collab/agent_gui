"""Deterministic safety, semantic conflicts and completion checks."""

import re
from .contracts import collection_add

from .task import normalize_app


def check_decision(decision, observation, ledger, task, allowed, stagnant):
    hard, audit = [], []
    if decision.observation_id and decision.observation_id != observation.identifier:
        hard.append("动作引用了旧截图")
    if decision.action == "OPEN":
        decision.parameters["app_name"] = normalize_app(decision.parameters["app_name"])
    if decision.action == "COMPLETE":
        if re.search(r"未完成|尚未|还没|not\s+(?:yet\s+)?(?:done|finished|complete)", decision.summary, re.I):
            hard.append("决策明确表示任务尚未完成")
        if decision.next_objective.strip().lower() not in {"", "none", "done", "完成"}:
            hard.append("仍有下一目标，不能完成")
        hard.extend(ledger.completion_issues(task, observation))
        audit.append("completion request")
    label = decision.label
    adding = collection_add(label, decision.expected_effect)
    if task.collection and adding and decision.action in {"CLICK", "DOUBLE_CLICK", "LONG_PRESS"}:
        item_id = decision.expected_effect.get("item_id")
        item = ledger.items.get(item_id) if isinstance(item_id, str) else None
        if not item:
            hard.append("添加条目缺少稳定 item_id 和属性证据")
        elif item["status"] == "rejected" or not task.qualify(item.get("rating")):
            hard.append("不能添加不满足原始条件的条目")
        elif item["status"] == "added":
            hard.append("条目已添加，不重复点击收藏开关")
    if decision.action in {"CLICK", "DOUBLE_CLICK", "LONG_PRESS"}:
        if not label:
            audit.append("target label missing; inspect the screenshot before clicking")
        if re.search(r"支付|收银|订单确认|删除|权限|授权", ledger.summary):
            audit.append("sensitive page requires visual review")
    risks = {"payment": r"立即支付|确认支付|付款|支付密码|pay\s*now",
             "order": r"提交订单|确认下单|立即购买|确认预订",
             "delete": r"删除|清空|永久移除|delete|erase",
             "authorize": r"授权|允许访问|允许所有|authorize",
             "publish": r"发布|发送评论|提交评论|send|publish"}
    for kind, pattern in risks.items():
        if decision.action in {"CLICK", "DOUBLE_CLICK", "LONG_PRESS", "TYPE"} and re.search(pattern, label, re.I):
            if kind not in allowed:
                hard.append(f"未允许的副作用: {kind}")
            else:
                audit.append(f"authorized side effect: {kind}")
    recent = list(ledger.history)[-3:]
    if len(recent) == 3 and all(r["action"] == decision.action and r["parameters"] == decision.parameters for r in recent):
        if stagnant >= 2 and decision.action not in {"WAIT", "COMPLETE"}:
            audit.append("repeated action without visible progress")
    if ledger.pending and decision.action not in {"WAIT", "BACK", "HOME", "COMPLETE"}:
        hard.append("上一动作未核验，先观察或安全恢复，不继续叠加操作")
    if decision.action == "SCROLL":
        start, end = decision.parameters["start_point"], decision.parameters["end_point"]
        decision.parameters["end_point"] = [max(0, min(1000, start[i] + max(-350, min(350, end[i] - start[i])))) for i in range(2)]
    return hard, audit
