"""Plan Assistant: an agent that turns a plain-English request into a PawPal+ schedule.

Loop (see diagrams/ai-system-design.mmd):
  PLAN/ACT  - Claude reads the request and calls tools (add_task, generate_schedule, ...)
  CHECK     - our own code rebuilds the schedule and looks for conflicts/unscheduled tasks
  REVISE    - if problems remain, they are sent back to Claude for another attempt
After MAX_ATTEMPTS failed attempts the agent stops guessing and asks the user a question.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date

import anthropic
from dotenv import load_dotenv

from agent_tools import PRIORITIES, TOOL_SCHEMAS, PlanToolbox
from evaluations.confidence import combine, parse_rating, rule_score
from pawpal_system import Pet

load_dotenv()  # reads ANTHROPIC_API_KEY from .env

MODEL = "claude-sonnet-5-5"
MAX_ATTEMPTS = 3         # revise cap before asking the user to clarify
MAX_TOOL_TURNS = 12      # safety cap on tool round-trips inside one attempt
MAX_INPUT_CHARS = 1000   # guardrail on request length
MIN_RATING = 0.7         # a model self-rating below this sends the plan back for revision

logger = logging.getLogger("pawpal.agent")


def setup_logging(path: str = "agent.log") -> None:
    """Send the agent's log lines to a file (once, even if called repeatedly)."""
    if any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        return
    handler = logging.FileHandler(path)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


@dataclass
class PlanResult:
    """What the agent hands back to the CLI/UI."""
    status: str                      # "ok", "needs_clarification" or "error"
    message: str = ""                # error text or the clarifying question
    schedule_lines: list[str] = field(default_factory=list)
    explanation: str = ""
    attempts: int = 0
    toolbox: PlanToolbox | None = None
    confidence: float | None = None  # 0.0-1.0, set only when status == "ok"
    confidence_note: str = ""        # how the score was reached, shown next to it
    not_due: list[str] = field(default_factory=list)  # saved tasks that aren't due on the plan date


