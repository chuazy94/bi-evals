# Migration: flat `agent:` → adapter-nested schema

The `agent:` block in `bi-evals.yaml` changed shape. The old flat schema (a
top-level `type:` with `model:`/`endpoint:`/`tools:`/… as sibling keys) is **no
longer accepted** — loading such a config now fails fast with a migration hint
rather than silently mis-parsing. This is a deliberate clean break that came in
with the "one contract, many adapters" refactor.

## What changed

- `agent.type` → `agent.adapter`.
- Each adapter's config nests under a block **named for that adapter**, instead
  of being flattened as top-level peers of `type`.
- The driving adapter (`anthropic_tool_loop`) is now **dev-only** — its fields
  (`model`, `models`, `system_prompt`, `tools`, `max_rounds`, `api_key_env`)
  move into the nested `anthropic_tool_loop:` block.

## api_endpoint (the default on-ramp)

Before:
```yaml
agent:
  type: "api_endpoint"
  endpoint:
    url: "${BI_AGENT_URL}"
    headers:
      Authorization: "Bearer ${BI_AGENT_TOKEN}"
```

After:
```yaml
agent:
  adapter: "api_endpoint"
  api_endpoint:
    url: "${BI_AGENT_URL}"
    headers:
      Authorization: "Bearer ${BI_AGENT_TOKEN}"
```

(The `endpoint:` key is renamed to `api_endpoint:` to match the adapter name.)

## anthropic_tool_loop (dev-only)

Before:
```yaml
agent:
  type: "anthropic_tool_loop"
  model: "claude-sonnet-4-6"
  system_prompt: "system-prompt.md"
  tools:
    - name: read_skill_file
      type: file_reader
      config:
        base_dir: "skills/"
  max_rounds: 10
```

After:
```yaml
agent:
  adapter: "anthropic_tool_loop"
  anthropic_tool_loop:
    model: "claude-sonnet-4-6"
    system_prompt: "system-prompt.md"
    tools:
      - name: read_skill_file
        type: file_reader
        config:
          base_dir: "skills/"
    max_rounds: 10
```

Multi-model (`models: [...]`) moves under `anthropic_tool_loop:` the same way.

## How to migrate

1. Rename `type:` → `adapter:`.
2. Move every adapter field under a block named for the adapter
   (`api_endpoint:` or `anthropic_tool_loop:`), indented one level deeper.
3. Re-run `bi-evals doctor` to confirm the config loads and validates.

If you still see `agent: uses the old flat schema (found [...])`, a stray
top-level key remains — move it into the nested block.

## Re-scaffolding from scratch

`bi-evals init api_endpoint` (default on-ramp) and `bi-evals init dev` (dev-only
driving adapter) emit the new shape directly. The old `init built-in` / `init
byo` subcommands were renamed to `init dev` / `init api_endpoint`.
