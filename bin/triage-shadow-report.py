#!/usr/bin/env python3
"""Summarise the ``triage.shadow`` lines of the Zammad autoreply.

The autoreply logs one ``triage.shadow {json}`` line per triaged customer
article when the decision-model shadow is on (see
``docker/autoreply/triage_shadow.py``). This script reads those lines and
prints agreement per field and, for disagreements, the decision model's
probability distribution. The lines hold ids, labels and probabilities
only, so the output contains no ticket content either.

Sources (pick one):
  bin/triage-shadow-report.py                       # docker logs of zammad-zammad-autoreply-1
  bin/triage-shadow-report.py --since 168h          # same, limited window
  bin/triage-shadow-report.py --file saved.log      # a saved log file
  logcli query '{container="zammad-zammad-autoreply-1"} |= "triage.shadow"' --raw | bin/triage-shadow-report.py --stdin

Options: --vs final  compares against the applied decision (after the policy
rules) instead of the LLM's own choice; --first-only keeps only the first
customer article of each ticket; --json prints the summary as JSON.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from collections import Counter
from typing import Any, Iterable

MARKER = "triage.shadow "
FIELDS = ("disposition", "category", "priority")
DEFAULT_CONTAINER = "zammad-zammad-autoreply-1"


def iter_lines(args: argparse.Namespace) -> Iterable[str]:
    if args.stdin:
        yield from sys.stdin
        return
    if args.file:
        with open(args.file, "r", encoding="utf-8", errors="replace") as handle:
            yield from handle
        return
    cmd = ["docker", "logs", args.container]
    if args.since:
        cmd += ["--since", args.since]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.exit(f"docker logs failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}")
    # The service logs to stderr; docker splits the streams.
    yield from proc.stdout.splitlines()
    yield from proc.stderr.splitlines()


def parse(lines: Iterable[str]) -> list[dict[str, Any]]:
    records = []
    for line in lines:
        idx = line.find(MARKER)
        if idx < 0:
            continue
        raw = line[idx + len(MARKER):].strip()
        if not raw.startswith("{"):
            continue
        try:
            records.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return records


def _fmt_probs(entry: dict[str, Any]) -> str:
    if "p_high" in entry:
        return f"p_high={entry['p_high']:.2f}"
    probs = entry.get("p") or {}
    top = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)[:3]
    return " ".join(f"{k}={v:.2f}" for k, v in top)


def summarise(records: list[dict[str, Any]], *, vs: str, first_only: bool) -> dict[str, Any]:
    statuses = Counter(r.get("status") for r in records)
    errors = Counter(r.get("error") for r in records if r.get("status") == "error")
    ok = [r for r in records if r.get("status") == "ok"]
    if first_only:
        ok = [r for r in ok if r.get("first_article")]
    if vs == "llm":
        # Without a usable LLM decision there is nothing to compare against.
        ok = [r for r in ok if r.get("llm_ok")]
    agree_key = "agree" if vs == "llm" else "agree_final"

    per_field: dict[str, Any] = {}
    for field in FIELDS:
        compared = [r for r in ok if (r.get(agree_key) or {}).get(field) is not None]
        agreed = sum(1 for r in compared if r[agree_key][field])
        confusion = Counter(
            (r[vs][field], r["dm"][field]["choice"]) for r in compared if not r[agree_key][field]
        )
        examples = [
            {
                "ticket_id": r.get("ticket_id"),
                "article_id": r.get("article_id"),
                vs: r[vs][field],
                "dm": r["dm"][field]["choice"],
                "dm_probs": _fmt_probs(r["dm"][field]),
            }
            for r in compared
            if not r[agree_key][field]
        ]
        per_field[field] = {
            "compared": len(compared),
            "agreed": agreed,
            "agreement": round(agreed / len(compared), 3) if compared else None,
            "confusion": [{vs: a, "dm": b, "n": n} for (a, b), n in confusion.most_common()],
            "disagreements": examples,
        }

    latencies = [r["latency_ms"] for r in ok if isinstance(r.get("latency_ms"), int)]
    latency = {}
    if latencies:
        latencies.sort()
        latency = {
            "p50_ms": int(statistics.median(latencies)),
            "p95_ms": latencies[min(len(latencies) - 1, int(round(0.95 * (len(latencies) - 1))))],
            "max_ms": latencies[-1],
        }
    return {
        "lines": len(records),
        "status": dict(statuses),
        "errors": dict(errors),
        "compared_against": vs,
        "first_article_only": first_only,
        "records_compared": len(ok),
        "all_three_agree": sum(1 for r in ok if all((r.get(agree_key) or {}).get(f) for f in FIELDS)),
        "fields": per_field,
        "latency": latency,
    }


def print_text(summary: dict[str, Any], *, max_examples: int) -> None:
    vs = summary["compared_against"]
    print(f"triage.shadow: {summary['lines']} lines, status {summary['status']}")
    if summary["errors"]:
        print(f"  errors: {summary['errors']}")
    print(
        f"compared against: {vs}{' (first customer article only)' if summary['first_article_only'] else ''}; "
        f"{summary['records_compared']} records, all three fields agree in {summary['all_three_agree']}"
    )
    if summary["latency"]:
        lat = summary["latency"]
        print(f"latency: p50 {lat['p50_ms']} ms, p95 {lat['p95_ms']} ms, max {lat['max_ms']} ms")
    for field, data in summary["fields"].items():
        pct = f"{data['agreement'] * 100:.1f}%" if data["agreement"] is not None else "n/a"
        print(f"\n{field}: {data['agreed']}/{data['compared']} agree ({pct})")
        for row in data["confusion"]:
            print(f"  {vs}={row[vs]:<16} dm={row['dm']:<16} n={row['n']}")
        for ex in data["disagreements"][:max_examples]:
            print(
                f"    ticket {ex['ticket_id']} article {ex['article_id']}: {vs}={ex[vs]} dm={ex['dm']} [{ex['dm_probs']}]"
            )
        if len(data["disagreements"]) > max_examples:
            print(f"    ... {len(data['disagreements']) - max_examples} more (use --max-examples)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--stdin", action="store_true", help="read log lines from stdin (e.g. logcli --raw)")
    source.add_argument("--file", help="read log lines from a file")
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--since", help="docker logs --since (e.g. 24h, 2026-10-06)")
    parser.add_argument("--vs", choices=("llm", "final"), default="llm")
    parser.add_argument("--first-only", action="store_true")
    parser.add_argument("--max-examples", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    summary = summarise(parse(iter_lines(args)), vs=args.vs, first_only=args.first_only)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print_text(summary, max_examples=args.max_examples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
