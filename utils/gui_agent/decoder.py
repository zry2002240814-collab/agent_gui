"""Strict JSON decisions and safe compatibility with explicit Action: calls."""

import ast
import json
import math
import re

from agent_base import VALID_ACTIONS
from .contracts import Decision, ParseError

ALIASES = {"TAP": "CLICK", "SWIPE": "SCROLL", "INPUT": "TYPE",
           "OPEN_APP": "OPEN", "FINISHED": "COMPLETE", "FINISH": "COMPLETE",
           "REQUEST_COMPLETION": "COMPLETE"}


def json_object(text):
    if not isinstance(text, str) or len(text) > 100000:
        raise ParseError("response must be bounded text")
    cleaned = re.sub(r"<think[^>]*>.*?</think[^>]*>", "", text,
                     flags=re.I | re.S).strip()
    if "<think" in cleaned.lower():
        raise ParseError("unclosed thinking block")
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.I | re.S)
    candidate = fenced.group(1).strip() if fenced else cleaned
    try:
        data = json.loads(candidate, parse_constant=lambda s: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON value: {s}")))
    except (ValueError, TypeError) as exc:
        raise ParseError("expected a JSON object") from exc
    if not isinstance(data, dict):
        raise ParseError("expected a JSON object")
    return data


def _number(value):
    if isinstance(value, bool):
        raise ParseError("boolean is not a coordinate or duration")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ParseError("invalid numeric parameter") from exc
    if not math.isfinite(number):
        raise ParseError("non-finite numeric parameter")
    return int(round(number))


def _point(value):
    if isinstance(value, str):
        value = re.sub(r"</?point>", "", value, flags=re.I).strip(" []")
        value = re.split(r"[\s,]+", value)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ParseError("point must contain exactly two coordinates")
    return [max(0, min(1000, _number(v))) for v in value]


def normalize_action(name, params):
    if not isinstance(name, str) or not isinstance(params, dict):
        raise ParseError("action type and parameters required")
    action = ALIASES.get(name.upper(), name.upper())
    if action not in VALID_ACTIONS:
        raise ParseError(f"unknown action {name}")
    if action in {"CLICK", "DOUBLE_CLICK", "LONG_PRESS"}:
        result = {"point": _point(params.get("point"))}
        if action == "LONG_PRESS":
            result["duration"] = max(100, min(5000, _number(params.get("duration", 1000))))
    elif action in {"SCROLL", "DRAG"}:
        if "start_point" in params and "end_point" in params:
            start, end = _point(params["start_point"]), _point(params["end_point"])
        elif action == "SCROLL" and "point" in params:
            start = _point(params["point"])
            direction = params.get("direction", "down")
            # direction describes content to reveal, not the finger's movement.
            offsets = {"down": (0, -300), "up": (0, 300),
                       "right": (-300, 0), "left": (300, 0)}
            if direction not in offsets:
                raise ParseError("invalid scroll direction")
            dx, dy = offsets[direction]
            end = _point([start[0] + dx, start[1] + dy])
        else:
            raise ParseError("start_point and end_point required")
        if start == end:
            raise ParseError("swipe has zero displacement")
        result = {"start_point": start, "end_point": end}
    elif action == "TYPE":
        text = params.get("text", params.get("content"))
        if not isinstance(text, str) or not text or len(text) > 4000:
            raise ParseError("TYPE requires nonempty bounded text")
        result = {"text": "\n" if text.strip().lower() in {"<enter>", "回车"} else text}
    elif action == "OPEN":
        name = params.get("app_name", params.get("app"))
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ParseError("OPEN requires an app name")
        result = {"app_name": name.strip()}
    elif action == "WAIT":
        result = {"seconds": max(1, min(10, _number(params.get("seconds", 1))))}
    else:
        result = {}
    return action, result


def _records(data, name):
    value = data.get(name, [])
    if not isinstance(value, list) or len(value) > 100 or any(not isinstance(v, dict) for v in value):
        raise ParseError(f"{name} must be a bounded list of objects")
    return value


def decode_decision(text):
    try:
        data = json_object(text)
    except ParseError:
        # Legacy compatibility is restricted to the last explicit Action line.
        lines = re.findall(r"^\s*Action\s*:\s*(.+)$", text or "", re.I | re.M)
        if not lines or "<think" in (text or "").lower():
            raise ParseError("no valid JSON or explicit action")
        try:
            call = ast.parse(lines[-1].strip(), mode="eval").body
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.args:
                raise ValueError("expected a keyword-only action call")
            if any(k.arg is None for k in call.keywords):
                raise ValueError("keyword expansion is forbidden")
            params = {k.arg: ast.literal_eval(k.value) for k in call.keywords}
            if len(params) != len(call.keywords):
                raise ValueError("duplicate keyword")
            action, params = normalize_action(call.func.id, params)
        except (SyntaxError, ValueError, TypeError, RecursionError) as exc:
            raise ParseError("invalid action call") from exc
        thought = re.search(r"^\s*Thought\s*:\s*(.*)$", text, re.I | re.M)
        return Decision(action, params, summary=(thought.group(1)[:250] if thought else ""))
    action_data = data.get("action")
    if not isinstance(action_data, dict):
        raise ParseError("action must be an object")
    params = action_data.get("parameters", {})
    if not isinstance(params, dict):
        raise ParseError("parameters must be an object")
    target = action_data.get("target")
    if target is not None:
        if not isinstance(target, dict) or target.get("kind", "point") != "point":
            raise ParseError("only screenshot point targets are supported")
        params = dict(params, point=target.get("point"))
    action, params = normalize_action(action_data.get("type"), params)
    mappings = {}
    for field in ("expected_effect", "observation", "pending_verification"):
        value = data.get(field, {})
        if not isinstance(value, dict):
            raise ParseError(f"{field} must be an object")
        mappings[field] = value
    strings = {}
    for field in ("decision_summary", "observation_id", "phase", "next_objective"):
        value = data.get(field, "")
        if not isinstance(value, str) or len(value) > 2000:
            raise ParseError(f"invalid {field}")
        strings[field] = value
    if not strings["observation_id"]:
        raise ParseError("structured decisions must identify the current observation")
    plan = data.get("plan", [])
    if not isinstance(plan, list) or len(plan) > 16 or any(not isinstance(v, str) for v in plan):
        raise ParseError("plan must be a list of strings")
    label = action_data.get("label", (target or {}).get("label", ""))
    if not isinstance(label, str) or len(label) > 300:
        raise ParseError("invalid action label")
    return Decision(action, params, summary=strings["decision_summary"], label=label,
                    observation_id=strings["observation_id"], phase=strings["phase"],
                    next_objective=strings["next_objective"], plan=plan,
                    expected_effect=mappings["expected_effect"], observation=mappings["observation"],
                    verification=mappings["pending_verification"],
                    facts=_records(data, "facts"), items=_records(data, "items"),
                    checks=_records(data, "checks"), requirements=_records(data, "requirements"))
