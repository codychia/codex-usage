# Configuration and data contract

## Multi-computer setup

1. Copy the `usage` skill to `~/.codex/skills/usage` on every computer.
2. Choose one synchronized directory visible to all computers, such as an iCloud Drive, Dropbox, Google Drive, or network-share folder.
3. Give every computer a stable, unique name such as `office-mac`, `home-mac`, or `windows-laptop`.
4. Run `collect` on each computer. Each environment rewrites only its own `<environment>.usage.json` bundle.
5. Run `report` from any computer after synchronization completes.

For environments that cannot share a directory, copy their `*.usage.json` bundle into one aggregation directory manually.

## Files

The shared directory contains:

- `<environment>.usage.json`: one full usage snapshot per computer.
- `reports/report.md`: human-readable report.
- `reports/summary.json`: structured aggregates.
- `reports/events.csv`: normalized, deduplicated event-level data.

Bundle fields include schema version, environment, collection timestamp, source Codex home, anonymization flag, scanned-file counts, parse warnings, task metadata, and timestamped token deltas. Bundles never contain conversation bodies.

## Token fields

- `total_tokens`: primary total recorded by Codex.
- `input_tokens`: all input tokens, including cached input.
- `cached_input_tokens`: subset of input tokens served from cache.
- `output_tokens`: all output tokens.
- `reasoning_output_tokens`: subset of output tokens used for reasoning.

Useful derived values:

- Uncached input: `input_tokens - cached_input_tokens`.
- Cache rate: `cached_input_tokens / input_tokens`.
- Chat share: chat total divided by period total.

Do not sum all five token fields; that double-counts cached and reasoning tokens.

## Period definitions

- Day: midnight to midnight in the selected timezone.
- Week: Monday through Sunday in the selected timezone.
- Month: calendar month in the selected timezone.
- All time: all token events still available in collected local logs.

The current day, week, and month are partial periods. Comparison percentages in the report compare the current partial period with the previous complete calendar period and are labeled accordingly.

## Coverage and limitations

- Deleted or rotated-away task logs cannot be reconstructed.
- A currently running task may change while collection is reading it; a later collection refreshes the snapshot.
- A computer that has not refreshed its bundle appears with an older `collected_at` value.
- If the same task log is synchronized to multiple computers, stable event IDs prevent it from inflating the overall total. The event is attributed to the first environment in deterministic bundle order.
- Local token usage does not necessarily equal API billing usage, subscription credits, or product rate-limit accounting.
- Titles and paths can be sensitive metadata. Use `--anonymize` or secure the shared directory.

## Common commands

Collect from a custom Codex home:

```bash
python3 scripts/usage.py collect --environment office-mac --codex-home /path/to/.codex --shared-dir /path/to/shared
```

Create an anonymized bundle:

```bash
python3 scripts/usage.py collect --environment office-mac --shared-dir /path/to/shared --anonymize
```

Show the top 20 chats and a 60-day trend:

```bash
python3 scripts/usage.py report --shared-dir /path/to/shared --top 20 --trend-days 60
```
