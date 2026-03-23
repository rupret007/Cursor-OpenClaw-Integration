"""Types for deterministic Andrea experience assurance runs."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


def new_experience_run_id() -> str:
    return f"exp_{uuid.uuid4().hex[:16]}"


def new_experience_check_id() -> str:
    return f"expchk_{uuid.uuid4().hex[:16]}"


def _clip_text(value: Any, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


@dataclass
class ExperienceObservation:
    description: str
    expected: str
    observed: Any
    passed: bool
    issue_code: str = ""
    severity: str = "medium"
    observation_id: str = field(default_factory=new_experience_check_id)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "description": self.description,
            "expected": self.expected,
            "observed": self.observed,
            "passed": bool(self.passed),
            "issue_code": str(self.issue_code or "").strip(),
            "severity": str(self.severity or "medium").strip(),
        }


ExperienceScenarioRunner = Callable[[Any, "ExperienceScenario"], "ExperienceCheckResult"]


@dataclass
class ExperienceScenario:
    scenario_id: str
    title: str
    description: str
    category: str
    runner: ExperienceScenarioRunner = field(repr=False, compare=False)
    tags: List[str] = field(default_factory=list)
    suspected_files: List[str] = field(default_factory=list)
    required: bool = True
    weight: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "tags": list(self.tags),
            "suspected_files": list(self.suspected_files),
            "required": bool(self.required),
            "weight": float(self.weight),
            "metadata": dict(self.metadata),
        }


@dataclass
class ExperienceCheckResult:
    check_id: str
    scenario_id: str
    title: str
    category: str
    passed: bool
    score: int
    summary: str
    output_excerpt: str
    observations: List[ExperienceObservation] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    suspected_files: List[str] = field(default_factory=list)
    required: bool = True
    weight: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    completed_at: float = field(default_factory=time.time)

    @classmethod
    def from_observations(
        cls,
        scenario: ExperienceScenario,
        observations: List[ExperienceObservation],
        *,
        output_excerpt: Any = "",
        metadata: Dict[str, Any] | None = None,
        started_at: float | None = None,
        completed_at: float | None = None,
    ) -> "ExperienceCheckResult":
        total = max(1, len(observations))
        passed_count = sum(1 for row in observations if row.passed)
        score = int(round(100.0 * (float(passed_count) / float(total))))
        failed = [row for row in observations if not row.passed]
        summary = (
            "All expectations passed."
            if not failed
            else "; ".join(
                _clip_text(
                    f"{row.description}: expected {row.expected}, observed {row.observed}",
                    220,
                )
                for row in failed[:3]
            )
        )
        excerpt = _clip_text(
            output_excerpt
            or "\n".join(
                f"- {row.description}: expected {row.expected}, observed {row.observed}"
                for row in failed[:5]
            )
            or "All experience expectations passed.",
            1200,
        )
        extra = dict(metadata or {})
        extra.setdefault("issue_codes", [row.issue_code for row in failed if row.issue_code])
        extra.setdefault("observation_count", len(observations))
        return cls(
            check_id=f"experience_{scenario.scenario_id}",
            scenario_id=scenario.scenario_id,
            title=scenario.title,
            category=scenario.category,
            passed=not failed,
            score=score,
            summary=summary,
            output_excerpt=excerpt,
            observations=list(observations),
            tags=list(scenario.tags),
            suspected_files=list(scenario.suspected_files),
            required=bool(scenario.required),
            weight=float(scenario.weight),
            metadata=extra,
            started_at=float(started_at or time.time()),
            completed_at=float(completed_at or time.time()),
        )

    @property
    def issue_codes(self) -> List[str]:
        codes: List[str] = []
        for row in self.observations:
            code = str(row.issue_code or "").strip()
            if code and code not in codes:
                codes.append(code)
        return codes

    def as_verification_check(self) -> Dict[str, Any]:
        return {
            "check_id": self.check_id,
            "label": f"Experience: {self.title}",
            "command": f"experience_assurance::{self.scenario_id}",
            "passed": bool(self.passed),
            "required": bool(self.required),
            "score": int(self.score),
            "output_excerpt": self.output_excerpt,
            "summary": self.summary,
            "scenario_id": self.scenario_id,
            "category": self.category,
            "tags": list(self.tags),
            "suspected_files": list(self.suspected_files),
            "issue_codes": list(self.issue_codes),
            "observations": [row.as_dict() for row in self.observations],
            "metadata": dict(self.metadata),
            "started_at": float(self.started_at),
            "completed_at": float(self.completed_at),
            "duration_ms": max(0, int(round((self.completed_at - self.started_at) * 1000.0))),
        }

    def as_dict(self) -> Dict[str, Any]:
        payload = self.as_verification_check()
        payload.update(
            {
                "title": self.title,
                "weight": float(self.weight),
            }
        )
        return payload


@dataclass
class ExperienceRun:
    run_id: str
    actor: str
    status: str
    checks: List[ExperienceCheckResult]
    summary: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    completed_at: float = field(default_factory=time.time)

    @property
    def total_checks(self) -> int:
        return len(self.checks)

    @property
    def passed_checks(self) -> int:
        return sum(1 for row in self.checks if row.passed)

    @property
    def failed_checks(self) -> int:
        return sum(1 for row in self.checks if not row.passed)

    @property
    def passed(self) -> bool:
        return self.failed_checks == 0

    @property
    def average_score(self) -> float:
        if not self.checks:
            return 0.0
        total = sum(float(row.score) for row in self.checks)
        return round(total / float(len(self.checks)), 2)

    @property
    def score_counts(self) -> Dict[str, int]:
        counts = {"excellent": 0, "warn": 0, "failed": 0}
        for row in self.checks:
            if row.score >= 90:
                counts["excellent"] += 1
            elif row.score >= 70:
                counts["warn"] += 1
            else:
                counts["failed"] += 1
        return counts

    @property
    def category_counts(self) -> List[Dict[str, Any]]:
        buckets: Dict[str, Dict[str, Any]] = {}
        for row in self.checks:
            if row.passed:
                continue
            bucket = buckets.setdefault(
                row.category,
                {"category": row.category, "count": 0, "issue_codes": []},
            )
            bucket["count"] += 1
            for code in row.issue_codes:
                if code not in bucket["issue_codes"]:
                    bucket["issue_codes"].append(code)
        return sorted(
            buckets.values(),
            key=lambda item: (-int(item.get("count") or 0), str(item.get("category") or "")),
        )

    def as_verification_report(self) -> Dict[str, Any]:
        summary = self.summary or (
            f"{self.passed_checks}/{self.total_checks} experience scenarios passed"
        )
        return {
            "passed": bool(self.passed),
            "summary": summary,
            "checks": [row.as_verification_check() for row in self.checks],
            "metadata": {
                "run_id": self.run_id,
                "actor": self.actor,
                "status": self.status,
                "average_score": self.average_score,
                "failed_checks": self.failed_checks,
                "score_counts": self.score_counts,
                "category_counts": self.category_counts,
                **dict(self.metadata),
            },
        }

    def as_dict(self) -> Dict[str, Any]:
        summary = self.summary or (
            f"{self.passed_checks}/{self.total_checks} experience scenarios passed"
        )
        failed = [row for row in self.checks if not row.passed]
        return {
            "run_id": self.run_id,
            "actor": self.actor,
            "status": self.status,
            "passed": bool(self.passed),
            "summary": summary,
            "total_checks": self.total_checks,
            "passed_checks": self.passed_checks,
            "failed_checks": self.failed_checks,
            "average_score": self.average_score,
            "score_counts": self.score_counts,
            "category_counts": self.category_counts,
            "checks": [row.as_dict() for row in self.checks],
            "failed_scenarios": [
                {
                    "scenario_id": row.scenario_id,
                    "title": row.title,
                    "category": row.category,
                    "score": row.score,
                    "summary": row.summary,
                    "issue_codes": row.issue_codes,
                    "suspected_files": list(row.suspected_files),
                }
                for row in failed
            ],
            "metadata": dict(self.metadata),
            "started_at": float(self.started_at),
            "completed_at": float(self.completed_at),
        }
