"""bi-evals: Evaluation framework for SQL-generating BI agents."""

import logging

# Library logging convention: the `bi_evals.*` tree is silent by default (a
# NullHandler), so importing bi-evals never prints unless the consumer opts in
# (`logging.basicConfig(level=logging.INFO)`, or `Runner(verbose=True)`).
# INFO = milestones; DEBUG = payloads (extracted SQL, etc.).
logging.getLogger("bi_evals").addHandler(logging.NullHandler())

from bi_evals.compare.gate import GateResult
from bi_evals.sdk import Case, RunReport, Runner, TestResult

__version__ = "0.1.0"

__all__ = ["Runner", "Case", "RunReport", "TestResult", "GateResult", "__version__"]
