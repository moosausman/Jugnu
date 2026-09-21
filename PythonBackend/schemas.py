"""
schemas.py -- typed, validated shapes for everything a model produces that
the system then acts on: plans, tasks, verification verdicts, reflections,
replans. Validated with pydantic (already a dependency of the ollama
client) before anything is executed; a model response that doesn't fit is
sent back with the exact validation errors for correction.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

RiskLevel = Literal["read", "low", "medium", "high"]
Tier = Literal["fast", "standard", "deep"]
RequestType = Literal["conversation", "quick_action", "agentic", "scheduled", "background"]

MAX_TASKS = 12


class TaskSpec(BaseModel):
    id: int = Field(ge=1, le=999)
    agent: str = Field(min_length=1, max_length=40)
    title: str = Field(default="", max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    depends_on: list[int] = Field(default_factory=list)
    risk: RiskLevel = "low"
    completion_criteria: str = Field(default="", max_length=600)
    verification: str = Field(default="", max_length=600)
    # None = the assigned agent's default tier (writer/developer default deep).
    model_tier: Optional[Tier] = None
    optional: bool = False
    max_attempts: int = Field(default=2, ge=1, le=3)

    @field_validator("depends_on", mode="before")
    @classmethod
    def _deps(cls, v):
        if v is None:
            return []
        if isinstance(v, (int, str)):
            v = [v]
        return [int(x) for x in v]

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v):
        return (v or "")[:120]


class ScheduleSpec(BaseModel):
    """When a scheduled request should run. `at` is ISO-8601 local time;
    `every_minutes` makes it recurring; `in_minutes` is relative."""
    at: Optional[str] = None
    in_minutes: Optional[float] = Field(default=None, ge=0, le=60 * 24 * 365)
    every_minutes: Optional[float] = Field(default=None, ge=1, le=60 * 24 * 366)
    until: Optional[str] = None
    # "notify" = a timer/alarm (just tell the user); "task" = do work when due.
    kind: Literal["task", "notify"] = "task"
    instruction: str = Field(min_length=1, max_length=2000)


class PlanSpec(BaseModel):
    objective: str = Field(min_length=1, max_length=600)
    request_type: RequestType = "agentic"
    complexity: Literal["simple", "standard", "complex"] = "standard"
    assumptions: list[str] = Field(default_factory=list, max_length=12)
    success_criteria: list[str] = Field(default_factory=list, max_length=12)
    clarify: Optional[str] = Field(default=None, max_length=600)
    session: Optional[Literal["agent", "primary"]] = None
    schedule: Optional[ScheduleSpec] = None
    tasks: list[TaskSpec] = Field(default_factory=list, max_length=MAX_TASKS)

    @field_validator("session", mode="before")
    @classmethod
    def _session(cls, v):
        return v or None

    @field_validator("assumptions", "success_criteria", mode="before")
    @classmethod
    def _strings(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return [str(x)[:300] for x in v][:12]

    @model_validator(mode="after")
    def _graph(self):
        if self.clarify:
            return self
        if self.request_type == "scheduled" and self.schedule is None:
            raise ValueError('request_type "scheduled" requires a "schedule" object')
        if not self.tasks and self.request_type != "scheduled":
            raise ValueError("a plan needs at least one task (or a clarify question)")
        ids = [t.id for t in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        known = set(ids)
        for t in self.tasks:
            for d in t.depends_on:
                if d not in known:
                    raise ValueError(f"task {t.id} depends on unknown task {d}")
                if d == t.id:
                    raise ValueError(f"task {t.id} depends on itself")
        # Cycle check (Kahn).
        indegree = {t.id: len(t.depends_on) for t in self.tasks}
        children: dict[int, list[int]] = {t.id: [] for t in self.tasks}
        for t in self.tasks:
            for d in t.depends_on:
                children[d].append(t.id)
        queue = [i for i, n in indegree.items() if n == 0]
        seen = 0
        while queue:
            node = queue.pop()
            seen += 1
            for c in children[node]:
                indegree[c] -= 1
                if indegree[c] == 0:
                    queue.append(c)
        if seen != len(self.tasks):
            raise ValueError("task dependencies contain a cycle")
        return self


class Verdict(BaseModel):
    passed: bool
    confidence: float = Field(default=0.7, ge=0, le=1)
    reason: str = Field(default="", max_length=800)
    missing: list[str] = Field(default_factory=list, max_length=10)
    recovery: Literal["none", "retry", "replan", "ask_user", "accept_partial", "stop"] = "none"
    question: Optional[str] = Field(default=None, max_length=500)

    @field_validator("missing", mode="before")
    @classmethod
    def _missing(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return [str(x)[:300] for x in v][:10]


class ReplanSpec(BaseModel):
    """Revised remaining work after a failure or a wrong assumption."""
    reason: str = Field(default="", max_length=600)
    assumptions: list[str] = Field(default_factory=list, max_length=12)
    abandon: bool = False                 # nothing sensible left to try
    tasks: list[TaskSpec] = Field(default_factory=list, max_length=MAX_TASKS)

    @field_validator("assumptions", mode="before")
    @classmethod
    def _strings(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return [str(x)[:300] for x in v]


class MemoryCandidate(BaseModel):
    content: str = Field(min_length=3, max_length=600)
    kind: Literal["user", "episodic", "semantic", "error", "strategy"]
    key: Optional[str] = Field(default=None, max_length=80)
    confidence: float = Field(default=0.7, ge=0, le=1)
    source: Literal["user_stated", "observed", "tool_result", "reflection", "inferred"] = "reflection"
    scope: str = Field(default="global", max_length=200)
    importance: float = Field(default=0.5, ge=0, le=1)


class SkillCandidate(BaseModel):
    name: str = Field(min_length=3, max_length=80)
    purpose: str = Field(min_length=3, max_length=400)
    trigger_phrases: list[str] = Field(default_factory=list, max_length=8)
    inputs: list[str] = Field(default_factory=list, max_length=10)
    preconditions: list[str] = Field(default_factory=list, max_length=10)
    expected_output: str = Field(default="", max_length=400)
    verification: str = Field(default="", max_length=400)


class Reflection(BaseModel):
    objective: str = Field(default="", max_length=600)
    succeeded: bool
    outcome_summary: str = Field(default="", max_length=1500)
    what_worked: list[str] = Field(default_factory=list, max_length=10)
    what_failed: list[str] = Field(default_factory=list, max_length=10)
    user_corrections: list[str] = Field(default_factory=list, max_length=10)
    lessons: list[str] = Field(default_factory=list, max_length=10)
    memories: list[MemoryCandidate] = Field(default_factory=list, max_length=12)
    skill: Optional[SkillCandidate] = None
    planning_advice: str = Field(default="", max_length=600)
    code_change_suggestion: Optional[str] = Field(default=None, max_length=1500)

    @field_validator("what_worked", "what_failed", "user_corrections", "lessons", mode="before")
    @classmethod
    def _strings(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return [str(x)[:400] for x in v][:10]


def validation_message(err: ValidationError) -> str:
    """Compact, model-readable description of what was wrong."""
    parts = []
    for e in err.errors()[:8]:
        loc = ".".join(str(x) for x in e.get("loc", ()))
        parts.append(f"{loc or 'plan'}: {e.get('msg')}")
    return "; ".join(parts)
