"""Evaluation harness for the Plan Assistant.

    python evaluations/eval.py                 # offline: scripted fake Claude, free, repeatable
    python evaluations/eval.py --live          # real Anthropic API (costs a little; needs ANTHROPIC_API_KEY)
    python evaluations/eval.py --live --runs 3 # run each case 3 times to check consistency

Offline mode tests everything AROUND the model (guardrails, the check/revise loop, the
attempt cap, confidence scoring) because the model's replies are scripted. Live mode is
the one that tests the model itself.
Results are printed and saved as a markdown table in evaluations/results/
(eval_results_offline.md or eval_results_live.md).
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# This file lives in evaluations/, so put the project root on the import path. That lets
# `python evaluations/eval.py` find plan_assistant, fake_claude and the other root modules.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
RESULTS_DIR = Path(__file__).resolve().parent / "results"

from fake_claude import FakeClaude, reply, say, tool_call  # noqa: E402
from pawpal_system import Pet  # noqa: E402
from plan_assistant import PlanResult, run_plan_assistant  # noqa: E402

load_dotenv()

EVAL_DATE = date(2026, 11, 16)  # fixed so results don't depend on the day it runs
END = reply("end_turn", say("Done."))
RATE = reply("end_turn", say("RATING: 0.9 | REASON: Matches the request."))


def add(n: int, title: str, minutes: int, pet: str, priority: str = "high", fixed: str | None = None):
    """Shorthand for a scripted add_task call."""
    args = dict(title=title, duration=minutes, priority=priority, pet_names=[pet])
    if fixed:
        args["fixed_start"] = fixed
    return tool_call(n, "add_task", **args)


@dataclass
class Case:
    name: str
    request: str
    expect_status: str                                    # "ok", "needs_clarification" or "error"
    expect_in_plan: list[str] = field(default_factory=list)  # lowercase words that must appear in the plan
    pets: list[str] = field(default_factory=lambda: ["Max", "Luna"])
    script: list = field(default_factory=list)            # offline replies; ignored in live mode


CASES = [
    Case("Simple task", "Walk Max for 30 minutes", "ok", ["walk"],
         script=[reply("tool_use", add(1, "Walk", 30, "Max")), END, RATE]),
    Case("Pinned time", "Give Luna her medication at 9am for 5 minutes", "ok", ["09:00", "med"],
         script=[reply("tool_use", add(1, "Medication", 5, "Luna", fixed="09:00")), END, RATE]),
    Case("Three tasks, two pets", "Walk Max 30 min, feed Luna 10 min, and play with Max for 20 min",
         "ok", ["walk", "feed", "play"],
         script=[reply("tool_use", add(1, "Walk", 30, "Max"), add(2, "Feed", 10, "Luna", "medium"),
                       add(3, "Play", 20, "Max", "low")), END, RATE]),
    Case("Fixable conflict", "Vet visit for Luna at 10am for 60 minutes, and grooming for Max "
         "around 10:30 for 45 minutes, but I'm flexible on the grooming time", "ok", ["vet", "groom"],
         script=[reply("tool_use", add(1, "Vet visit", 60, "Luna", fixed="10:00"),
                       add(2, "Grooming", 45, "Max", "medium", fixed="10:30")), END,
                 reply("tool_use", tool_call(3, "remove_task", title="Grooming"),
                       add(4, "Grooming", 45, "Max", "medium")), END, RATE]),
    Case("Impossible conflict", "Vet for Luna at exactly 2pm for 60 minutes AND grooming for Max at "
         "exactly 2pm for 60 minutes. Neither can move.", "needs_clarification",
         script=[reply("tool_use", add(1, "Vet", 60, "Luna", fixed="14:00"),
                       add(2, "Grooming", 60, "Max", "medium", fixed="14:00")), END, END, END,
                 reply("end_turn", say("Which of the two 2pm appointments can move?"))]),
    Case("Unknown pet", "Walk Rex for 30 minutes", "needs_clarification",
         script=[reply("tool_use", add(1, "Walk", 30, "Rex")), END]),
    Case("Vague request", "Do something nice for my pets", "needs_clarification"),
    Case("Empty input", "   ", "error"),
    Case("Oversized input", "walk the dog " * 200, "error"),
    Case("No pets added", "Walk the dog for 20 minutes", "error", pets=[]),
]


def run_case(case: Case, live: bool) -> PlanResult:
    pets = [Pet(name) for name in case.pets]
    client = None if live else FakeClaude(case.script)
    return run_plan_assistant(case.request, pets, EVAL_DATE, client=client)


def passed(case: Case, result: PlanResult) -> bool:
    if result.status != case.expect_status:
        return False
    plan = " ".join(result.schedule_lines).lower()
    return all(word in plan for word in case.expect_in_plan)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the Plan Assistant.")
    parser.add_argument("--live", action="store_true", help="use the real Anthropic API")
    parser.add_argument("--runs", type=int, default=1, help="runs per case (consistency check)")
    args = parser.parse_args()
    if args.live and not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is missing. Add it to .env before using --live.")
        return 1

    mode = "live" if args.live else "offline"
    rows, passes, total, confidences, attempts_used = [], 0, 0, [], []
    for number, case in enumerate(CASES, start=1):
        outcomes = [run_case(case, args.live) for _ in range(args.runs)]
        wins = sum(passed(case, r) for r in outcomes)
        passes, total = passes + wins, total + len(outcomes)
        last = outcomes[-1]
        for r in outcomes:
            if r.status == "ok":
                confidences.append(r.confidence)
                attempts_used.append(r.attempts)
        conf = f"{last.confidence:.2f}" if last.confidence is not None else "-"
        verdict = "Pass" if wins == len(outcomes) else f"Fail ({wins}/{len(outcomes)})"
        rows.append(f"| {number} | {case.name} | {case.expect_status} | {last.status} | "
                    f"{last.attempts} | {conf} | {verdict} |")

    lines = [f"## Plan Assistant eval ({mode} mode, {args.runs} run(s) per case)", "",
             "| # | Test | Expected | Actual | Attempts | Confidence | Result |",
             "|:--|:--|:--|:--|:--|:--|:--|", *rows, "",
             f"**{passes} of {total} runs passed.**"]
    if confidences:
        lines.append(f"Average confidence on successful plans: {sum(confidences) / len(confidences):.2f}; "
                     f"average attempts: {sum(attempts_used) / len(attempts_used):.1f}.")
    report = "\n".join(lines)
    print(report)
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / f"eval_results_{mode}.md").write_text(report + "\n")
    return 0 if passes == total else 1


if __name__ == "__main__":
    sys.exit(main())
