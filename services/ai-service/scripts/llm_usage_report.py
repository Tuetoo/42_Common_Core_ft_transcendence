"""Read-only LLM usage and cost report for finished analyses.

Reads ai-service log lines (the run folder's <T>_ai-service.log files,
logs/ai-service/<date>.log, or `docker compose logs` output on stdin) and
sums the `anthropic.completion_succeeded` / `anthropic.stream_completed`
events whose analysis_id matches. Standard library only: it runs with the
host's python3 (3.9+) and inside the ai-service container.

Examples (host, repository root):
  python3 services/ai-service/scripts/llm_usage_report.py \\
      --group A=runs/run_20261021_090000 --group B=runs/run_20261021_100000
  python3 services/ai-service/scripts/llm_usage_report.py \\
      --log logs/ai-service/2026-10-21.log --group A=<id>,<id>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Iterable, Iterator

DESCRIPTION = (
    "Owner-run, read-only: per analysis and per group, LLM calls by model, "
    "prompt / uncached / cache-write / cache-read / output / thinking "
    "tokens, wall time and cost at the docs/MODEL_COMPARISON.md list "
    "prices.")

# USD per million tokens (input, output): docs/MODEL_COMPARISON.md.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0)
}
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25
USAGE_EVENTS = frozenset(
    {"anthropic.completion_succeeded", "anthropic.stream_completed"})
START_EVENT = "analysis_queue_job_started"
END_EVENTS = frozenset(
    {"analysis_queue_job_finished", "analysis_queue_job_errored"})
NEW_FILING_EVENT = "fresh_filing_check.found_new_content"
TOKEN_FIELDS = (
    "calls", "prompt", "uncached", "cache_write", "cache_read", "output",
    "thinking", "evidence_first")
_SUMMARY_LINE = re.compile(
    r"^(?P<ticker>\S+): analysis_id=(?P<id>[0-9a-fA-F-]{36})", re.M)


def log_records(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    for line in lines:
        start = line.find("{")
        if start < 0:
            continue
        try:
            record = json.loads(line[start:])
        except ValueError:
            continue
        if isinstance(record, dict) and "event" in record:
            yield record


def _int(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _timestamp(record: dict[str, Any]) -> datetime | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class AnalysisUsage:
    analysis_id: str
    ticker: str = ""
    models: dict[str, dict[str, int]] = field(default_factory=dict)
    started: datetime | None = None
    finished: datetime | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    new_filing: bool = False

    def add(self, record: dict[str, Any]) -> None:
        stamp = _timestamp(record)
        if stamp is not None:
            if self.first_seen is None or stamp < self.first_seen:
                self.first_seen = stamp
            if self.last_seen is None or stamp > self.last_seen:
                self.last_seen = stamp
        event = record["event"]
        if event == START_EVENT and stamp is not None:
            self.started = stamp
        elif event in END_EVENTS and stamp is not None:
            self.finished = stamp
        if event == NEW_FILING_EVENT:
            self.new_filing = True
        if not self.ticker and isinstance(record.get("ticker"), str):
            self.ticker = record["ticker"]
        if event not in USAGE_EVENTS:
            return
        totals = self.models.setdefault(
            str(record.get("model")), dict.fromkeys(TOKEN_FIELDS, 0))
        read = _int(record.get("cache_read_tokens"))
        write = _int(record.get("cache_creation_tokens"))
        prompt = _int(record.get("prompt_tokens"))
        uncached = record.get("uncached_prompt_tokens")
        totals["calls"] += 1
        totals["prompt"] += prompt
        totals["uncached"] += (
            uncached if isinstance(uncached, int) else prompt - read - write)
        totals["cache_write"] += write
        totals["cache_read"] += read
        totals["output"] += _int(record.get("completion_tokens"))
        totals["thinking"] += _int(record.get("thinking_tokens"))
        totals["evidence_first"] += 1 if record.get("evidence_first") else 0

    def wall_seconds(self) -> tuple[float | None, bool]:
        if self.started is not None and self.finished is not None:
            return (self.finished - self.started).total_seconds(), True
        if self.first_seen is not None and self.last_seen is not None:
            return (self.last_seen - self.first_seen).total_seconds(), False
        return None, False


def model_cost(
        model: str, totals: dict[str, int], *,
        sonnet_read_multiplier: float = CACHE_READ_MULTIPLIER
) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    price_in, price_out = price
    read_multiplier = (
        sonnet_read_multiplier
        if "sonnet" in model else CACHE_READ_MULTIPLIER)
    return (
        totals["uncached"] * price_in +
        totals["cache_read"] * price_in * read_multiplier +
        totals["cache_write"] * price_in * CACHE_WRITE_MULTIPLIER +
        (totals["output"] + totals["thinking"]) * price_out) / 1e6


def collect(
        analysis_ids: list[str], streams: Iterable[Iterable[str]],
        tickers: dict[str, str] | None = None) -> dict[str, AnalysisUsage]:
    usages = {
        aid: AnalysisUsage(aid, (tickers or {}).get(aid, ""))
        for aid in analysis_ids
    }
    for lines in streams:
        for record in log_records(lines):
            usage = usages.get(str(record.get("analysis_id")))
            if usage is not None:
                usage.add(record)
    return usages


def read_run_folder(folder: Path) -> list[tuple[str, str, Path]]:
    summary = (folder / "_summary.txt").read_text(errors="replace")
    return [(m.group("ticker"), m.group("id"),
             folder / f"{m.group('ticker')}_ai-service.log")
            for m in _SUMMARY_LINE.finditer(summary)]


def _open_lines(path: str) -> Iterator[str]:
    with open(path, errors="replace") as handle:
        yield from handle


def _merge(target: dict[str, int], source: dict[str, int]) -> None:
    for key in TOKEN_FIELDS:
        target[key] = target.get(key, 0) + source.get(key, 0)


def _fmt_cost(cost: float | None) -> str:
    return "n/a" if cost is None else f"${cost:.4f}"


def _model_rows(models: dict[str, dict[str, int]], indent: str,
                sonnet_read: float) -> list[str]:
    rows = []
    for model in sorted(models):
        t = models[model]
        rows.append(
            f"{indent}{model:<20} {t['calls']:>5} {t['prompt']:>10} "
            f"{t['uncached']:>10} {t['cache_write']:>9} "
            f"{t['cache_read']:>10} {t['output']:>8} {t['thinking']:>8} "
            f"{t['evidence_first']:>6} "
            f"{_fmt_cost(model_cost(model, t, sonnet_read_multiplier=sonnet_read)):>10}"  # noqa: E501
        )
    return rows


def _total_cost(models: dict[str, dict[str, int]],
                sonnet_read: float) -> float | None:
    costs = [
        model_cost(m, t, sonnet_read_multiplier=sonnet_read)
        for m, t in models.items()
    ]
    if any(c is None for c in costs):
        return None
    return sum(c for c in costs if c is not None)


def render_group(
        label: str, usages: list[AnalysisUsage], *,
        sonnet_read: float = CACHE_READ_MULTIPLIER) -> str:
    header = (
        f"    {'model':<20} {'calls':>5} {'prompt':>10} {'uncached':>10} "
        f"{'c_write':>9} {'c_read':>10} {'output':>8} {'thinking':>8} "
        f"{'ev1st':>6} {'cost':>10}")
    out = [f"== group {label}: {len(usages)} analyses", header]
    group_models: dict[str, dict[str, int]] = {}
    wall_total = 0.0
    for usage in usages:
        wall, exact = usage.wall_seconds()
        if wall is not None:
            wall_total += wall
        wall_text = (
            "wall n/a" if wall is None else
            f"wall {wall:.0f}s" + ("" if exact else " (first-last line)"))
        cost = _total_cost(usage.models, sonnet_read)
        calls = sum(t["calls"] for t in usage.models.values())
        out.append(
            f"  {usage.ticker or '?':<6} {usage.analysis_id} {wall_text}, "
            f"{calls} calls, cost {_fmt_cost(cost)}")
        if usage.new_filing:
            out.append(
                "    note: a new filing was ingested in this analysis; its "
                "filing summary and records calls are included above")
        if not usage.models:
            out.append("    (no Anthropic usage lines for this analysis)")
        out.extend(_model_rows(usage.models, "    ", sonnet_read))
        for model, totals in usage.models.items():
            _merge(group_models.setdefault(model, {}), totals)
    total = _total_cost(group_models, sonnet_read)
    count = len(usages) or 1
    out.append(
        f"  TOTAL {label}: wall {wall_total:.0f}s, cost {_fmt_cost(total)}"
        + ("" if total is None else f" (per analysis ${total / count:.4f})"))
    out.extend(_model_rows(group_models, "    ", sonnet_read))
    return "\n".join(out)


def _parse_group(spec: str) -> tuple[str, list[str]]:
    label, sep, sources = spec.partition("=")
    if not sep or not label or not sources:
        raise argparse.ArgumentTypeError(
            f"--group wants LABEL=SOURCE[,SOURCE...], got {spec!r}")
    return label, [s for s in sources.split(",") if s]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "--group", action="append", type=_parse_group, default=[],
        metavar="LABEL=SOURCE[,SOURCE...]", help=(
            "one group; a SOURCE is a runs/run_<ts> folder (ids from its "
            "_summary.txt, lines from its <T>_ai-service.log files) or an "
            "analysis id (lines from --log)"))
    parser.add_argument(
        "sources", nargs="*",
        help="run folders or analysis ids for one unlabelled group")
    parser.add_argument(
        "--log", action="append", default=[], metavar="PATH",
        help=(
            "log file to read instead of the run folders' logs "
            "(logs/ai-service/<date>.log, or - for stdin); repeatable"))
    parser.add_argument(
        "--sonnet-cache-read", type=float, default=CACHE_READ_MULTIPLIER,
        help=(
            "cache-read price multiplier for Sonnet (default 0.1 as in "
            "docs/MODEL_COMPARISON.md; Anthropic lists 0.05 for Sonnet 5.5)"))
    return parser


def _resolve(
        sources: list[str]) -> tuple[list[str], dict[str, str], list[Path]]:
    ids: list[str] = []
    tickers: dict[str, str] = {}
    logs: list[Path] = []
    for source in sources:
        path = Path(source)
        if path.is_dir():
            seen: set[str] = set()
            for ticker, aid, log in read_run_folder(path):
                if ticker in seen:
                    print(
                        f"warning: {path} lists {ticker} twice; its "
                        f"{log.name} holds only the last run, so the "
                        "earlier analysis shows no usage lines",
                        file=sys.stderr)
                seen.add(ticker)
                ids.append(aid)
                tickers[aid] = ticker
                logs.append(log)
        else:
            ids.append(source)
    return ids, tickers, logs


def main(argv: list[str] | None = None, out: IO[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stream = out or sys.stdout
    groups: list[tuple[str, list[str]]] = list(args.group)
    if args.sources:
        groups.append(("all", list(args.sources)))
    if not groups:
        build_parser().print_usage(sys.stderr)
        return 2
    stdin_lines: list[str] | None = None
    for label, sources in groups:
        ids, tickers, run_logs = _resolve(sources)
        if args.log:
            if "-" in args.log and stdin_lines is None:
                stdin_lines = list(sys.stdin)
            streams: list[Iterable[str]] = [
                (stdin_lines or []) if path == "-" else _open_lines(path)
                for path in args.log
            ]
        else:
            missing = [
                aid for aid in ids
                if aid not in tickers  # an id given without a run folder
            ]
            if missing:
                print(
                    f"group {label}: ids without a run folder need --log: "
                    f"{', '.join(missing)}", file=sys.stderr)
                return 2
            # One stream per file: a ticker listed twice in a run (or a
            # folder given twice) names the same log file again.
            streams = [
                _open_lines(str(p)) for p in dict.fromkeys(run_logs)
                if p.exists()
            ]
        usages = collect(ids, streams, tickers)
        print(
            render_group(
                label, [usages[aid] for aid in dict.fromkeys(ids)],
                sonnet_read=args.sonnet_cache_read), file=stream)
        print(file=stream)
    return 0


if __name__ == "__main__":
    sys.exit(main())
