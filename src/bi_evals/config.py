"""Configuration schema and loading for bi-evals projects."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, model_validator


_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")


def _resolve_env_vars(raw: str) -> str:
    """Replace ``${ENV_VAR}`` placeholders with environment variable values.

    Raises ``ValueError`` listing every placeholder that can't be resolved
    (env var is unset, distinct from set-but-empty). Failing at config-load
    time produces an obviously-actionable error rather than silently
    substituting empty strings that propagate into downstream connectors
    (Snowflake, Anthropic) and surface as cryptic errors several layers
    deep.

    A var that is *set but empty* is intentionally allowed — some optional
    fields (e.g. ``private_key_passphrase``) are legitimately blank.
    """
    missing: list[str] = []

    def _replace(m: re.Match[str]) -> str:
        name = m.group(1)
        value = os.environ.get(name)
        if value is None:
            missing.append(name)
            return ""
        return value

    resolved = _ENV_VAR_RE.sub(_replace, raw)
    if missing:
        unique = sorted(set(missing))
        raise ValueError(
            "Unresolved environment variables in config: "
            f"{unique}. Set them in your shell or in a .env file alongside "
            "the config; if the field isn't used, remove it from the YAML."
        )
    return resolved


class _DuplicateKeyError(ValueError):
    """Raised when a YAML mapping contains the same key twice.

    PyYAML's default ``SafeLoader`` silently lets the second value win, which
    once let ``tmp/my-evals/bi-evals.yaml`` ship with two ``scoring:`` blocks
    where the second silently dropped the entire scoring config on the floor.
    """


class _StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys."""


