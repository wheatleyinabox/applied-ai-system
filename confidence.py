"""Confidence scoring for Plan Assistant results.

Final score = average of two signals, each from 0.0 to 1.0:
  rule score   - computed from facts about the run (attempts used, schedule slack)
  model rating - Claude's own rating of how well the plan matches the request
If the model rating is unavailable, the rule score is used alone.
"""
from __future__ import annotations

import re

from agent_tools import PlanToolbox

ATTEMPT_PENALTY = 0.2   # lost per extra revise attempt (1.0, 0.8, 0.6 for attempts 1-3)
MIN_SLACK = 0.10        # a plan using more than 90% of the window is considered fragile
SLACK_FACTOR = 0.8


def rule_score(toolbox: PlanToolbox, attempts: int) -> float:
    """Confidence from run facts. Needs a built schedule; returns 0.0 if there isn't one."""
    schedule = toolbox.schedule
    if schedule is None or not schedule.get_tasks():
        return 0.0
    score = max(1.0 - ATTEMPT_PENALTY * (attempts - 1), 0.0)
    window = (toolbox.end.hour - toolbox.start.hour) * 60 + (toolbox.end.minute - toolbox.start.minute)
    if window > 0 and schedule.get_available_time() / window < MIN_SLACK:
        score *= SLACK_FACTOR
    return round(score, 2)


def parse_rating(text: str) -> tuple[float | None, str]:
    """Pull 'RATING: 0.85 | REASON: ...' out of Claude's reply. (None, '') if malformed."""
    match = re.search(r"RATING:\s*([01](?:\.\d+)?)", text or "")
    if not match:
        return None, ""
    rating = float(match.group(1))
    if not 0.0 <= rating <= 1.0:
        return None, ""
    reason = re.search(r"REASON:\s*(.+)", text, re.DOTALL)
    return rating, reason.group(1).strip() if reason else ""


def combine(rule: float, model_rating: float | None) -> float:
    """Average the two signals, or fall back to the rule score alone."""
    if model_rating is None:
        return round(rule, 2)
    return round((rule + model_rating) / 2, 2)
