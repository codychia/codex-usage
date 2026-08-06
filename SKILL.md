---
name: usage
description: Analyze Codex token usage from one or more desktop computers. Use when the user asks about token usage, total tokens, input or output tokens, cached tokens, reasoning tokens, usage by computer or environment, highest-usage chats, daily/weekly/monthly usage, usage trends, or wants to collect and merge local Codex usage reports across machines.
---

# Usage

Use the bundled `scripts/usage.py` command to collect privacy-safe token metadata from local Codex task logs and aggregate bundles from multiple computers.

## Choose the operation

- For a first-time setup, configure every computer with the same shared directory and a unique environment name.
- To refresh one computer's bundle, run `collect` on that computer.
- To build reports from all collected bundles, run `report` on any computer that can see the shared directory.
- To collect the current computer and immediately rebuild reports, run `run`.
- To answer a usage question, refresh the local bundle when appropriate, run `report`, and summarize the relevant generated values. State the report's collection time and coverage.

## Commands

Set `SKILL_DIR` to this skill directory before using these examples.

```bash
python3 "$SKILL_DIR/scripts/usage.py" collect \
  --environment "work-macbook" \
  --shared-dir "$HOME/Shared/codex-usage"
```

```bash
python3 "$SKILL_DIR/scripts/usage.py" report \
  --shared-dir "$HOME/Shared/codex-usage" \
  --output "$HOME/Shared/codex-usage/reports" \
  --timezone "Asia/Kuala_Lumpur"
```

```bash
python3 "$SKILL_DIR/scripts/usage.py" run \
  --environment "work-macbook" \
  --shared-dir "$HOME/Shared/codex-usage" \
  --timezone "Asia/Kuala_Lumpur"
```

The default Codex home is `$CODEX_HOME`, falling back to `~/.codex`. Pass `--codex-home` when logs live elsewhere. Pass `--anonymize` to hash chat titles and project paths in the exported bundle.

## Interpret the metrics correctly

- Treat `total_tokens` as the primary total. Do not add cached-input or reasoning tokens to it: both are subsets of other counters.
- Calculate usage at event timestamps. The script derives positive deltas from cumulative task counters so a chat spanning several days is allocated to the days when usage occurred.
- Treat output as a local Codex activity estimate, not an official subscription, rate-limit, billing, or API-cost statement.
- Mention that deleted, unavailable, or not-yet-synchronized logs are outside report coverage.
- Use the environment breakdown for computer attribution. Overall totals are deduplicated by stable usage-event IDs.
- Prefer `report.md` for a human summary, `summary.json` for exact structured values, and `events.csv` for custom analysis.

## Protect privacy

The collector reads only session metadata and `token_count` events. It never exports prompts, assistant responses, tool arguments, or tool outputs. By default, it exports task titles and project paths so reports remain recognizable. Use `--anonymize` when the shared directory is not private.

Read [references/configuration.md](references/configuration.md) when setting up multiple computers, troubleshooting coverage, or interpreting the bundle schema.
