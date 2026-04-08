"""Golden test Pydantic models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class SkillStep(BaseModel):
    tool: str
    input_contains: str


class ExpectedSkillPath(BaseModel):
    required_skills: list[SkillStep] = []
    sequence_matters: bool = True
    allow_extra_skills: bool = True


class ValueCheck(BaseModel):
    column: str
    condition: str  # "type", "equals", "contains"
    value: Any = None


class RowComparison(BaseModel):
    enabled: bool = False
    completeness_threshold: float = 0.95
    precision_threshold: float = 0.95
    value_tolerance: float = 0.0001
    key_columns: list[str] = []
    value_columns: list[str] = []
    ignore_order: bool = True


class ExpectedResults(BaseModel):
    min_rows: int = 0
    required_columns: list[str] = []
    checks: list[ValueCheck] = []
    row_comparison: RowComparison = RowComparison()


class GoldenTest(BaseModel):
    id: str
    category: str = ""
    difficulty: str = ""
    question: str
    expected_skill_path: ExpectedSkillPath = ExpectedSkillPath()
    reference_sql: str = ""
    expected: ExpectedResults = ExpectedResults()
    tags: list[str] = []
    notes: str = ""
