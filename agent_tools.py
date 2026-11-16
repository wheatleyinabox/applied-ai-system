"""Tools the Plan Assistant agent can call.

The LLM never touches PawPal+ objects directly. It asks for a tool by name with
JSON arguments, and PlanToolbox runs the matching method against the real
Task/Schedule classes. Every tool validates its input and returns a plain dict,
so a bad request from the model becomes an error message it can read and fix.
"""
from __future__ import annotations

import logging
from datetime import date, time

from pawpal_system import Pet, Schedule, Task, WEEKDAY_NAMES

logger = logging.getLogger("pawpal.agent")

PRIORITIES = {"high": 3, "medium": 2, "low": 1}  # same scale as the Streamlit app
MAX_DURATION = 240  # minutes, same cap as the app's duration field


class ToolError(Exception):
    """A problem with the model's tool input that the model can read and correct."""


# JSON schemas sent to the Anthropic API so Claude knows which tools exist.
TOOL_SCHEMAS = [
    {
        "name": "add_task",
        "description": "Add one care task for one or more pets. Call once per task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short task name, e.g. 'Morning walk'."},
                "duration": {"type": "integer", "description": f"Minutes, 1-{MAX_DURATION}."},
                "priority": {"type": "string", "enum": list(PRIORITIES)},
                "pet_names": {"type": "array", "items": {"type": "string"},
                              "description": "Names of existing pets this task is for."},
                "fixed_start": {"type": "string",
                                "description": "Optional exact start time as 24h 'HH:MM'. Omit if flexible."},
                "repeat_days": {"type": "array", "items": {"type": "string", "enum": WEEKDAY_NAMES},
                                "description": "Optional weekdays it repeats on. All seven means daily."},
                "on_date": {"type": "string",
                            "description": "Optional one-time date as 'YYYY-MM-DD'. Ignored if repeat_days is set."},
            },
            "required": ["title", "duration", "priority", "pet_names"],
        },
    },
    {
        "name": "remove_task",
        "description": "Remove a previously added task by title (case-insensitive). Use to revise a plan.",
        "input_schema": {"type": "object",
                         "properties": {"title": {"type": "string"}},
                         "required": ["title"]},
    },
    {
        "name": "generate_schedule",
        "description": "Build the day's schedule from all tasks added so far. Returns placed and unplaced tasks.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_conflicts",
        "description": "After generate_schedule: list time conflicts and tasks that could not be scheduled, with reasons.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


class PlanToolbox:
    """Holds the agent's working state (pets, tasks, schedule) and exposes the tools."""

    def __init__(self, pets: list[Pet], plan_date: date,
                 start: time = time(8, 0), end: time = time(20, 0)):
        self.pets = list(pets)
        self.plan_date = plan_date
        self.start = start
        self.end = end
        self.tasks: list[Task] = []
        self.schedule: Schedule | None = None

    # ---- helpers -------------------------------------------------------
    def _find_pet(self, name: str) -> Pet:
        for pet in self.pets:
            if pet.name.lower() == name.strip().lower():
                return pet
        known = ", ".join(p.name for p in self.pets) or "none"
        raise ToolError(f"Unknown pet '{name}'. Known pets: {known}.")

    @staticmethod
    def _parse_time(text: str) -> time:
        try:
            hour, minute = text.strip().split(":")
            return time(int(hour), int(minute))
        except (ValueError, AttributeError):
            raise ToolError(f"Invalid time '{text}'. Use 24-hour 'HH:MM', e.g. '14:30'.")

    @staticmethod
    def _parse_days(days: list[str]) -> list[int]:
        lookup = {name.lower(): i for i, name in enumerate(WEEKDAY_NAMES)}
        try:
            return sorted({lookup[d.strip().lower()] for d in days})
        except (KeyError, AttributeError):
            raise ToolError(f"Invalid repeat_days {days}. Use any of {WEEKDAY_NAMES}.")

    # ---- tools ---------------------------------------------------------
    def add_task(self, title: str, duration: int, priority: str, pet_names: list[str],
                 fixed_start: str | None = None, repeat_days: list[str] | None = None,
                 on_date: str | None = None) -> dict:
        if not isinstance(title, str) or not title.strip():
            raise ToolError("Task title can't be empty.")
        if not isinstance(duration, int) or isinstance(duration, bool) or not 1 <= duration <= MAX_DURATION:
            raise ToolError(f"Duration must be a whole number of minutes from 1 to {MAX_DURATION}.")
        if priority not in PRIORITIES:
            raise ToolError(f"Priority must be one of {list(PRIORITIES)}.")
        if not pet_names:
            raise ToolError("A task needs at least one pet in pet_names.")
        pets = [self._find_pet(n) for n in pet_names]
        start = self._parse_time(fixed_start) if fixed_start else None
        if start and not (self.start <= start and (start.hour * 60 + start.minute + duration)
                          <= self.end.hour * 60 + self.end.minute):
            raise ToolError(f"Pinned task must fit in the {self.start:%H:%M}-{self.end:%H:%M} window.")
        days = self._parse_days(repeat_days) if repeat_days else []
        when = None
        if on_date:
            try:
                when = date.fromisoformat(on_date)
            except ValueError:
                raise ToolError(f"Invalid on_date '{on_date}'. Use 'YYYY-MM-DD'.")
        task = Task(title=title.strip(), duration=duration, priority=PRIORITIES[priority],
                    pets=pets, fixed_start=start, repeat_days=days, on_date=when)
        self.tasks.append(task)
        return {"ok": True, "added": task.title, "total_tasks": len(self.tasks)}

    def remove_task(self, title: str) -> dict:
        matches = [t for t in self.tasks if t.title.lower() == str(title).strip().lower()]
        if not matches:
            raise ToolError(f"No task titled '{title}'. Current tasks: {[t.title for t in self.tasks]}.")
        for t in matches:
            self.tasks.remove(t)
        return {"ok": True, "removed": len(matches), "total_tasks": len(self.tasks)}

    def generate_schedule(self) -> dict:
        if not self.tasks:
            raise ToolError("No tasks yet. Call add_task first.")
        self.schedule = Schedule(title="Plan Assistant plan", start_time=self.start,
                                 end_time=self.end, plan_date=self.plan_date)
        self.schedule.generate_schedule(self.tasks)
        return {
            "scheduled": [f"{p.start_time:%H:%M}-{p.end_time:%H:%M} {p.title}"
                          for p in self.schedule.get_tasks_by_time()],
            "unscheduled": [t.title for t in self.schedule.get_unscheduled_tasks()],
            "not_due_today": [t.title for t in self.schedule.get_not_due_tasks()],
            "minutes_left": self.schedule.get_available_time(),
        }

    def get_conflicts(self) -> dict:
        if self.schedule is None:
            raise ToolError("Call generate_schedule first.")
        return {
            "conflicts": [f"'{dropped.title}' overlaps '{kept.title}'"
                          for dropped, kept in self.schedule.get_conflicts()],
            "unscheduled": [{"title": t.title, "reason": self.schedule.get_reason(t)}
                            for t in self.schedule.get_unscheduled_tasks()],
        }

    # ---- dispatcher ----------------------------------------------------
    def run(self, name: str, args: dict | None) -> dict:
        """Run a tool by name. Never raises: failures come back as {'error': ...}."""
        tools = {"add_task": self.add_task, "remove_task": self.remove_task,
                 "generate_schedule": self.generate_schedule, "get_conflicts": self.get_conflicts}
        logger.info("tool call: %s %s", name, args)
        if name not in tools:
            result = {"error": f"Unknown tool '{name}'. Available: {list(tools)}."}
        else:
            try:
                result = tools[name](**(args or {}))
            except ToolError as exc:
                result = {"error": str(exc)}
            except TypeError as exc:  # missing or unexpected arguments from the model
                result = {"error": f"Bad arguments for {name}: {exc}"}
            except Exception as exc:  # last-resort guard so one bad call can't crash the agent
                logger.exception("tool %s crashed", name)
                result = {"error": f"Unexpected failure in {name}: {exc}"}
        logger.info("tool result: %s", result)
        return result
