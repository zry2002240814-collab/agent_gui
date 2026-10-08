"""Protocol and state-machine regression tests. No network or device operations."""

import hashlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from agent import Agent
from agent_base import AgentInput, DEFAULT_API_URL, DEFAULT_MODEL_ID, TokenLimitExceeded
from test_runner import TestRunner
from utils.gui_agent.contracts import Decision, ParseError
from utils.gui_agent.decoder import decode_decision
from utils.gui_agent.ledger import Ledger
from utils.gui_agent.observer import observe
from utils.gui_agent.skills import SkillRouter
from utils.gui_agent.task import compile_task, normalize_app

ROOT = Path(__file__).resolve().parents[1]
FIXED_HASHES = {
    "agent_base.py": "0676e0b5ffd1088e1d46e39aed65faeea8a684cd15d9dcd19fd378f94235bda0",
    "device_controller.py": "0c106582f9e1fe85dacd8b4dc8f3021474c8d6acabe327373e0ffc01b6c35ee3",
    "requirements.txt": "88cffaa2d424df7471f792eddec0bbf752882990dd50f984288453a41c208d9b",
    "run_tests.py": "d276c23965e99f18025f5090689121da3d027bbab5689aa387efb4e85a4cefd0",
    "test_runner.py": "478166abbe6f3d6dff1cfc01d8cee5d62876cb7e5afe7f5686ee2e7e6e7933a2",
}


def packet(step, action="WAIT", params=None, **extra):
    result = dict(observation_id=f"obs-{step}", decision_summary="观察后推进任务",
                  observation={"app": "得到", "summary": "当前页面内容"},
                  action={"type": action, "parameters": params if params is not None else {"seconds": 1}, "label": "搜索框"})
    result.update(extra)
    return result


def audit(step, criteria=None, verdict="approve"):
    return dict(verdict=verdict, reason="已逐项核验", evidence="当前截图及步骤证据符合任务",
                observation_id=f"obs-{step}", criteria=criteria or {"task_goal": True})


class ScriptedAgent(Agent):
    def __init__(self, script, config=None):
        super().__init__(config)
        self.script = list(script)
        self.calls = []

    def _call_api(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if not self.script:
            raise AssertionError("unexpected model call")
        result = self.script.pop(0)
        if isinstance(result, BaseException):
            raise result
        tokens = 7
        if isinstance(result, tuple):
            result, tokens = result
        content = json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else result
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                               usage=SimpleNamespace(prompt_tokens=tokens - 2, completion_tokens=2,
                                                     total_tokens=tokens))


def frame(step, instruction="完成页面上的任务"):
    image = Image.new("RGB", (100, 200), "white")
    return AgentInput(instruction=instruction, current_image=image, step_count=step)