def validate_request(request: str, pets: list[Pet], client) -> str | None:
    """Input guardrails. Returns an error message, or None if the request may proceed."""
    if not request or not request.strip():
        return "Please type a request, for example: 'Walk Max for 30 minutes every morning.'"
    if len(request) > MAX_INPUT_CHARS:
        return f"That request is too long ({len(request)} characters). Keep it under {MAX_INPUT_CHARS}."
    if not pets:
        return "Add at least one pet first so the assistant knows who the tasks are for."
    if client is None and not os.getenv("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY is missing. Add it to your .env file."
    return None


def build_system_prompt(pets: list[Pet], plan_date: date, toolbox: PlanToolbox) -> str:
    pet_lines = "\n".join(
        f"- {p.name} ({p.breed or 'unknown breed'}, age {p.age or '?'}; "
        f"health: {p.health_conditions or 'none'}; meds: {p.medications or 'none'})"
        for p in pets
    )
    return (
        "You are the PawPal+ Plan Assistant. Turn the owner's request into pet care tasks "
        "and a daily schedule using ONLY the provided tools.\n\n"
        f"Planning date: {plan_date:%A %Y-%m-%d}. Schedule window: "
        f"{toolbox.start:%H:%M}-{toolbox.end:%H:%M}.\n"
        f"Pets:\n{pet_lines}\n\n"
        f"Already saved in the owner's plan (never add these again; you cannot remove them):\n"
        f"{chr(10).join(_describe_task(t) for t in toolbox.tasks) or 'none'}\n\n"
        "Process: call add_task once per NEW task, then generate_schedule, then get_conflicts. "
        "The schedule covers saved and new tasks together. If there are conflicts or unscheduled "
        "tasks, fix them by changing the NEW tasks (remove_task and re-add with a different "
        "priority, time or duration) and check again. If the request only repeats tasks that are "
        "already saved, reply in words saying so without calling tools.\n"
        f"Priorities: {', '.join(PRIORITIES)}. Use fixed_start only when the owner names a time. "
        "Use repeat_days for recurring tasks (all seven days for daily).\n"
        "Never invent pets, times or durations the owner did not give or clearly imply. If "
        "something essential is missing or ambiguous, stop calling tools and say what is missing.\n"
        "When finished, reply with one short sentence summarizing the plan."
    )


def _call_claude(client, system: str, messages: list[dict], tools: list[dict] | None,
                 tool_choice: str = "auto"):
    """One Messages API call. Fallbacks reroute a safety refusal to a backup model.

    If the history contains earlier tool turns, always send the SAME tools list: the API
    ties Claude's saved reasoning to it and rejects the call otherwise. To stop Claude from
    using tools on a later call, pass tool_choice="none" instead of removing them.
    """
    kwargs = dict(model=MODEL, max_tokens=4096, system=system, messages=messages,
                  output_config={"effort": "medium"},
                  betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = {"type": tool_choice}
    return client.beta.messages.create(**kwargs)


def _run_pass(client, system: str, messages: list[dict], toolbox: PlanToolbox) -> tuple[str, bool]:
    """PLAN + ACT: let Claude call tools until it stops asking for them.

    Returns (Claude's last text, whether it called any tool). Text with no tool calls
    usually means Claude is asking the owner something.
    """
    made_calls, text = False, ""
    for _ in range(MAX_TOOL_TURNS):
        response = _call_claude(client, system, messages, TOOL_SCHEMAS)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "refusal":
            raise RuntimeError("The model declined this request.")
        text = " ".join(b.text for b in response.content if b.type == "text").strip()
        if response.stop_reason != "tool_use":
            return text, made_calls
        results = []
        for block in response.content:
            if block.type == "tool_use":
                made_calls = True
                output = toolbox.run(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(output), "is_error": "error" in output})
        messages.append({"role": "user", "content": results})  # all results in ONE message
    logger.warning("tool turn cap reached in one attempt")
    return text, made_calls


def check_plan(toolbox: PlanToolbox) -> list[str]:
    """CHECK: rebuild the schedule ourselves and list anything still wrong.

    We don't trust the model's claim that it checked; the scheduler is deterministic.
    """
    if not toolbox.new_tasks:
        return ["No new tasks were added. The request may be missing details."]
    toolbox.run("generate_schedule", {})
    schedule = toolbox.schedule
    problems = [f"Conflict: '{d.title}' overlaps '{k.title}'." for d, k in schedule.get_conflicts()]
    for task in schedule.get_unscheduled_tasks():
        reason = schedule.get_reason(task)
        if not any(task.title in p for p in problems):
            problems.append(f"Unscheduled: '{task.title}'. {reason}")
    return problems


def _ask_clarifying_question(client, system: str, messages: list[dict], problems: list[str]) -> str:
    """After the cap: have Claude write ONE specific question instead of guessing again."""
    ask = messages + [{"role": "user", "content": (
        "You could not produce a clean plan. Problems: " + " | ".join(problems) +
        "\nDo not call tools. Write ONE short, specific question for the owner whose "
        "answer would let you fix this (for example which task to drop or move).")}]
    response = _call_claude(client, system, ask, TOOL_SCHEMAS, tool_choice="none")
    return next((b.text for b in response.content if b.type == "text"), "").strip() or \
        "Could you tell me which task matters most, or give more detail about times and durations?"


def _describe_task(task) -> str:
    """One readable line per saved task, including its pets, time and repeat rule."""
    when = f"pinned to {task.fixed_start:%H:%M}" if task.fixed_start else "flexible time"
    pets = ", ".join(p.name for p in task.pets)
    return f"- {task.title}: {task.duration} min, {pets}, {when}, repeats: {task.describe_repeat()}"


def _self_rate(client, request: str, toolbox: PlanToolbox) -> tuple[float | None, str]:
    """Ask Claude how well the finished plan matches the request. (None, '') on any failure."""
    plan = "\n".join(f"{p.start_time:%H:%M}-{p.end_time:%H:%M} {p.title}"
                     for p in toolbox.schedule.get_tasks_by_time()) or "(nothing is due that day)"
    saved = "\n".join(_describe_task(t) + (" [saved earlier, not part of this request]"
                                           if id(t) in toolbox.locked else "")
                      for t in toolbox.tasks)
    prompt = (f"Owner's request:\n{request}\n\n"
              f"Tasks saved, with their repeat rules:\n{saved}\n\n"
              f"Schedule for {toolbox.plan_date:%A %Y-%m-%d} (only tasks due that day appear):\n{plan}\n\n"
              "Rate from 0.0 to 1.0 how completely and accurately the saved TASKS match the request "
              "(missing tasks, wrong times, durations, pets or repeat rules lower it). A repeating task "
              "that isn't due on the schedule date is correctly absent from that day's schedule; do not "
              "penalize that. Reply in exactly this format:\n"
              "RATING: <number> | REASON: <one short sentence>")
    try:
        response = _call_claude(client, "You are a strict reviewer of pet care plans.",
                                [{"role": "user", "content": prompt}], None)
        text = next((b.text for b in response.content if b.type == "text"), "")
        return parse_rating(text)
    except Exception:  # scoring must never break a good plan
        logger.exception("self-rating failed; using rule score only")
        return None, ""


def run_plan_assistant(request: str, pets: list[Pet], plan_date: date | None = None,
                       client=None, max_attempts: int = MAX_ATTEMPTS,
                       existing_tasks: list | None = None) -> PlanResult:
    """Run the whole agent. Never raises: every failure becomes a PlanResult.

    existing_tasks are the owner's already-saved tasks. They are scheduled together with the
    new ones but can't be removed, and result.toolbox.new_tasks holds only what this run added.
    """
    setup_logging()
    plan_date = plan_date or date.today()
    logger.info("request: %r (pets=%s, date=%s)", request, [p.name for p in pets], plan_date)

    error = validate_request(request, pets, client)
    if error:
        logger.warning("guardrail rejected request: %s", error)
        return PlanResult(status="error", message=error)

    try:
        client = client or anthropic.Anthropic()
        toolbox = PlanToolbox(pets, plan_date, existing_tasks=existing_tasks)
        system = build_system_prompt(pets, plan_date, toolbox)
        messages: list[dict] = [{"role": "user", "content": request.strip()}]
        problems: list[str] = []

        for attempt in range(1, max_attempts + 1):
            logger.info("attempt %d/%d", attempt, max_attempts)
            text, made_calls = _run_pass(client, system, messages, toolbox)
            if not made_calls and not toolbox.new_tasks and text:
                # Claude used no tools and replied in words: it is asking the owner something
                # (or pointing out that the request repeats tasks that are already saved).
                logger.info("model asked the owner directly: %s", text)
                return PlanResult(status="needs_clarification", message=text,
                                  attempts=attempt, toolbox=toolbox)
            problems = check_plan(toolbox)
            logger.info("check after attempt %d: %s", attempt, problems or "clean")
            if not problems:
                schedule = toolbox.schedule
                rule = rule_score(toolbox, attempt)
                rating, reason = _self_rate(client, request, toolbox)
                if rating is not None and rating < MIN_RATING and attempt < max_attempts:
                    logger.info("self-rating %.2f is below %.2f; revising: %s", rating, MIN_RATING, reason)
                    messages.append({"role": "user", "content": (
                        f"A reviewer rated this plan {rating:.2f} against the owner's request: {reason}\n"
                        "If that is a real gap, fix it with the tools. If the request is ambiguous, "
                        "ask the owner instead of guessing.")})
                    continue
                score = combine(rule, rating)
                note = (f"rule {rule:.2f}, model {rating:.2f}" if rating is not None
                        else f"rule {rule:.2f}, model rating unavailable") + (f" ({reason})" if reason else "")
                logger.info("confidence %.2f: %s", score, note)
                return PlanResult(
                    status="ok", attempts=attempt, toolbox=toolbox, explanation=schedule.explain(),
                    confidence=score, confidence_note=note,
                    not_due=[f"{t.title} ({t.describe_repeat()})" for t in schedule.get_not_due_tasks()],
                    schedule_lines=[f"{p.start_time:%H:%M}-{p.end_time:%H:%M} {p.title}"
                                    for p in schedule.get_tasks_by_time()])
            messages.append({"role": "user", "content": (
                "Revise the plan. Problems found by the checker:\n- " + "\n- ".join(problems))})

        question = _ask_clarifying_question(client, system, messages, problems)
        logger.info("cap reached; asking user: %s", question)
        return PlanResult(status="needs_clarification", message=question,
                          attempts=max_attempts, toolbox=toolbox)
    except anthropic.AuthenticationError:
        msg = "The API key was rejected. Check ANTHROPIC_API_KEY in your .env file."
    except anthropic.RateLimitError:
        msg = "The API is rate limiting requests. Wait a moment and try again."
    except anthropic.APIConnectionError:
        msg = "Couldn't reach the Anthropic API. Check your internet connection."
    except anthropic.APIStatusError as exc:
        msg = f"The API returned an error ({exc.status_code}). Try again shortly."
    except Exception as exc:  # last-resort guard, includes the refusal RuntimeError
        logger.exception("agent crashed")
        msg = f"Something went wrong: {exc}"
    logger.error("agent failed: %s", msg)
    return PlanResult(status="error", message=msg)
