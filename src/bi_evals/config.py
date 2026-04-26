"""Configuration schema and loading for bi-evals projects."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, model_validator


def _resolve_env_vars(raw: str) -> str:
    """Replace ${ENV_VAR} placeholders with environment variable values."""
    return re.sub(
        r"\$\{(\w+)\}",
        lambda m: os.environ.get(m.group(1), ""),
        raw,
    )


class ToolConfig(BaseModel):
    name: str
    type: str  # "file_reader" for MVP
    config: dict[str, Any] = {}


class ApiEndpointConfig(BaseModel):
    url: str = ""
    method: str = "POST"
    headers: dict[str, str] = {}
    # JSONPath-like keys to extract fields from the response JSON
    response_sql_key: str = "sql"  # where to find the SQL in the response
    response_text_key: str = "text"  # where to find the text answer
    timeout: int = 60


class AgentConfig(BaseModel):
    type: str = "anthropic_tool_loop"  # "anthropic_tool_loop" or "api_endpoint"
    model: str = ""
    # Multi-model evaluation: list of models to run the same goldens against.
    # Mutually exclusive with `model`; exactly one of the two must be set for
    # anthropic_tool_loop. After validation, `models` is always the canonical
    # list (single `model` is normalized to a one-element list).
    models: list[str] = []
    system_prompt: str = ""  # relative path to system prompt file
    tools: list[ToolConfig] = []
    max_rounds: int = 10
    api_key_env: str = "ANTHROPIC_API_KEY"
    # For api_endpoint type
    endpoint: ApiEndpointConfig = ApiEndpointConfig()

    @model_validator(mode="after")
    def _normalize_models(self) -> AgentConfig:
        if self.type != "anthropic_tool_loop":
            return self
        has_singular = bool(self.model)
        has_plural = bool(self.models)
        # If both are set but `models` is just the normalized mirror of `model`
        # (exactly one element matching), that's idempotent re-validation —
        # leave it alone.
        if has_singular and has_plural:
            if len(self.models) == 1 and self.models[0] == self.model:
                return self
            raise ValueError(
                "agent.model and agent.models are mutually exclusive; set exactly one."
            )
        if has_singular and not has_plural:
            self.models = [self.model]
        elif has_plural and not has_singular:
            self.model = self.models[0]
        return self


class DatabaseConnection(BaseModel):
    account: str = ""
    user: str = ""
    private_key_path: str = ""
    private_key_passphrase: str = ""
    warehouse: str = ""
    database: str = ""
    schema_: str = ""

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def rename_schema(cls, data: Any) -> Any:
        if isinstance(data, dict) and "schema" in data:
            data["schema_"] = data.pop("schema")
        return data


class DatabaseConfig(BaseModel):
    type: str  # "snowflake" for MVP
    connection: DatabaseConnection = DatabaseConnection()
    query_timeout: int = 30


class ScoringThresholds(BaseModel):
    completeness: float = 0.95
    precision: float = 0.95
    value_tolerance: float = 0.0001


ALL_DIMENSIONS = [
    "execution",
    "table_alignment",
    "column_alignment",
    "filter_correctness",
    "row_completeness",
    "row_precision",
    "value_accuracy",
    "no_hallucinated_columns",
    "skill_path_correctness",
]

# Default tiers: result-based correctness checks are critical, structural
# alignment checks are diagnostic (helpful to debug, not gating).
DEFAULT_CRITICAL_DIMENSIONS = [
    "execution",
    "row_completeness",
    "value_accuracy",
]

DEFAULT_DIMENSION_WEIGHTS = {
    "execution": 3.0,
    "row_completeness": 3.0,
    "value_accuracy": 3.0,
    "row_precision": 2.0,
    "column_alignment": 2.0,
    "table_alignment": 1.0,
    "filter_correctness": 1.0,
    "no_hallucinated_columns": 1.0,
    "skill_path_correctness": 1.0,
}


class ScoringConfig(BaseModel):
    dimensions: list[str] = ALL_DIMENSIONS.copy()
    thresholds: ScoringThresholds = ScoringThresholds()
    # Critical dimensions must all pass for the test to pass, regardless of score.
    critical_dimensions: list[str] = DEFAULT_CRITICAL_DIMENSIONS.copy()
    # Per-dimension weights for the overall score (defaults applied for any missing key).
    dimension_weights: dict[str, float] = DEFAULT_DIMENSION_WEIGHTS.copy()
    # Minimum weighted score (0.0–1.0) required to pass once critical dimensions pass.
    pass_threshold: float = 0.75
    # Number of trials per golden (repeat-run variance). 1 keeps legacy behavior.
    repeats: int = 1
    # Goldens whose ``last_verified_at`` is older than this trigger a warning at
    # `bi-evals run` time. 0 disables the check entirely.
    stale_after_days: int = 180


class CompareConfig(BaseModel):
    # Minimum absolute drop in pass_rate before a test is flagged as regressed.
    # 0.2 means "needs to drop by at least 20 percentage points". For single-trial
    # runs (rate ∈ {0, 1}) any flip clears 0.2, so legacy semantics are preserved.
    regression_threshold: float = 0.2


class GoldenTestsConfig(BaseModel):
    dir: str = "golden/"


class ReportingConfig(BaseModel):
    output_dir: str = "reports/"
    results_dir: str = "results/"


class StorageConfig(BaseModel):
    db_path: str = "results/bi-evals.duckdb"
    auto_ingest: bool = True
    # Cost-anomaly detection. A run is flagged when total cost exceeds
    # ``cost_alert_multiplier`` × the median of the prior ``cost_alert_window``
    # runs. 0 disables the check.
    cost_alert_multiplier: float = 2.0
    cost_alert_window: int = 10


class ProjectConfig(BaseModel):
    name: str


class BiEvalsConfig(BaseModel):
    project: ProjectConfig
    agent: AgentConfig
    database: DatabaseConfig
    golden_tests: GoldenTestsConfig = GoldenTestsConfig()
    scoring: ScoringConfig = ScoringConfig()
    reporting: ReportingConfig = ReportingConfig()
    storage: StorageConfig = StorageConfig()
    compare: CompareConfig = CompareConfig()

    # Set after loading — not part of the YAML schema
    _base_dir: Path = Path(".")

    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    def load(cls, path: Path | str = "bi-evals.yaml") -> BiEvalsConfig:
        """Load config from YAML, resolving env vars and relative paths.

        If ``<config-dir>/.env`` exists, it is loaded first (``python-dotenv``,
        ``override=False``) so ``${VAR}`` placeholders in YAML can be filled
        without manually ``source``-ing the file. Shell-exported vars win.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        env_file = path.parent / ".env"
        if env_file.is_file():
            # Do not override variables already set in the shell / process.
            load_dotenv(env_file, override=False)

        raw = path.read_text()
        resolved = _resolve_env_vars(raw)
        data = yaml.safe_load(resolved)

        config = cls(**data)
        config._base_dir = path.parent.resolve()
        return config

    def resolve_path(self, relative: str) -> Path:
        """Resolve a path relative to the config file's directory."""
        return (self._base_dir / relative).resolve()