class DecoderTests(unittest.TestCase):
    def test_all_actions_fit_fixed_runner(self):
        examples = {"CLICK": {"point": [1, 2]}, "DOUBLE_CLICK": {"point": [3, 4]},
                    "LONG_PRESS": {"point": [5, 6], "duration": 1000},
                    "SCROLL": {"start_point": [500, 600], "end_point": [500, 300]},
                    "DRAG": {"start_point": [100, 700], "end_point": [900, 700]},
                    "TYPE": {"text": "中文输入\n"}, "OPEN": {"app_name": "QQ音乐"},
                    "WAIT": {"seconds": 1}, "BACK": {}, "HOME": {}, "COMPLETE": {}}
        for name, params in examples.items():
            with self.subTest(action=name):
                decision = decode_decision(json.dumps(packet(1, name, params)))
                self.assertTrue(TestRunner._validate_action(decision.action, decision.parameters)[0])

    def test_missing_and_boolean_coordinates_rejected(self):
        for point in (None, [True, 1], [1], [1, 2, 3], ["NaN", 2]):
            with self.subTest(point=point), self.assertRaises(ParseError):
                decode_decision(json.dumps(packet(1, "CLICK", {"point": point})))

    def test_coordinates_clamped_but_slider_not_shortened(self):
        decision = decode_decision(json.dumps(packet(1, "DRAG", {
            "start_point": [-5, 500], "end_point": [1300, 500]})))
        self.assertEqual(decision.parameters, {"start_point": [0, 500], "end_point": [1000, 500]})

    def test_legacy_uses_last_action_and_preserves_quotes(self):
        decision = decode_decision("Thought: click elsewhere\nAction: click(point='1 2')\n"
                                   'Action: type(content="don\'t change (this)\\n")')
        self.assertEqual((decision.action, decision.parameters), ("TYPE", {"text": "don't change (this)\n"}))

    def test_thought_is_never_an_action(self):
        with self.assertRaises(ParseError):
            decode_decision("Thought: task not done yet; maybe complete()\nAction: ???")

    def test_never_evaluates_model_code(self):
        with self.assertRaises(ParseError):
            decode_decision("Action: type(content=__import__('os').getcwd())")

    def test_scroll_down_reveals_lower_content(self):
        decision = decode_decision("Action: scroll(point='<point>500 600</point>', direction='down')")
        self.assertLess(decision.parameters["end_point"][1], decision.parameters["start_point"][1])

    def test_non_finite_json_rejected(self):
        with self.assertRaises(ParseError):
            decode_decision('{"action":{"type":"CLICK","parameters":{"point":[NaN,2]}}}')


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(3)
        self.task = compile_task("找四个评分超过4.8的课程加入书单")
        self.obs = observe(frame(2).current_image, 2)

    def item(self, key="a", name="课程 A", rating=4.9, status="added"):
        return dict(id=key, name=name, rating=rating, status=status, step=2, evidence="详情页评分和收藏状态")

    def test_extract_count_and_strict_rating(self):
        self.assertEqual(self.task.target_count, 4)
        self.assertFalse(self.task.qualify(4.8))
        self.assertTrue(self.task.qualify(4.9))
        self.assertTrue(compile_task("找4门评分不低于4.8的课程加入书单").qualify(4.8))

    def test_playlist_container_is_not_the_item_count(self):
        task = compile_task("把豆瓣音乐排行榜前五放到QQ音乐里新建一个歌单并播放")
        self.assertEqual(task.target_count, 5)
        self.assertEqual(compile_task("把前二十五首歌放进一个歌单").target_count, 25)

    def test_reading_favorites_is_not_an_add_task(self):
        self.assertFalse(compile_task("查看我的收藏列表").collection)

    def test_sorting_by_play_count_does_not_require_playing(self):
        task = compile_task("搜索赵丽颖筛选播放最多和最近一天和0-10分钟的视频")
        self.assertNotIn("playback", task.requirements)
        self.assertIn("playback", compile_task("搜索教程并播放播放量最高的").requirements)

    def test_issued_action_does_not_increment_count(self):
        self.ledger.issue(Decision("CLICK", {"point": [1, 2]}, expected_effect={"item_id": "a", "kind": "collection_added"}), 1)
        self.ledger.apply(Decision("WAIT", {"seconds": 1}, items=[self.item()]), self.obs, self.task)
        self.assertEqual(self.ledger.added_count, 0)

    def test_matching_next_frame_verifies_addition(self):
        self.ledger.issue(Decision("CLICK", {"point": [1, 2]}, expected_effect={"item_id": "a", "kind": "collection_added"}), 1)
        decision = Decision("WAIT", {"seconds": 1}, items=[self.item()], verification={
            "action_step": 1, "status": "confirmed", "evidence": "收藏显示已加入"})
        self.ledger.apply(decision, self.obs, self.task)
        self.assertEqual(self.ledger.added_count, 1)
        self.assertIsNone(self.ledger.pending)

    def test_wrong_action_step_does_not_verify(self):
        self.ledger.issue(Decision("CLICK", {"point": [1, 2]}, expected_effect={"item_id": "a", "kind": "collection_added"}), 1)
        self.ledger.apply(Decision("WAIT", {"seconds": 1}, items=[self.item()], verification={
            "action_step": 9, "status": "confirmed", "evidence": "已加入"}), self.obs, self.task)
        self.assertEqual(self.ledger.added_count, 0)
        self.assertIsNotNone(self.ledger.pending)

    def test_wait_keeps_unverified_action(self):
        self.ledger.issue(Decision("CLICK", {"point": [1, 2]}), 1)
        self.ledger.issue(Decision("WAIT", {"seconds": 1}), 2)
        self.assertEqual(self.ledger.pending["step"], 1)

    def test_below_threshold_item_rejected(self):
        self.ledger.issue(Decision("CLICK", {"point": [1, 2]}, expected_effect={"item_id": "a", "kind": "collection_added"}), 1)
        self.ledger.apply(Decision("WAIT", {"seconds": 1}, items=[self.item(rating=4.8)], verification={
            "action_step": 1, "status": "confirmed", "evidence": "已加入"}), self.obs, self.task)
        self.assertEqual(self.ledger.items["a"]["status"], "rejected")

    def test_same_name_new_id_not_counted_twice(self):
        self.ledger.apply(Decision("WAIT", {"seconds": 1}, items=[self.item(status="qualified")]), self.obs, self.task)
        issues = self.ledger.apply(Decision("WAIT", {"seconds": 1}, items=[self.item(key="b")]), self.obs, self.task)
        self.assertEqual(len(self.ledger.items), 1)
        self.assertTrue(any("已存在" in message for message in issues))

    def test_opening_details_is_not_an_add_confirmation(self):
        self.ledger.issue(Decision("CLICK", {"point": [1, 2]}, label="课程详情",
                                  expected_effect={"item_id": "a", "description": "打开详情"}), 1)
        self.ledger.apply(Decision("WAIT", {"seconds": 1}, items=[self.item()], verification={
            "action_step": 1, "status": "confirmed", "evidence": "详情已打开"}), self.obs, self.task)
        self.assertEqual(self.ledger.added_count, 0)

    def test_page_facts_expire_task_artifacts_survive(self):
        facts = [dict(key=scope, value="v", scope=scope, step=2, evidence="可见文字") for scope in ("page", "task")]
        self.ledger.apply(Decision("WAIT", {"seconds": 1}, facts=facts), self.obs, self.task)
        self.ledger.begin_observation(observe(frame(3).current_image, 3))
        self.assertEqual(self.ledger.facts["page"]["status"], "superseded")
        self.assertEqual(self.ledger.facts["task"]["status"], "confirmed")

    def test_old_and_future_evidence_ignored(self):
        facts = [dict(key=str(step), value="v", step=step, evidence="文字") for step in (1, 3, True)]
        self.ledger.apply(Decision("WAIT", {"seconds": 1}, facts=facts), self.obs, self.task)
        self.assertFalse(self.ledger.facts)


