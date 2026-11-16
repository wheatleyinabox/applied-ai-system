"""Tests for the Plan Assistant: tools, confidence scoring and the agent loop.

No test here calls the real API. The agent is driven by FakeClaude's scripted replies.
"""
from datetime import date, time

import pytest

from agent_tools import PlanToolbox
from confidence import combine, parse_rating, rule_score
from fake_claude import FakeClaude, reply, say, tool_call
from pawpal_system import Pet
from plan_assistant import MAX_INPUT_CHARS, run_plan_assistant

DAY = date(2026, 11, 16)  # a Monday
END = reply("end_turn", say("Done."))
RATE = reply("end_turn", say("RATING: 0.9 | REASON: Looks right."))


@pytest.fixture
def pets():
    return [Pet("Max"), Pet("Luna")]


@pytest.fixture
def toolbox(pets):
    return PlanToolbox(pets, DAY)


def add_args(**overrides):
    args = dict(title="Walk", duration=30, priority="high", pet_names=["Max"])
    args.update(overrides)
    return args


def add(n, **overrides):
    return tool_call(n, "add_task", **add_args(**overrides))


# ---- tools ------------------------------------------------------------
def test_add_task_creates_a_real_task_with_the_right_values(toolbox, pets):
    result = toolbox.run("add_task", add_args(fixed_start="09:00", repeat_days=["Mon", "Wed"]))
    task = toolbox.tasks[0]
    assert result["ok"] is True
    assert (task.title, task.duration, task.priority) == ("Walk", 30, 3)
    assert task.fixed_start == time(9, 0) and task.repeat_days == [0, 2]
    assert task.pets[0] is pets[0]


def test_pet_names_are_case_insensitive(toolbox):
    assert toolbox.run("add_task", add_args(pet_names=["mAx"]))["ok"] is True


@pytest.mark.parametrize("bad, message", [
    (dict(title="  "), "title"),
    (dict(duration=0), "Duration"),
    (dict(duration=500), "Duration"),
    (dict(duration=True), "Duration"),
    (dict(priority="urgent"), "Priority"),
    (dict(pet_names=[]), "at least one pet"),
    (dict(pet_names=["Rex"]), "Unknown pet"),
    (dict(fixed_start="9am"), "Invalid time"),
    (dict(fixed_start="19:45"), "window"),
    (dict(repeat_days=["Funday"]), "repeat_days"),
    (dict(on_date="tomorrow"), "on_date"),
])
def test_add_task_rejects_bad_input_without_adding(toolbox, bad, message):
    result = toolbox.run("add_task", add_args(**bad))
    assert message in result["error"]
    assert toolbox.tasks == []


def test_missing_arguments_and_unknown_tools_become_errors_not_crashes(toolbox):
    assert "Bad arguments" in toolbox.run("add_task", {"title": "X"})["error"]
    assert "Unknown tool" in toolbox.run("delete_everything", {})["error"]


def test_remove_task(toolbox):
    toolbox.run("add_task", add_args())
    assert toolbox.run("remove_task", {"title": "walk"})["removed"] == 1
    assert "No task titled" in toolbox.run("remove_task", {"title": "walk"})["error"]


def test_generate_schedule_needs_tasks_and_get_conflicts_needs_a_schedule(toolbox):
    assert "No tasks" in toolbox.run("generate_schedule", {})["error"]
    assert "generate_schedule first" in toolbox.run("get_conflicts", {})["error"]


def test_conflicts_report_the_dropped_task_and_reason(toolbox):
    toolbox.run("add_task", add_args(title="Meds", pet_names=["Luna"], duration=5, fixed_start="09:00"))
    toolbox.run("add_task", add_args(title="Vet", priority="medium", pet_names=["Luna"], duration=60,
                                     fixed_start="09:00"))
    toolbox.run("generate_schedule", {})
    report = toolbox.run("get_conflicts", {})
    assert report["conflicts"] == ["'Vet' overlaps 'Meds'"]
    assert "higher priority" in report["unscheduled"][0]["reason"]


# ---- confidence -------------------------------------------------------
def test_rule_score_drops_with_each_extra_attempt(toolbox):
    toolbox.run("add_task", add_args())
    toolbox.run("generate_schedule", {})
    assert [rule_score(toolbox, n) for n in (1, 2, 3)] == [1.0, 0.8, 0.6]


def test_rule_score_is_zero_without_a_schedule(toolbox):
    assert rule_score(toolbox, 1) == 0.0


