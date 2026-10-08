"""Static, manually maintained skills with explicit App filtering."""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class SkillRouter:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(__file__).resolve().parents[1] / "gui_agent_skills.json"
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("skill library must be a list")
        self.skills = []
        for skill in data:
            if (not isinstance(skill, dict) or not isinstance(skill.get("id"), str)
                    or not isinstance(skill.get("instructions"), str)
                    or not isinstance(skill.get("apps", []), list)
                    or not isinstance(skill.get("keywords", []), list)
                    or any(not isinstance(v, str) for v in skill.get("apps", []) + skill.get("keywords", []))):
                raise ValueError("invalid static skill")
            self.skills.append(skill)

    def select(self, task, ledger, limit=3, recovering=False):
        app = ledger.app
        if ledger.pending and ledger.pending["action"] == "OPEN":
            app = ""  # launching another App is not evidence that it opened
        if not app and not ledger.history and task.apps:
            app = task.apps[0]
        text = task.instruction + " " + ledger.phase + " " + ledger.next_objective
        ranked = []
        for skill in self.skills:
            if skill.get("status", "active") != "active":
                continue
            apps = skill.get("apps", [])
            if apps and app not in apps:
                continue
            if skill.get("category") == "recovery" and not recovering:
                continue
            matches = sum(word.casefold() in text.casefold() for word in skill.get("keywords", []))
            score = matches + (3 if apps else 0) + (5 if recovering and skill.get("category") == "recovery" else 0)
            if score or skill.get("always", False):
                ranked.append((score, skill["id"], skill))
        return [entry[2] for entry in sorted(ranked, key=lambda x: (-x[0], x[1]))[:limit]]
