#!/usr/bin/env python3
"""Collect and aggregate local Codex token usage without exporting conversations."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 1
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-.").lower()
    if not slug:
        raise ValueError("environment name must contain a letter or number")
    return slug


def hash_label(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def clean_label(value: Any, fallback: str, limit: int = 120) -> str:
    first_line = next((line.strip() for line in str(value or "").splitlines() if line.strip()), "")
    normalized = " ".join(first_line.split())
    if not normalized:
        return fallback
    return normalized if len(normalized) <= limit else normalized[: limit - 1].rstrip() + "…"


def token_values(raw: dict[str, Any]) -> dict[str, int]:
    return {field: max(0, int(raw.get(field, 0) or 0)) for field in TOKEN_FIELDS}


def load_thread_metadata(codex_home: Path) -> dict[str, dict[str, Any]]:
    candidates = [codex_home / "state_5.sqlite", codex_home / "sqlite" / "state_5.sqlite"]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            wanted = [name for name in ("id", "title", "cwd", "model", "source") if name in columns]
            if "id" not in wanted:
                continue
            rows = connection.execute(f"SELECT {', '.join(wanted)} FROM threads")
            return {str(row["id"]): dict(row) for row in rows}
        except sqlite3.Error:
            continue
        finally:
            if connection is not None:
                connection.close()
    return {}


def session_paths(codex_home: Path) -> list[Path]:
    paths: set[Path] = set()
    for root in (codex_home / "sessions", codex_home / "archived_sessions"):
        if root.is_dir():
            paths.update(path for path in root.rglob("*.jsonl") if path.is_file())
    return sorted(paths)


def read_session(path: Path) -> tuple[str | None, dict[str, Any], list[dict[str, Any]], list[str]]:
    thread_id: str | None = None
    session_started_at: datetime | None = None
    metadata: dict[str, Any] = {}
    snapshots: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    warnings.append(f"{path.name}:{line_number}: invalid JSON")
                    continue
                payload = item.get("payload") or {}
                if item.get("type") == "session_meta" and thread_id is None:
                    thread_id = str(payload.get("id") or payload.get("session_id") or "") or thread_id
                    try:
                        session_started_at = parse_timestamp(str(item.get("timestamp")))
                    except (TypeError, ValueError):
                        session_started_at = None
                    metadata.update(
                        {
                            "cwd": payload.get("cwd") or metadata.get("cwd") or "",
                            "source": payload.get("source") or metadata.get("source") or "",
                            "model_provider": payload.get("model_provider") or metadata.get("model_provider") or "",
                        }
                    )
                elif item.get("type") == "event_msg" and payload.get("type") == "token_count":
                    usage = ((payload.get("info") or {}).get("total_token_usage") or {})
                    timestamp = item.get("timestamp")
                    if timestamp and usage:
                        try:
                            occurred_at = parse_timestamp(str(timestamp))
                        except ValueError:
                            warnings.append(f"{path.name}:{line_number}: invalid timestamp")
                            continue
                        # Forked/subagent rollouts may embed their parent's complete history,
                        # including session_meta and token_count events. Only count events at
                        # or after this rollout file's own first session_meta timestamp.
                        if session_started_at is not None and occurred_at < session_started_at:
                            continue
                        normalized_time = iso_z(occurred_at)
                        snapshots.append({"timestamp": normalized_time, **token_values(usage)})
    except OSError as exc:
        warnings.append(f"{path.name}: {exc}")
    return thread_id, metadata, snapshots, warnings


def snapshots_to_events(
    thread_id: str,
    environment: str,
    metadata: dict[str, Any],
    snapshots: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for snapshot in snapshots:
        key = (snapshot["timestamp"], *(snapshot[field] for field in TOKEN_FIELDS))
        unique[key] = snapshot
    ordered = sorted(unique.values(), key=lambda item: item["timestamp"])
    previous: dict[str, int] | None = None
    events: list[dict[str, Any]] = []
    for sequence, snapshot in enumerate(ordered):
        current = {field: int(snapshot[field]) for field in TOKEN_FIELDS}
        reset = previous is None or current["total_tokens"] < previous["total_tokens"]
        if reset:
            delta = current
        else:
            delta = {field: max(0, current[field] - previous[field]) for field in TOKEN_FIELDS}
        previous = current
        if delta["total_tokens"] <= 0:
            continue
        identity = "|".join(
            [thread_id, snapshot["timestamp"], str(sequence), *(str(current[field]) for field in TOKEN_FIELDS)]
        )
        event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        events.append(
            {
                "event_id": event_id,
                "environment": environment,
                "thread_id": thread_id,
                "timestamp": snapshot["timestamp"],
                "title": clean_label(metadata.get("title"), f"Task {thread_id[:8]}"),
                "cwd": metadata.get("cwd") or "",
                "model": metadata.get("model") or metadata.get("model_provider") or "unknown",
                "source": metadata.get("source") or "",
                **delta,
            }
        )
    return events


def collect(codex_home: Path, environment: str, anonymize: bool) -> dict[str, Any]:
    environment = environment.strip()
    paths = session_paths(codex_home)
    database_metadata = load_thread_metadata(codex_home)
    by_thread: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    files_with_tokens = 0

    for path in paths:
        thread_id, file_metadata, snapshots, file_warnings = read_session(path)
        warnings.extend(file_warnings)
        if not thread_id or not snapshots:
            continue
        files_with_tokens += 1
        entry = by_thread.setdefault(thread_id, {"metadata": {}, "snapshots": []})
        entry["metadata"].update(file_metadata)
        entry["metadata"].update({k: v for k, v in database_metadata.get(thread_id, {}).items() if v})
        entry["snapshots"].extend(snapshots)

    events: list[dict[str, Any]] = []
    for thread_id, entry in sorted(by_thread.items()):
        events.extend(
            snapshots_to_events(thread_id, environment, entry["metadata"], entry["snapshots"])
        )

    if anonymize:
        for event in events:
            event["title"] = hash_label("task", event["thread_id"])
            event["cwd"] = hash_label("project", event["cwd"]) if event["cwd"] else ""

    return {
        "schema_version": SCHEMA_VERSION,
        "environment": environment,
        "collected_at": iso_z(utc_now()),
        "codex_home": hash_label("codex-home", str(codex_home)) if anonymize else str(codex_home),
        "anonymized": anonymize,
        "coverage": {
            "files_scanned": len(paths),
            "files_with_token_events": files_with_tokens,
            "threads": len(by_thread),
            "events": len(events),
            "warnings": warnings[:100],
            "warning_count": len(warnings),
        },
        "events": sorted(events, key=lambda item: (item["timestamp"], item["event_id"])),
    }


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def load_bundles(shared_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    bundles: list[dict[str, Any]] = []
    events_by_id: dict[str, dict[str, Any]] = {}
    for path in sorted(shared_dir.glob("*.usage.json")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                bundle = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read bundle {path}: {exc}") from exc
        if bundle.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema version in {path}")
        bundles.append({key: value for key, value in bundle.items() if key != "events"})
        for event in bundle.get("events", []):
            events_by_id.setdefault(str(event["event_id"]), event)
    events = sorted(events_by_id.values(), key=lambda item: (item["timestamp"], item["event_id"]))
    return bundles, events


def zero_tokens() -> dict[str, int]:
    return {field: 0 for field in TOKEN_FIELDS}


def sum_tokens(events: Iterable[dict[str, Any]]) -> dict[str, int]:
    result = zero_tokens()
    for event in events:
        for field in TOKEN_FIELDS:
            result[field] += int(event.get(field, 0) or 0)
    return result


def percent_change(current: int, previous: int) -> float | None:
    if previous == 0:
        return None if current == 0 else 100.0
    return round((current - previous) * 100.0 / previous, 1)


def start_of_day(value: datetime) -> datetime:
    return datetime.combine(value.date(), time.min, tzinfo=value.tzinfo)


def next_month(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1, day=1)
    return value.replace(month=value.month + 1, day=1)


def previous_month(value: datetime) -> datetime:
    if value.month == 1:
        return value.replace(year=value.year - 1, month=12, day=1)
    return value.replace(month=value.month - 1, day=1)


def in_range(event: dict[str, Any], start: datetime | None, end: datetime | None, tz: ZoneInfo) -> bool:
    occurred = parse_timestamp(str(event["timestamp"])).astimezone(tz)
    return (start is None or occurred >= start) and (end is None or occurred < end)


def group_totals(events: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        label = str(event.get(key) or "unknown")
        grouped[label].append(event)
    rows = []
    for label, items in grouped.items():
        tokens = sum_tokens(items)
        rows.append({key: label, "chats": len({item["thread_id"] for item in items}), **tokens})
    return sorted(rows, key=lambda row: (-row["total_tokens"], str(row[key]).lower()))


def chat_totals(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[(str(event["environment"]), str(event["thread_id"]))].append(event)
    rows = []
    for (environment, thread_id), items in grouped.items():
        latest = max(items, key=lambda item: item["timestamp"])
        rows.append(
            {
                "environment": environment,
                "thread_id": thread_id,
                "title": clean_label(latest.get("title"), f"Task {thread_id[:8]}"),
                "project": latest.get("cwd") or "",
                "model": latest.get("model") or "unknown",
                "last_activity": latest["timestamp"],
                **sum_tokens(items),
            }
        )
    return sorted(rows, key=lambda row: (-row["total_tokens"], row["title"].lower()))


def format_number(value: int | float) -> str:
    return f"{value:,.0f}"


def format_change(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.1f}%"


def cache_rate(tokens: dict[str, Any]) -> float:
    inputs = int(tokens.get("input_tokens", 0) or 0)
    return round(int(tokens.get("cached_input_tokens", 0) or 0) * 100.0 / inputs, 1) if inputs else 0.0


def build_summary(
    bundles: list[dict[str, Any]], events: list[dict[str, Any]], tz: ZoneInfo, top: int, trend_days: int
) -> dict[str, Any]:
    now = datetime.now(tz)
    today = start_of_day(now)
    tomorrow = today + timedelta(days=1)
    week = today - timedelta(days=today.weekday())
    next_week = week + timedelta(days=7)
    month = today.replace(day=1)
    next_month_start = next_month(month)
    previous_day = today - timedelta(days=1)
    previous_week = week - timedelta(days=7)
    previous_month_start = previous_month(month)

    ranges = {
        "today": (today, tomorrow),
        "this_week": (week, next_week),
        "this_month": (month, next_month_start),
        "all_time": (None, None),
    }
    previous_ranges = {
        "today": (previous_day, today),
        "this_week": (previous_week, week),
        "this_month": (previous_month_start, month),
    }

    periods: dict[str, Any] = {}
    for name, (start, end) in ranges.items():
        selected = [event for event in events if in_range(event, start, end, tz)]
        totals = sum_tokens(selected)
        chats = chat_totals(selected)
        period: dict[str, Any] = {
            "start": start.isoformat() if start else None,
            "end_exclusive": end.isoformat() if end else None,
            "partial": name != "all_time" and end > now,
            "chats": len(chats),
            "events": len(selected),
            "cache_rate_percent": cache_rate(totals),
            "totals": totals,
            "by_environment": group_totals(selected, "environment"),
            "by_model": group_totals(selected, "model"),
            "top_chats": chats[:top],
        }
        if name in previous_ranges:
            previous_start, previous_end = previous_ranges[name]
            previous_events = [
                event for event in events if in_range(event, previous_start, previous_end, tz)
            ]
            previous_tokens = sum_tokens(previous_events)
            period["previous_period"] = {
                "start": previous_start.isoformat(),
                "end_exclusive": previous_end.isoformat(),
                "totals": previous_tokens,
                "total_change_percent": percent_change(
                    totals["total_tokens"], previous_tokens["total_tokens"]
                ),
            }
        periods[name] = period

    all_chat_rows = chat_totals(events)
    all_values = [row["total_tokens"] for row in all_chat_rows]
    daily: list[dict[str, Any]] = []
    for offset in range(trend_days - 1, -1, -1):
        day_start = today - timedelta(days=offset)
        day_end = day_start + timedelta(days=1)
        day_events = [event for event in events if in_range(event, day_start, day_end, tz)]
        daily.append(
            {
                "date": day_start.date().isoformat(),
                "chats": len({event["thread_id"] for event in day_events}),
                **sum_tokens(day_events),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_z(utc_now()),
        "timezone": str(tz),
        "coverage": {
            "environments": len({bundle["environment"] for bundle in bundles}),
            "bundles": [
                {
                    "environment": bundle["environment"],
                    "collected_at": bundle["collected_at"],
                    "anonymized": bundle.get("anonymized", False),
                    **bundle.get("coverage", {}),
                }
                for bundle in bundles
            ],
            "deduplicated_events": len(events),
            "first_event": events[0]["timestamp"] if events else None,
            "last_event": events[-1]["timestamp"] if events else None,
        },
        "statistics": {
            "total_chats": len(all_chat_rows),
            "average_tokens_per_chat": round(sum(all_values) / len(all_values)) if all_values else 0,
            "median_tokens_per_chat": round(median(all_values)) if all_values else 0,
        },
        "periods": periods,
        "daily_trend": daily,
    }


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    lines.extend("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def render_markdown(summary: dict[str, Any], top: int) -> str:
    coverage = summary["coverage"]
    statistics = summary["statistics"]
    lines = [
        "# Codex token usage",
        "",
        f"Generated: {summary['generated_at']}  ",
        f"Reporting timezone: {summary['timezone']}  ",
        f"Coverage: {coverage['environments']} environment(s), {statistics['total_chats']} chat(s), "
        f"{coverage['deduplicated_events']} usage event(s)",
        "",
        "## Overview",
        "",
    ]
    overview_rows: list[list[str]] = []
    for name, label in (("today", "Today"), ("this_week", "This week"), ("this_month", "This month"), ("all_time", "All time")):
        period = summary["periods"][name]
        totals = period["totals"]
        previous = period.get("previous_period")
        change = format_change(previous.get("total_change_percent")) if previous else "—"
        overview_rows.append(
            [
                label + (" (partial)" if period["partial"] else ""),
                format_number(totals["total_tokens"]),
                format_number(totals["input_tokens"]),
                format_number(totals["output_tokens"]),
                f"{period['cache_rate_percent']:.1f}%",
                str(period["chats"]),
                change,
            ]
        )
    lines.extend(
        [
            markdown_table(
                ["Period", "Total", "Input", "Output", "Cache rate", "Chats", "vs previous"],
                overview_rows,
            ),
            "",
            "Current periods are partial. Changes compare them with the previous complete calendar period.",
            "",
            f"Average per chat: **{format_number(statistics['average_tokens_per_chat'])}** · "
            f"Median per chat: **{format_number(statistics['median_tokens_per_chat'])}**",
        ]
    )

    for name, label in (("today", "Today"), ("this_week", "This week"), ("this_month", "This month"), ("all_time", "All time")):
        period = summary["periods"][name]
        lines.extend(["", f"## {label}: top chats", ""])
        rows = [
            [
                row["title"],
                row["environment"],
                format_number(row["total_tokens"]),
                format_number(row["input_tokens"]),
                format_number(row["output_tokens"]),
                row["model"],
            ]
            for row in period["top_chats"][:top]
        ]
        lines.append(markdown_table(["Chat", "Environment", "Total", "Input", "Output", "Model"], rows) if rows else "No usage recorded.")

    lines.extend(["", "## All-time usage by environment", ""])
    environment_rows = [
        [
            row["environment"],
            format_number(row["total_tokens"]),
            format_number(row["input_tokens"]),
            format_number(row["cached_input_tokens"]),
            format_number(row["output_tokens"]),
            str(row["chats"]),
        ]
        for row in summary["periods"]["all_time"]["by_environment"]
    ]
    lines.append(markdown_table(["Environment", "Total", "Input", "Cached", "Output", "Chats"], environment_rows) if environment_rows else "No bundles contained usage.")

    lines.extend(["", "## Recent daily trend", ""])
    trend_rows = [
        [row["date"], format_number(row["total_tokens"]), format_number(row["input_tokens"]), format_number(row["output_tokens"]), str(row["chats"])]
        for row in summary["daily_trend"]
    ]
    lines.append(markdown_table(["Date", "Total", "Input", "Output", "Chats"], trend_rows))

    lines.extend(["", "## Bundle freshness", ""])
    freshness_rows = [
        [bundle["environment"], bundle["collected_at"], str(bundle.get("threads", 0)), str(bundle.get("events", 0)), str(bundle.get("warning_count", 0))]
        for bundle in coverage["bundles"]
    ]
    lines.append(markdown_table(["Environment", "Collected", "Threads", "Events", "Warnings"], freshness_rows) if freshness_rows else "No bundles found.")
    lines.extend(
        [
            "",
            "> Local token activity is not an official billing, subscription-credit, or rate-limit report. "
            "Deleted or unavailable logs are outside coverage.",
            "",
        ]
    )
    return "\n".join(lines)


def write_events_csv(path: Path, events: list[dict[str, Any]], tz: ZoneInfo) -> None:
    fields = [
        "event_id", "timestamp_utc", "timestamp_local", "environment", "thread_id", "title", "cwd", "model", "source", *TOKEN_FIELDS
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for event in events:
            row = dict(event)
            row["timestamp_utc"] = event["timestamp"]
            row["timestamp_local"] = parse_timestamp(event["timestamp"]).astimezone(tz).isoformat()
            writer.writerow(row)


def write_report(shared_dir: Path, output: Path, timezone_name: str, top: int, trend_days: int) -> dict[str, Any]:
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {timezone_name}") from exc
    bundles, events = load_bundles(shared_dir)
    if not bundles:
        raise ValueError(f"no *.usage.json bundles found in {shared_dir}")
    summary = build_summary(bundles, events, tz, top, trend_days)
    output.mkdir(parents=True, exist_ok=True)
    atomic_json_write(output / "summary.json", summary)
    (output / "report.md").write_text(render_markdown(summary, top), encoding="utf-8")
    write_events_csv(output / "events.csv", events, tz)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_collection_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--environment", required=True, help="Unique computer/environment name")
        command.add_argument(
            "--codex-home",
            type=Path,
            default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
            help="Codex data directory (default: CODEX_HOME or ~/.codex)",
        )
        command.add_argument("--shared-dir", type=Path, required=True, help="Directory for usage bundles")
        command.add_argument("--anonymize", action="store_true", help="Hash titles and project paths")

    def add_report_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--timezone", default="Asia/Kuala_Lumpur", help="IANA reporting timezone")
        command.add_argument("--output", type=Path, help="Report directory (default: SHARED_DIR/reports)")
        command.add_argument("--top", type=int, default=10, help="Top chats per period")
        command.add_argument("--trend-days", type=int, default=30, help="Number of daily trend rows")

    collect_parser = subparsers.add_parser("collect", help="Create this computer's usage bundle")
    add_collection_arguments(collect_parser)

    report_parser = subparsers.add_parser("report", help="Aggregate all usage bundles")
    report_parser.add_argument("--shared-dir", type=Path, required=True, help="Directory containing bundles")
    add_report_arguments(report_parser)

    run_parser = subparsers.add_parser("run", help="Collect this computer and aggregate all bundles")
    add_collection_arguments(run_parser)
    add_report_arguments(run_parser)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if hasattr(args, "top") and args.top < 1:
        raise ValueError("--top must be at least 1")
    if hasattr(args, "trend_days") and not 1 <= args.trend_days <= 3660:
        raise ValueError("--trend-days must be between 1 and 3660")
    if hasattr(args, "codex_home") and not args.codex_home.expanduser().is_dir():
        raise ValueError(f"Codex home does not exist: {args.codex_home}")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        validate_args(args)
        if args.command in {"collect", "run"}:
            shared_dir = args.shared_dir.expanduser().resolve()
            bundle = collect(args.codex_home.expanduser().resolve(), args.environment, args.anonymize)
            bundle_path = shared_dir / f"{slugify(args.environment)}.usage.json"
            atomic_json_write(bundle_path, bundle)
            print(
                f"Collected {bundle['coverage']['events']} events from {bundle['coverage']['threads']} "
                f"tasks into {bundle_path}"
            )
        if args.command in {"report", "run"}:
            shared_dir = args.shared_dir.expanduser().resolve()
            output = (args.output or (shared_dir / "reports")).expanduser().resolve()
            summary = write_report(shared_dir, output, args.timezone, args.top, args.trend_days)
            print(
                f"Reported {summary['coverage']['environments']} environments and "
                f"{summary['statistics']['total_chats']} chats into {output}"
            )
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"usage: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
