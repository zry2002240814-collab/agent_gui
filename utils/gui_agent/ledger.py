"""Bounded, task-local evidence ledger; issued actions are never execution receipts."""

import math
import re
from collections import deque
from .contracts import collection_add


def _text(value, limit=400):
    return value[:limit].strip() if isinstance(value, str) else ""


class Ledger:
    def __init__(self, history_window=12):
        self.history = deque(maxlen=history_window)
        self.events = deque(maxlen=100)
        self.facts = {}
        self.items = {}
        self.checks = {}
        self.app = ""
        self.summary = ""
        self.blockers = []
        self.phase = ""
        self.next_objective = ""
        self.notice = ""
        self.pending = None
        self.status = "running"

    def begin_observation(self, observation):
        # Page facts and checks need fresh evidence; task artifacts remain.
        for fact in self.facts.values():
            if fact["scope"] == "page" and fact["step"] != observation.step:
                fact["status"] = "superseded"

    @staticmethod
    def _evidence(record, observation):
        return (record.get("step") == observation.step
                and not isinstance(record.get("step"), bool)
                and bool(_text(record.get("evidence"))))

    def apply(self, decision, observation, task):
        issues = []
        self.summary = _text(decision.observation.get("summary"))
        blockers = decision.observation.get("blockers", [])
        self.blockers = [_text(v, 160) for v in blockers[:8] if _text(v, 160)] if isinstance(blockers, list) else []
        app = _text(decision.observation.get("app"), 100)
        if app:
            from .task import normalize_app
            self.app = normalize_app(app)
        self.phase = _text(decision.phase, 100)
        self.next_objective = _text(decision.next_objective)
        # First planning response may only add requirements, never replace constraints.
        if not task.plan and decision.plan:
            task.plan = [v[:200] for v in decision.plan]
            for requirement in decision.requirements[:12]:
                key = _text(requirement.get("id"), 60)
                description = _text(requirement.get("description"))
                if key and description and key not in task.requirements:
                    task.requirements[key] = description

        pending = self.pending
        verification = decision.verification
        confirmed_item = None
        if pending and verification:
            status = verification.get("status")
            if (verification.get("action_step") != pending["step"]
                    or isinstance(verification.get("action_step"), bool)
                    or not _text(verification.get("evidence"))
                    or observation.step <= pending["step"]):
                issues.append("上一动作核验缺少正确步骤或当前截图证据")
            elif status in {"confirmed", "failed"}:
                self.events.append(dict(kind="verification", step=observation.step,
                                        action_step=pending["step"], status=status,
                                        evidence=_text(verification["evidence"])))
                item_id = pending.get("expected_effect", {}).get("item_id")
                if (status == "confirmed" and item_id and pending["action"] in {
                        "CLICK", "DOUBLE_CLICK", "LONG_PRESS"}
                        and collection_add(pending["label"], pending.get("expected_effect", {}))):
                    confirmed_item = item_id
                if status == "failed":
                    issues.append("上一动作未生效，请根据截图换目标或路径")
                self.pending = None
            elif status != "unknown":
                issues.append("上一动作核验状态无效")

        for record in decision.facts[:32]:
            key = _text(record.get("key"), 100)
            scope = record.get("scope", "page")
            value = record.get("value")
            if (not key or scope not in {"page", "task"}
                    or not self._evidence(record, observation)
                    or not isinstance(value, (str, int, float, bool))
                    or isinstance(value, float) and not math.isfinite(value)):
                issues.append("忽略缺少当前证据的事实")
                continue
            if isinstance(value, str):
                value = value[:500]
            old = self.facts.get(key)
            fact = dict(key=key, value=value, scope=scope, step=observation.step,
                        evidence=_text(record["evidence"]), status="confirmed",
                        verification_method="actor_visual", observation_id=observation.identifier)
            if old and old["value"] != value:
                self.events.append(dict(kind="fact_superseded", key=key, old=old,
                                        replacement_step=observation.step))
            if len(self.facts) >= 32 and key not in self.facts:
                self.facts.pop(next(iter(self.facts)))
            self.facts[key] = fact

        for record in decision.items:
            name = _text(record.get("name"), 160)
            key = _text(record.get("id"), 100)
            status = record.get("status")
            if (not key or not name or not self._evidence(record, observation)
                    or status not in {"qualified", "rejected", "added"}):
                issues.append("忽略缺少当前证据的条目")
                continue
            rating = record.get("rating")
            if rating is not None and (isinstance(rating, bool)
                                      or not isinstance(rating, (int, float))
                                      or not math.isfinite(rating) or not 0 <= rating <= 5):
                issues.append("条目评分格式无效")
                continue
            identity = re.sub(r"\s+", "", name).casefold()
            # A model changing an item's id must not count the same named item twice.
            duplicate = next((k for k, item in self.items.items()
                              if item["identity"] == identity and k != key), None)
            if duplicate:
                issues.append("条目已存在，请复用已有 id")
                continue
            old = self.items.get(key)
            if old and old["identity"] != identity:
                issues.append("条目 id 不可指向另一个项目")
                continue
            if rating is None and old:
                rating = old.get("rating")
            if not task.qualify(rating):
                status = "rejected"
                issues.append("条目评分不满足原始阈值")
            elif status == "added" and key != confirmed_item:
                # An explicit current screenshot can verify a pre-existing entry,
                # but this must be audited separately before declaring completion.
                status = "qualified"
                issues.append("添加尚未获得对应动作的截图核验，暂不计数")
            if old and old["status"] == "added" and status == "qualified":
                status = "added"
            if len(self.items) >= 100 and key not in self.items:
                issues.append("条目容量已满，不能忽略目标继续声明完成")
                continue
            self.items[key] = dict(id=key, name=name, identity=identity, rating=rating,
                                   status=status, step=observation.step,
                                   evidence=_text(record["evidence"]))

        for record in decision.checks:
            key = record.get("id")
            if (key not in task.requirements or not self._evidence(record, observation)
                    or not isinstance(record.get("satisfied"), bool)):
                issues.append("忽略缺少当前证据的完成条件")
                continue
            self.checks[key] = dict(satisfied=record["satisfied"], step=observation.step,
                                    evidence=_text(record["evidence"]))
        return issues

    @property
    def added_count(self):
        return sum(item["status"] == "added" for item in self.items.values())

    def completion_issues(self, task, observation):
        problems = []
        if self.pending:
            problems.append("上一实际动作仍待核验")
        if self.blockers:
            problems.append("当前仍有阻塞: " + "; ".join(self.blockers))
        for key, description in task.requirements.items():
            check = self.checks.get(key)
            if not check or not check["satisfied"]:
                problems.append(f"未核实条件 {key}: {description}")
        goal = self.checks.get("task_goal", {})
        if goal.get("step") != observation.step:
            problems.append("总体完成必须引用当前截图")
        if task.target_count is not None and self.added_count < task.target_count:
            problems.append(f"符合条件且已添加的去重条目仅 {self.added_count}/{task.target_count}")
        if task.collection and self.added_count == 0:
            problems.append("收集任务尚无经过添加核验的条目")
        if task.rating_threshold is not None:
            if any(not task.qualify(i.get("rating")) for i in self.items.values() if i["status"] == "added"):
                problems.append("已添加条目包含不符合评分阈值的项目")
        return problems

    def issue(self, decision, step):
        self.history.append(dict(step=step, action=decision.action, parameters=decision.parameters,
                                 note=decision.summary[:160], label=decision.label))
        if decision.action not in {"WAIT", "COMPLETE"}:
            self.pending = dict(step=step, action=decision.action, parameters=decision.parameters,
                                label=decision.label, expected_effect=decision.expected_effect)
        if decision.action == "COMPLETE":
            self.status = "succeeded"

    def snapshot(self):
        return dict(status=self.status, app=self.app, phase=self.phase,
                    next_objective=self.next_objective, observation_summary=self.summary,
                    blockers=self.blockers,
                    facts=[f for f in self.facts.values() if f["status"] == "confirmed"],
                    items=list(self.items.values()), added_count=self.added_count,
                    checks=self.checks, pending_action=self.pending,
                    history=list(self.history), notice=self.notice)
