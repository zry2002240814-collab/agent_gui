"""Validated internal decisions, kept separate from AgentOutput."""

from dataclasses import dataclass, field
import re
from typing import Any, Dict, List


class ParseError(ValueError):
    """A model response was not a valid decision; never implies completion."""


def collection_add(label, effect):
    return (effect.get("kind") == "collection_added"
            or bool(re.search(r"加入书单|加到歌单|加入收藏|添加到", label))
            or isinstance(effect.get("description"), str) and bool(re.search(
                r"加入书单|加入歌单|加入收藏|添加.*(?:书单|歌单|收藏)|收藏(?:成功|选中)", effect["description"])))


@dataclass
class Decision:
    action: str
    parameters: Dict[str, Any]
    summary: str = ""
    label: str = ""
    observation_id: str = ""
    phase: str = ""
    next_objective: str = ""
    expected_effect: Dict[str, Any] = field(default_factory=dict)
    observation: Dict[str, Any] = field(default_factory=dict)
    verification: Dict[str, Any] = field(default_factory=dict)
    facts: List[Dict[str, Any]] = field(default_factory=list)
    items: List[Dict[str, Any]] = field(default_factory=list)
    checks: List[Dict[str, Any]] = field(default_factory=list)
    requirements: List[Dict[str, Any]] = field(default_factory=list)
    plan: List[str] = field(default_factory=list)


def bounded_int(config, key, default, minimum, maximum):
    value = config.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value