def _construct_mapping_strict(
    loader: _StrictSafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    if not isinstance(node, yaml.MappingNode):
        raise yaml.constructor.ConstructorError(
            None,
            None,
            f"expected a mapping node, but found {node.id}",
            node.start_mark,
        )
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise _DuplicateKeyError(
                f"duplicate key {key!r} in YAML mapping at line "
                f"{key_node.start_mark.line + 1}, column "
                f"{key_node.start_mark.column + 1}. "
                "PyYAML normally lets the second value silently win — strict "
                "loading rejects this so a stray duplicate (e.g. two "
                "``scoring:`` blocks) can't quietly drop config on the floor."
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_strict,
)


def _safe_load_strict(text: str) -> Any:
    """``yaml.safe_load`` that rejects duplicate mapping keys."""
    return yaml.load(text, Loader=_StrictSafeLoader)  # noqa: S506 — strict subclass


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


class PushConfig(BaseModel):
    """Config for the push adapter (``adapter: push``).

    The customer runs their own agent over the goldens and submits a JSONL file
    of ``{golden_file, generated_sql, trace}`` rows; the push adapter replays
    each row instead of calling a live agent. ``input_file`` is normally set by
    ``bi-evals score --input`` rather than written into ``bi-evals.yaml`` by hand.
    """

    input_file: str = ""


# Fields that, at the top level of ``agent:``, mark the *old* flat (two-mode)
# schema. The schema is adapter-nested now (``agent.adapter`` + a block named for
# the adapter), so any of these at the top level means an un-migrated config.
_LEGACY_FLAT_AGENT_KEYS = frozenset(
    {
        "type",
        "model",
        "models",
        "system_prompt",
        "tools",
        "max_rounds",
        "api_key_env",
        "endpoint",
    }
)


class AnthropicToolLoopConfig(BaseModel):
    """Config for the dev-only driving adapter (``adapter: anthropic_tool_loop``).

    Not a public product feature — this drives a local Claude loop, useful for
    authoring goldens before a real agent exists. All the model/prompt/tool
    fields that used to live flat under ``agent:`` now nest here.
    """

    model: str = ""
    # Multi-model evaluation: list of models to run the same goldens against.
    # Mutually exclusive with `model`; exactly one of the two must be set.
    # After validation, `models` is always the canonical list (single `model` is
    # normalized to a one-element list).
    models: list[str] = []
    system_prompt: str = ""  # relative path to system prompt file
    tools: list[ToolConfig] = []
    max_rounds: int = 10
    api_key_env: str = "ANTHROPIC_API_KEY"

    @model_validator(mode="after")
    def _normalize_models(self) -> AnthropicToolLoopConfig:
        has_singular = bool(self.model)
        has_plural = bool(self.models)
        # Idempotent re-validation: `models` is just the normalized mirror of
        # `model` (exactly one matching element) — leave it alone.
        if has_singular and has_plural:
            if len(self.models) == 1 and self.models[0] == self.model:
                return self
            raise ValueError(
                "anthropic_tool_loop.model and .models are mutually exclusive; "
                "set exactly one."
            )
        if has_singular and not has_plural:
            self.models = [self.model]
        elif has_plural and not has_singular:
            self.model = self.models[0]
        return self


class AgentConfig(BaseModel):
    """Which adapter produces the agent's answer, plus that adapter's config.

    Adapter-nested schema (one contract, many adapters):

        agent:
          adapter: api_endpoint
          api_endpoint: { url: ... }

        agent:
          adapter: anthropic_tool_loop      # dev-only
          anthropic_tool_loop: { model: ..., system_prompt: ..., tools: [...] }

    The old flat shape (``type:`` + ``model:``/``endpoint:`` as top-level peers)
    is rejected with a migration error — see ``_reject_legacy_flat_schema``.

    The ``type``/``model``/``endpoint``/... properties below delegate into the
    nested blocks so readers can stay adapter-agnostic.
    """

    # api_endpoint = default on-ramp; anthropic_tool_loop = dev-only driving adapter.
    # Typed as a Literal so a typo'd adapter fails at config-load with a clear
    # pydantic error, rather than only blowing up later at dispatch time.
    adapter: Literal["api_endpoint", "anthropic_tool_loop", "push"] = "api_endpoint"
    api_endpoint: ApiEndpointConfig = ApiEndpointConfig()
    anthropic_tool_loop: AnthropicToolLoopConfig = AnthropicToolLoopConfig()
    push: PushConfig = PushConfig()

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_flat_schema(cls, data: Any) -> Any:
        if isinstance(data, dict):
            stray = _LEGACY_FLAT_AGENT_KEYS & set(data)
            if stray:
                raise ValueError(
                    f"agent: uses the old flat schema (found {sorted(stray)} at the "
                    "top level). The schema is now adapter-nested: set `agent.adapter` "
                    "and move adapter config under a block named for it, e.g.\n"
                    "  agent:\n"
                    "    adapter: api_endpoint\n"
                    "    api_endpoint: { url: ... }\n"
                    "See docs/migration-adapter-schema.md."
                )
        return data

    @model_validator(mode="after")
    def _require_model_for_driving_adapter(self) -> AgentConfig:
        # The dev-only driving adapter can't run without a model and has no
        # runtime-env escape hatch (unlike api_endpoint.url, which legitimately
        # resolves from ${BI_AGENT_URL} and may be deferred), so catch the empty
        # case at load time rather than as a runtime error in produce().
        if self.adapter == "anthropic_tool_loop":
            if (
                not self.anthropic_tool_loop.model
                and not self.anthropic_tool_loop.models
            ):
                raise ValueError(
                    "adapter 'anthropic_tool_loop' requires anthropic_tool_loop.model "
                    "or anthropic_tool_loop.models to be set."
                )
        return self

    # ── Back-compat accessors so readers stay adapter-agnostic ──────────────
    @property
    def type(self) -> str:
        """Alias for ``adapter`` (kept so existing readers/tests don't churn)."""
        return self.adapter

    @property
    def endpoint(self) -> ApiEndpointConfig:
        return self.api_endpoint

    @property
    def model(self) -> str:
        return self.anthropic_tool_loop.model

    @property
    def models(self) -> list[str]:
        return self.anthropic_tool_loop.models

    @property
    def system_prompt(self) -> str:
        return self.anthropic_tool_loop.system_prompt

    @property
    def tools(self) -> list[ToolConfig]:
        return self.anthropic_tool_loop.tools

    @property
    def max_rounds(self) -> int:
        return self.anthropic_tool_loop.max_rounds

    @property
    def api_key_env(self) -> str:
        return self.anthropic_tool_loop.api_key_env


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
    "anti_pattern_compliance",
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
    # Phase 6c — non-critical by default; teams who want it gating can add it
    # to ``critical_dimensions``. Weight 2.0 makes a violation meaningful in
    # the weighted score without forcing a hard fail.
    "anti_pattern_compliance": 2.0,
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
    # Phase 6d: knowledge files (skill / knowledge / system_prompt) whose mtime
    # is older than this trigger a warning at `bi-evals run` time. Only files
    # that were actually read in the previous run are considered, to avoid
    # warnings about unread files. 0 disables the check entirely.
    knowledge_stale_after_days: int = 90


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
        data = _safe_load_strict(resolved)

        config = cls(**data)
        config._base_dir = path.parent.resolve()
        return config

    def resolve_path(self, relative: str) -> Path:
        """Resolve a path relative to the config file's directory."""
        return (self._base_dir / relative).resolve()