def test_rule_score_penalizes_a_nearly_full_day(toolbox):
    toolbox.run("add_task", add_args(duration=220))
    toolbox.run("add_task", add_args(title="Play", duration=20))  # 240 of 720 min is fine
    toolbox.run("generate_schedule", {})
    assert rule_score(toolbox, 1) == 1.0
    for i in range(2):  # fill most of the 12-hour window
        toolbox.run("add_task", add_args(title=f"Long{i}", duration=240))
    toolbox.run("generate_schedule", {})
    assert rule_score(toolbox, 1) == 0.8


@pytest.mark.parametrize("text, expected", [
    ("RATING: 0.85 | REASON: Good.", (0.85, "Good.")),
    ("RATING: 1 | REASON: Perfect", (1.0, "Perfect")),
    ("RATING: 0.5", (0.5, "")),
    ("pretty good!", (None, "")),
    ("RATING: 7 | REASON: x", (None, "")),
    ("", (None, "")),
])
def test_parse_rating(text, expected):
    assert parse_rating(text) == expected


def test_combine_averages_or_falls_back():
    assert combine(0.8, 0.9) == 0.85
    assert combine(0.8, None) == 0.8


# ---- agent loop -------------------------------------------------------
def run(request, pets, replies):
    return run_plan_assistant(request, pets, DAY, client=FakeClaude(replies))


def test_clean_request_succeeds_on_attempt_one(pets):
    result = run("Walk Max", pets, [reply("tool_use", add(1)), END, RATE])
    assert (result.status, result.attempts) == ("ok", 1)
    assert result.schedule_lines == ["08:00-08:30 Walk"]
    assert result.confidence == 0.95


def test_conflict_is_sent_back_and_fixed_on_attempt_two(pets):
    pinned = reply("tool_use", add(1, title="A", fixed_start="09:00"),
                   add(2, title="B", priority="medium", fixed_start="09:00"))
    fix = reply("tool_use", tool_call(3, "remove_task", title="B"), add(4, title="B", priority="medium"))
    result = run("two at 9", pets, [pinned, END, fix, END, RATE])
    assert (result.status, result.attempts) == ("ok", 2)
    assert result.confidence == 0.85  # rule 0.8 and model 0.9


def test_unfixable_request_asks_a_question_after_exactly_max_attempts(pets):
    pinned = reply("tool_use", add(1, title="A", fixed_start="09:00"),
                   add(2, title="B", priority="medium", fixed_start="09:00"))
    result = run("two at 9", pets, [pinned, END, END, END, reply("end_turn", say("Which one can move?"))])
    assert result.status == "needs_clarification"
    assert result.attempts == 3 and result.message == "Which one can move?"
    assert result.confidence is None


def test_tool_errors_are_returned_to_the_model_as_errors(pets):
    seen = []

    class Spy(FakeClaude):
        def _create(self, **kwargs):
            seen.append(kwargs["messages"][-1])
            return super()._create(**kwargs)

    run_plan_assistant("Walk Rex", pets, DAY, client=Spy([reply("tool_use", add(1, pet_names=["Rex"])), END]))
    tool_result = seen[1]["content"][0]  # what the model saw right after its bad add_task
    assert tool_result["is_error"] is True and "Unknown pet" in tool_result["content"]


class Recorder(FakeClaude):
    """FakeClaude that remembers every request the agent sent."""
    def __init__(self, replies):
        super().__init__(replies)
        self.calls = []

    def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return super()._create(**kwargs)


# A: text with no tool calls is a question for the owner, shown immediately
def test_model_asking_a_question_is_returned_at_once_without_retries(pets):
    client = Recorder([reply("end_turn", say("How long should the supplement take?"))])
    result = run_plan_assistant("walk Max and give supplements", pets, DAY, client=client)
    assert result.status == "needs_clarification"
    assert result.message == "How long should the supplement take?"
    assert result.attempts == 1 and len(client.calls) == 1


def test_text_only_reply_after_tasks_exist_is_not_treated_as_a_question(pets):
    result = run("Walk Max", pets, [reply("tool_use", add(1)), END, END, RATE])
    assert result.status == "ok"


