# Codex Usage

A Codex skill that collects privacy-safe token metadata from local Codex task logs and produces daily, weekly, monthly, and all-time usage reports across one or more computers.

It exports token counters and task metadata only. Prompt text, assistant responses, tool arguments, and tool output are never included.

## Requirements

- Codex desktop task logs
- Python 3.10 or newer
- No third-party Python packages

## Install

Clone the repository into your Codex skills directory:

```bash
git clone https://github.com/codychia/codex-usage.git ~/.codex/skills/usage
```

Restart Codex if the skill is not discovered immediately.

## Use with Codex

Ask Codex:

```text
Use $usage to show my Codex token usage for today, this week, and this month.
```

The skill uses [`scripts/usage.py`](scripts/usage.py) to collect and aggregate local usage.

## Use from the command line

Collect this computer's usage bundle:

```bash
python3 scripts/usage.py collect \
  --environment "work-macbook" \
  --shared-dir "$HOME/Shared/codex-usage"
```

Generate reports from every bundle in the shared directory:

```bash
python3 scripts/usage.py report \
  --shared-dir "$HOME/Shared/codex-usage" \
  --output "$HOME/Shared/codex-usage/reports" \
  --timezone "Asia/Kuala_Lumpur"
```

Collect and report in one command:

```bash
python3 scripts/usage.py run \
  --environment "work-macbook" \
  --shared-dir "$HOME/Shared/codex-usage" \
  --timezone "Asia/Kuala_Lumpur"
```

Add `--anonymize` to hash task titles and project paths in exported bundles. See [`references/configuration.md`](references/configuration.md) for multi-computer setup, report semantics, and limitations.

## Output

- `<environment>.usage.json`: one local snapshot per computer
- `reports/report.md`: human-readable summary
- `reports/summary.json`: exact structured aggregates
- `reports/events.csv`: normalized, deduplicated usage events

`total_tokens` is the primary total. Cached input and reasoning output are subsets of other counters and must not be added again.

This is a local Codex activity estimate, not an official subscription, rate-limit, billing, or API-cost report. Deleted, unavailable, or unsynchronized logs are outside its coverage.

## License

[MIT](LICENSE)
