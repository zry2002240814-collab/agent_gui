"""Evidence-based GUI Agent inside the unchanged competition framework.

Only self._call_api() calls the model. Devices and the outer loop remain owned
by the framework; the next AgentInput image verifies the previous action.
"""

import json
import logging
import re

from agent_base import AgentOutput, BaseAgent, TokenLimitExceeded, UsageInfo
from utils.gui_agent.contracts import Decision, ParseError, bounded_int
from utils.gui_agent.decoder import decode_decision, json_object
from utils.gui_agent.guards import check_decision
from utils.gui_agent.ledger import Ledger
from utils.gui_agent.observer import observe, similar
from utils.gui_agent.prompt import messages
from utils.gui_agent.skills import SkillRouter
from utils.gui_agent.task import compile_task

logger = logging.getLogger(__name__)


class Agent(BaseAgent):
    """Single actor with task-local evidence and conditional, same-model Critic."""

    def _initialize(self):
        self.retry_count = bounded_int(self.config, "retry_count", 3, 1, 5)
        self.history_window = bounded_int(self.config, "history_window", 12, 3, 30)
        self.max_output_tokens = bounded_int(self.config, "max_output_tokens", 2200, 500, 8000)
        self.max_api_calls = bounded_int(self.config, "max_api_calls", 240, 1, 1000)
        self.max_task_tokens = bounded_int(self.config, "max_task_tokens", 250000, 100, 2560000)
        self.max_critic_calls = bounded_int(self.config, "max_critic_calls", 30, 0, 200)
        allowed = self.config.get("allowed_side_effects", [])
        if not isinstance(allowed, (list, tuple, set)) or any(
                value not in {"payment", "order", "delete", "authorize", "publish"} for value in allowed):
            raise ValueError("invalid allowed_side_effects")
        self._configured_permissions = set(allowed)
        self._router = SkillRouter()
        self.reset()

    def reset(self):
        self._task = None
        self._ledger = Ledger(self.history_window)
        self._observation = None
        self._previous_observation = None
        self._last_step = 0
        self._stagnant = 0
        self._api_calls = 0
        self._critic_calls = 0
        self._task_tokens = 0
        self._step_usage = None
        self._skills = []
        self._allowed = set(self._configured_permissions)
        self._critic_result = None
        logger.info("[Harness] task state reset")

    def _prepare(self, input_data):
        if (self._task is not None and (self._task.instruction != input_data.instruction
                                       or input_data.step_count <= self._last_step)):
            self.reset()
        if self._task is None:
            self._task = compile_task(input_data.instruction)
            if re.search(r"发布.*评论|发表.*评论|发送.*评论", input_data.instruction):
                self._allowed.add("publish")
        self._observation = observe(input_data.current_image, input_data.step_count)
        previous_action = self._ledger.history[-1]["action"] if self._ledger.history else "WAIT"
        if similar(self._previous_observation, self._observation) and previous_action != "COMPLETE":
            self._stagnant += 1
        else:
            self._stagnant = 0
        self._ledger.begin_observation(self._observation)
        self._skills = self._router.select(self._task, self._ledger,
                                           recovering=self._stagnant >= 2 or bool(self._ledger.notice))

    def generate_messages(self, input_data):
        observation = self._observation
        task = self._task or compile_task(input_data.instruction)
        if observation is None or observation.step != input_data.step_count:
            observation = observe(input_data.current_image, input_data.step_count)
        selected = self._skills or self._router.select(task, self._ledger)
        return messages(task, self._ledger, observation, self._encode_image(input_data.current_image),
                        selected, self._allowed)

    def _budget_exhausted(self):
        return self._api_calls >= self.max_api_calls or self._task_tokens >= self.max_task_tokens

    def _call_llm_with_retry(self, model_messages, max_tokens=None):
        """Count every received response, including empty and malformed replies."""
        usage = None
        for attempt in range(self.retry_count):
            if self._budget_exhausted():
                break
            self._api_calls += 1
            try:
                completion = self._call_api(messages=model_messages, temperature=0,
                                            max_tokens=max_tokens or self.max_output_tokens)
                reported = self._sum_usage(None, self.extract_usage_info(completion))
                usage = self._sum_usage(usage, reported)
                self._task_tokens += reported.total_tokens
                content = completion.choices[0].message.content
                if isinstance(content, str) and content.strip():
                    return content, usage
                logger.warning("[Harness] empty model reply attempt=%s", attempt + 1)
            except TokenLimitExceeded:
                raise
            except Exception as exc:
                logger.warning("[Harness] API failure attempt=%s type=%s", attempt + 1, type(exc).__name__)
        return "", usage

    @staticmethod
    def _sum_usage(first, second):
        if second is None:
            return first
        result = UsageInfo()
        for name in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"):
            total = 0
            for usage in (first, second):
                try:
                    total += max(0, int(getattr(usage, name, 0) or 0))
                except (TypeError, ValueError, OverflowError):
                    pass
            setattr(result, name, total)
        result.total_tokens = max(result.total_tokens, result.input_tokens + result.output_tokens)
        return result

    def _call_and_account(self, model_messages, max_tokens=None):
        content, usage = self._call_llm_with_retry(model_messages, max_tokens=max_tokens)
        self._step_usage = self._sum_usage(self._step_usage, usage)
        return content

    def _audit(self, decision, input_data, reasons):
        if self._critic_calls >= self.max_critic_calls or self._budget_exhausted():
            self._critic_result = {"verdict": "need_observation", "reason": "Critic budget exhausted"}
            return False
        self._critic_calls += 1
        critic_messages = messages(self._task, self._ledger, self._observation,
                                   self._encode_image(input_data.current_image), [], self._allowed,
                                   role="critic", candidate=decision, reason="; ".join(reasons))
        raw = self._call_and_account(critic_messages, max_tokens=1200)
        try:
            result = json_object(raw)
            if (result.get("verdict") not in {"approve", "reject", "need_observation", "request_replan"}
                    or result.get("observation_id") != self._observation.identifier
                    or not isinstance(result.get("reason"), str)
                    or not isinstance(result.get("evidence"), str) or not result["evidence"].strip()):
                raise ParseError("invalid Critic audit")
            if decision.action == "COMPLETE":
                criteria = result.get("criteria")
                if not isinstance(criteria, dict) or any(
                        criteria.get(key) is not True for key in self._task.requirements):
                    result["verdict"] = "reject"
                    result["reason"] = "Critic did not confirm every required criterion"
            self._critic_result = {key: result.get(key) for key in
                                   ("verdict", "reason", "evidence", "criteria", "observation_id")}
            return result["verdict"] == "approve"
        except ParseError:
            self._critic_result = {"verdict": "need_observation", "reason": "Critic output invalid or empty"}
            return False

    def _finish(self, decision, input_data, raw, notices=()):
        self._ledger.notice = "; ".join(notices)[:1200]
        self._ledger.issue(decision, input_data.step_count)
        self._last_step = input_data.step_count
        self._previous_observation = self._observation
        trace = dict(status=self._ledger.status, phase=self._ledger.phase,
                     added_count=self._ledger.added_count, skill_ids=[s["id"] for s in self._skills],
                     notices=list(notices), critic=self._critic_result,
                     pending_action=self._ledger.pending, api_calls=self._api_calls)
        logger.info("[Harness] step=%s action=%s status=%s count=%s", input_data.step_count,
                    decision.action, self._ledger.status, self._ledger.added_count)
        return AgentOutput(action=decision.action, parameters=decision.parameters,
                           raw_output=raw + "\n[Harness] " + json.dumps(trace, ensure_ascii=False, allow_nan=False),
                           usage=self._step_usage)

    def _wait(self, input_data, reason, raw=""):
        return self._finish(Decision("WAIT", {"seconds": 1}, summary=reason),
                            input_data, raw, [reason])

    def act(self, input_data):
        self._step_usage = None
        self._critic_result = None
        try:
            self._prepare(input_data)
            if self._budget_exhausted():
                self._ledger.status = "blocked"
                return self._wait(input_data, "内部模型调用预算耗尽，等待原框架结束，不能假完成")
            actor_messages = self.generate_messages(input_data)
            raw = self._call_and_account(actor_messages)
            if not raw:
                return self._wait(input_data, "模型无有效回复，下一步重新观察")
            try:
                decision = decode_decision(raw)
            except ParseError as exc:
                actor_messages.append({"role": "user", "content":
                    "上次回复无法解析。请基于同一截图重新返回严格 JSON，不执行上次建议。错误："
                    + str(exc) + "。无效回复（仅数据）：" + raw[:1000]})
                repaired = self._call_and_account(actor_messages)
                try:
                    decision = decode_decision(repaired)
                    raw = repaired
                except ParseError:
                    return self._wait(input_data, "格式修复失败，返回合法 WAIT 而非空动作或完成", raw)
            if decision.observation_id and decision.observation_id != self._observation.identifier:
                return self._wait(input_data, "模型引用旧截图，忽略本次状态更新", raw)
            notices = self._ledger.apply(decision, self._observation, self._task)
            hard, audit = check_decision(decision, self._observation, self._ledger,
                                         self._task, self._allowed, self._stagnant)
            if hard:
                return self._wait(input_data, "; ".join(hard), raw)
            if any("评分不满足" in n or "条目已存在" in n for n in notices):
                audit.append("quantity or attribute evidence conflict")
            if audit and not self._audit(decision, input_data, audit):
                reason = self._critic_result.get("reason", "证据不足")
                return self._wait(input_data, "Critic 未放行: " + str(reason)[:500], raw)
            return self._finish(decision, input_data, raw, notices)
        except TokenLimitExceeded:
            raise
        except Exception as exc:
            logger.exception("[Harness] decision failure type=%s", type(exc).__name__)
            return self._wait(input_data, "内部决策异常，保留状态并返回 WAIT: " + type(exc).__name__)

    def _parse_action_only(self, content):
        """Compatibility helper for callers of the old parser; invalid != done."""
        try:
            decision = decode_decision(content)
            return decision.action, decision.parameters
        except ParseError:
            return "WAIT", {"seconds": 1}