# B: a low model rating sends the plan back for revision
def test_low_self_rating_triggers_a_revision_with_the_reason(pets):
    low = reply("end_turn", say("RATING: 0.4 | REASON: The supplement task is missing."))
    second = reply("tool_use", add(2, title="Supplement", duration=5))
    client = Recorder([reply("tool_use", add(1)), END, low, second, END, RATE])
    result = run_plan_assistant("walk and supplement", pets, DAY, client=client)
    assert (result.status, result.attempts) == ("ok", 2)
    assert result.confidence == 0.85  # attempt 2: rule 0.8, model 0.9
    revise_message = next(m for c in client.calls for m in c["messages"]
                          if m["role"] == "user" and "reviewer rated" in str(m["content"]))
    assert "supplement task is missing" in revise_message["content"]


def test_low_rating_on_the_last_attempt_is_accepted_with_low_confidence(pets):
    low = reply("end_turn", say("RATING: 0.4 | REASON: Not great."))
    result = run_plan_assistant("Walk Max", pets, DAY, client=FakeClaude([reply("tool_use", add(1)), END, low]),
                                max_attempts=1)
    assert result.status == "ok" and result.confidence == 0.7  # (1.0 + 0.4) / 2


# C: repeating tasks that aren't due on the plan date
def test_task_not_due_on_the_plan_date_gives_an_ok_plan_that_says_so(pets):
    inhaler = reply("tool_use", add(1, title="Inhaler", pet_names=["Luna"], repeat_days=["Tue"]))
    client = Recorder([inhaler, END, RATE])
    result = run_plan_assistant("Luna's inhaler every Tuesday", pets, DAY, client=client)  # DAY is a Monday
    assert result.status == "ok" and result.schedule_lines == []
    assert result.not_due == ["Inhaler (Tue)"]
    assert result.confidence == 0.95  # not 0: an empty day is legitimate here
    rating_prompt = client.calls[-1]["messages"][0]["content"]
    assert "repeats: Tue" in rating_prompt and "nothing is due that day" in rating_prompt


def test_rule_score_is_not_zero_when_tasks_exist_but_none_are_due(toolbox):
    toolbox.run("add_task", add_args(repeat_days=["Tue"]))
    toolbox.run("generate_schedule", {})
    assert rule_score(toolbox, 1) == 1.0


def test_clarifying_question_call_keeps_the_tools_list_but_forbids_tool_use(pets):
    """Regression: the live API rejected this call when the tools list was dropped."""
    calls = []

    class Spy(FakeClaude):
        def _create(self, **kwargs):
            calls.append(kwargs)
            return super()._create(**kwargs)

    pinned = reply("tool_use", add(1, title="A", fixed_start="09:00"),
                   add(2, title="B", priority="medium", fixed_start="09:00"))
    run_plan_assistant("two at 9", pets, DAY, client=Spy([pinned, END, END, END]))
    question_call = calls[-1]
    assert question_call["tools"] == calls[0]["tools"]
    assert question_call["tool_choice"] == {"type": "none"}


def test_malformed_self_rating_falls_back_to_rule_score(pets):
    result = run("Walk Max", pets, [reply("tool_use", add(1)), END, reply("end_turn", say("nice"))])
    assert result.status == "ok" and result.confidence == 1.0
    assert "unavailable" in result.confidence_note


def test_failed_self_rating_call_does_not_break_the_plan(pets):
    class Boom(FakeClaude):
        def _create(self, **kwargs):
            if not self.replies:
                raise RuntimeError("rating call failed")
            return super()._create(**kwargs)

    result = run_plan_assistant("Walk Max", pets, DAY, client=Boom([reply("tool_use", add(1)), END]))
    assert result.status == "ok" and result.confidence == 1.0


def test_refusal_becomes_an_error_result(pets):
    result = run("Walk Max", pets, [reply("refusal", say(""))])
    assert result.status == "error" and "declined" in result.message


@pytest.mark.parametrize("request_text, fragment", [
    ("", "type a request"),
    ("   ", "type a request"),
    ("x" * (MAX_INPUT_CHARS + 1), "too long"),
])
def test_input_guardrails_stop_before_any_api_call(pets, request_text, fragment):
    client = FakeClaude([])
    result = run_plan_assistant(request_text, pets, DAY, client=client)
    assert result.status == "error" and fragment in result.message


def test_no_pets_is_rejected(pets):
    assert "at least one pet" in run("Walk", [], []).message


def test_missing_api_key_is_reported_when_no_client_is_given(pets, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = run_plan_assistant("Walk Max", pets, DAY)
    assert result.status == "error" and "ANTHROPIC_API_KEY" in result.message
