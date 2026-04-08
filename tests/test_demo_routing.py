"""Quick live test of the Anthropic tool loop.

Usage: uv run python test_live.py

Requires: ANTHROPIC_API_KEY environment variable set.
"""

import os
from pathlib import Path

# Load .env file if present
env_file = Path(".env")
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

from bi_evals.provider.agent_loop import run_agent_loop
from bi_evals.tools.file_reader import FileReaderTool

# Create minimal skill files in /tmp
skill_dir = Path("./tmp/test_routing")
skill_dir.mkdir(exist_ok=True)
(skill_dir / "SKILL.md").write_text(
    "# Routing\n"
    "For any question, read knowledge/INFO.md\n"
)
(skill_dir / "knowledge").mkdir(exist_ok=True)
(skill_dir / "knowledge" / "INFO.md").write_text(
    "# Info\n"
    "Table: SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS\n"
    "Columns: O_ORDERKEY, O_CUSTKEY, O_TOTALPRICE, O_ORDERDATE\n"
)

tool = FileReaderTool(tool_name="read_skill_file", base_dir=skill_dir)

result = run_agent_loop(
    question="How many orders are there?",
    system_prompt=(
        "You are a BI agent. You answer data questions by generating SQL.\n\n"
        "IMPORTANT: Always start by reading SKILL.md using the read_skill_file tool. "
        "SKILL.md contains a routing table that tells you which knowledge file to read next. "
        "Follow the routing table to read the relevant knowledge file, then generate SQL "
        "based on the schema information in that file.\n\n"
        "Present your final SQL in a ```sql code block."
    ),
    model="claude-sonnet-4-5-20250929",
    tools=[tool],
    tool_definitions=[tool.definition()],
    max_rounds=5,
    api_key=os.environ["ANTHROPIC_API_KEY"],
)

print(f"\nSQL: {result.extracted_sql}")
print(f"Files read: {result.files_read}")
print(f"Rounds: {result.rounds}")
print(f"Cost: ${result.cost:.4f}")
print(f"\nTrace:")
for step in result.trace:
    if step.type == "tool_use":
        print(f"  [{step.round}] tool: {step.tool_name}({step.tool_input})")
    else:
        print(f"  [{step.round}] text: {(step.text or '')[:100]}")