class AgentTests(unittest.TestCase):
    def test_malformed_negated_completion_is_wait(self):
        raw = "Thought: task not done yet\nAction: ???"
        agent = ScriptedAgent([raw, raw], {"retry_count": 1})
        result = agent.act(frame(1))
        self.assertEqual(result.action, "WAIT")
        self.assertEqual(result.usage.total_tokens, 14)

    def test_empty_response_retries_and_preserves_usage(self):
        agent = ScriptedAgent([None, "", ""])
        result = agent.act(frame(1))
        self.assertEqual(result.action, "WAIT")
        self.assertEqual(result.usage.total_tokens, 21)
        self.assertEqual(len(agent.calls), 3)

    def test_api_failure_returns_legal_wait(self):
        agent = ScriptedAgent([RuntimeError("offline")] * 3)
        result = agent.act(frame(1))
        self.assertTrue(TestRunner._validate_action(result.action, result.parameters)[0])
        self.assertEqual(result.action, "WAIT")

    def test_stale_packet_cannot_poison_memory(self):
        response = packet(9, facts=[dict(key="bad", value="v", step=1, evidence="text")])
        agent = ScriptedAgent([response])
        self.assertEqual(agent.act(frame(1)).action, "WAIT")
        self.assertFalse(agent._ledger.facts)

    def test_unknown_verification_keeps_original_pending(self):
        first = packet(1, "CLICK", {"point": [500, 500]})
        second = packet(2, "CLICK", {"point": [600, 500]}, pending_verification={
            "action_step": 1, "status": "unknown", "evidence": "页面仍在加载"})
        agent = ScriptedAgent([first, second])
        agent.act(frame(1))
        self.assertEqual(agent.act(frame(2)).action, "WAIT")
        self.assertEqual(agent._ledger.pending["step"], 1)

    def test_format_repair_reuses_screenshot_and_counts_both_calls(self):
        agent = ScriptedAgent(["bad output", packet(1, "TYPE", {"text": "中文 '引号' (括号)"})])
        result = agent.act(frame(1))
        self.assertEqual(result.action, "TYPE")
        self.assertEqual(result.parameters["text"], "中文 '引号' (括号)")
        self.assertEqual(result.usage.total_tokens, 14)

    def test_critic_invalid_and_stale_responses_fail_closed(self):
        checks = [dict(id="task_goal", satisfied=True, step=1, evidence="目标结果可见")]
        for response in ("not JSON", audit(9)):
            with self.subTest(response=response):
                agent = ScriptedAgent([packet(1, "COMPLETE", {}, checks=checks), response])
                self.assertEqual(agent.act(frame(1)).action, "WAIT")

    def test_scope_does_not_allow_unrequested_payment(self):
        response = packet(1, "CLICK", {"point": [500, 500]})
        response["action"]["label"] = "确认支付"
        agent = ScriptedAgent([response])
        self.assertEqual(agent.act(frame(1, "预订酒店，停在收银页不要支付")).action, "WAIT")

    def test_completion_requires_all_conditions_before_critic(self):
        agent = ScriptedAgent([packet(1, "COMPLETE", {}, checks=[dict(
            id="task_goal", satisfied=True, step=1, evidence="搜索结果出现")])])
        result = agent.act(frame(1, "播放指定歌曲"))
        self.assertEqual(result.action, "WAIT")
        self.assertEqual(agent._critic_calls, 0)

    def test_completion_critic_and_usage(self):
        checks = [dict(id="task_goal", satisfied=True, step=1, evidence="页面显示目标结果")]
        agent = ScriptedAgent([(packet(1, "COMPLETE", {}, checks=checks), 11), (audit(1), 17)])
        result = agent.act(frame(1))
        self.assertEqual(result.action, "COMPLETE")
        self.assertEqual(result.parameters, {})
        self.assertEqual(result.usage.total_tokens, 28)
        self.assertEqual(agent._task_tokens, 28)
        self.assertEqual(len(agent.calls), 2)

    def test_critic_rejection_prevents_completion(self):
        checks = [dict(id="task_goal", satisfied=True, step=1, evidence="结果可见")]
        agent = ScriptedAgent([packet(1, "COMPLETE", {}, checks=checks), audit(1, verdict="reject")])
        self.assertEqual(agent.act(frame(1)).action, "WAIT")

    def test_critic_cannot_skip_required_criterion(self):
        checks = [dict(id=key, satisfied=True, step=1, evidence="可见状态") for key in ("task_goal", "playback")]
        agent = ScriptedAgent([packet(1, "COMPLETE", {}, checks=checks), audit(1)])
        self.assertEqual(agent.act(frame(1, "播放歌曲")).action, "WAIT")

    def test_completion_fails_closed_when_critic_unavailable(self):
        checks = [dict(id="task_goal", satisfied=True, step=1, evidence="状态可见")]
        agent = ScriptedAgent([packet(1, "COMPLETE", {}, checks=checks)], {"max_critic_calls": 0})
        self.assertEqual(agent.act(frame(1)).action, "WAIT")

    def test_unconfirmed_final_click_blocks_completion(self):
        first = packet(1, "CLICK", {"point": [500, 500]}, expected_effect={"description": "播放开始"})
        checks = [dict(id="task_goal", satisfied=True, step=2, evidence="目标可见")]
        agent = ScriptedAgent([first, packet(2, "COMPLETE", {}, checks=checks)])
        self.assertEqual(agent.act(frame(1)).action, "CLICK")
        self.assertEqual(agent.act(frame(2)).action, "WAIT")

    def test_collect_flow_next_screenshot_then_complete(self):
        instruction = "找1个评分超过4.8的课程加入书单"
        item = dict(id="a", name="课程 A", rating=4.9, status="qualified", step=1, evidence="评分4.9")
        first = packet(1, "CLICK", {"point": [900, 240]}, items=[item],
                       expected_effect={"description": "课程加入书单", "item_id": "a"})
        item = dict(item, status="added", step=2, evidence="收藏已选中")
        checks = [dict(id=key, satisfied=True, step=2, evidence="符合要求的课程已收藏")
                  for key in ("task_goal", "collection")]
        second = packet(2, "COMPLETE", {}, items=[item], checks=checks,
                        pending_verification={"action_step": 1, "status": "confirmed", "evidence": "显示已收藏"})
        agent = ScriptedAgent([first, second, audit(2, {"task_goal": True, "collection": True})])
        self.assertEqual(agent.act(frame(1, instruction)).action, "CLICK")
        self.assertEqual(agent._ledger.added_count, 0)
        self.assertEqual(agent.act(frame(2, instruction)).action, "COMPLETE")
        self.assertEqual(agent._ledger.added_count, 1)

    def test_payment_blocked_without_model_audit(self):
        response = packet(1, "CLICK", {"point": [500, 500]})
        response["action"]["label"] = "立即支付"
        agent = ScriptedAgent([response])
        self.assertEqual(agent.act(frame(1)).action, "WAIT")
        self.assertEqual(len(agent.calls), 1)

    def test_below_threshold_addition_is_blocked(self):
        response = packet(1, "CLICK", {"point": [900, 240]},
                          items=[dict(id="a", name="课程 A", rating=4.8, status="qualified",
                                      step=1, evidence="课程评分4.8")], expected_effect={"item_id": "a"})
        response["action"]["label"] = "加入书单"
        agent = ScriptedAgent([response])
        result = agent.act(frame(1, "找4个评分超过4.8的课程加入书单"))
        self.assertEqual(result.action, "WAIT")
        self.assertEqual(len(agent.calls), 1)

    def test_legacy_completion_is_not_trusted(self):
        agent = ScriptedAgent(["Thought: 已完成\nAction: finished(content='')"])
        self.assertEqual(agent.act(frame(1)).action, "WAIT")

    def test_reset_clears_pending_memory_and_budgets(self):
        agent = ScriptedAgent([packet(1, "CLICK", {"point": [1, 2]})])
        agent.act(frame(1))
        agent.reset()
        self.assertIsNone(agent._task)
        self.assertIsNone(agent._ledger.pending)
        self.assertFalse(agent._ledger.history)
        self.assertEqual(agent._api_calls, 0)
        self.assertEqual(agent._task_tokens, 0)

    def test_budget_stops_api_without_false_completion(self):
        agent = ScriptedAgent([packet(1)], {"max_api_calls": 1})
        agent.act(frame(1))
        self.assertEqual(agent.act(frame(2)).action, "WAIT")
        self.assertEqual(len(agent.calls), 1)
        self.assertEqual(agent._ledger.status, "blocked")

    def test_same_model_and_url_are_preserved(self):
        agent = ScriptedAgent([])
        self.assertEqual(agent.model_id, DEFAULT_MODEL_ID)
        self.assertEqual(agent.api_url, DEFAULT_API_URL)

    def test_framework_token_limit_propagates(self):
        agent = ScriptedAgent([TokenLimitExceeded(100, 10)])
        with self.assertRaises(TokenLimitExceeded):
            agent.act(frame(1))


class ScopeTests(unittest.TestCase):
    def test_fixed_framework_files_unchanged(self):
        for name, expected in FIXED_HASHES.items():
            with self.subTest(file=name):
                self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), expected)

    def test_didi_does_not_receive_amap_skill(self):
        router = SkillRouter()
        task = compile_task("在滴滴出行查询经过博物馆的路线需要多少钱")
        ledger = Ledger()
        skills = router.select(task, ledger)
        self.assertNotIn("amap_nearby", [s["id"] for s in skills])
        self.assertIn("didi_route", [s["id"] for s in skills])

    def test_cross_app_launch_suspends_previous_app_skills(self):
        router = SkillRouter()
        ledger = Ledger()
        ledger.app = "QQ音乐"
        ledger.issue(Decision("OPEN", {"app_name": "高德地图"}), 1)
        skills = router.select(compile_task("QQ音乐歌单之后高德地图导航"), ledger)
        self.assertTrue(all(not s.get("apps") for s in skills))

    def test_aliases_use_existing_framework_names(self):
        self.assertEqual(normalize_app("B站"), "哔哩哔哩")
        self.assertEqual(normalize_app("高德"), "高德地图")


if __name__ == "__main__":
    unittest.main()
