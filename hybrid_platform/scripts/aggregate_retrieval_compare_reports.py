#!/usr/bin/env python3
"""Aggregate multiple eval-retrieval-compare JSON reports into one markdown / JSON summary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be an object")
    return data


def _report_label(path: Path, labels: dict[str, str]) -> str:
    key = str(path.resolve())
    if key in labels:
        return labels[key]
    for pattern, lab in labels.items():
        if pattern == str(path) or pattern == key:
            return lab
    # --label name=/abs/path 映射用 resolve 后的 path 存
    return path.stem


def _extract_metrics(summary: dict[str, Any], branch: str) -> dict[str, float]:
    sub = summary.get(branch)
    if not isinstance(sub, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in sub.items():
        if isinstance(k, str) and ("@" in k) and isinstance(v, (int, float)):
            out[k] = float(v)
    return out


def aggregate(paths: list[Path], labels: dict[str, str]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    all_keys: set[str] = set()

    for path in paths:
        data = _load(path)
        summary = data.get("summary")
        if not isinstance(summary, dict):
            raise ValueError(f"{path}: missing summary object")

        label = _report_label(path, labels)
        top_ks = summary.get("top_ks")
        if not isinstance(top_ks, list):
            top_ks = []

        row: dict[str, Any] = {
            "label": label,
            "path": str(path.resolve()),
            "embedding_version": summary.get("embedding_version"),
            "evaluated_cases": summary.get("evaluated_cases"),
            "dense": _extract_metrics(summary, "dense"),
            "bm25": _extract_metrics(summary, "bm25"),
        }
        for m in (row["dense"], row["bm25"]):
            all_keys.update(m.keys())
        rows.append(row)

    metric_keys = sorted(all_keys, key=lambda x: (x.split("@")[-1], x))
    return {
        "metric_keys": metric_keys,
        "rows": rows,
    }


def markdown_table(agg: dict[str, Any]) -> str:
    rows = agg["rows"]
    metric_keys: list[str] = agg["metric_keys"]
    if not rows:
        return "(no reports)\n"

    header = ["Label", "embedding_version", "evaluated_cases"]
    header.extend(metric_keys)
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * len(header)) + " |",
    ]

    def fmt_cell(k: str, dense: dict[str, float], bm25: dict[str, float]) -> str:
        if k.startswith("recall@"):
            dr = dense.get(k)
            br = bm25.get(k)
            if dr is None and br is None:
                return ""
            parts = []
            if dr is not None:
                parts.append(f"D:{dr * 100:.2f}%")
            if br is not None:
                parts.append(f"B:{br * 100:.2f}%")
            return " ".join(parts)
        # mrr@
        dr = dense.get(k)
        br = bm25.get(k)
        if dr is None and br is None:
            return ""
        parts = []
        if dr is not None:
            parts.append(f"D:{dr:.6g}")
        if br is not None:
            parts.append(f"B:{br:.6g}")
        return " ".join(parts)

    for row in rows:
        dense = row["dense"]
        bm25 = row["bm25"]
        cells = [
            str(row.get("label", "")),
            str(row.get("embedding_version", "")),
            str(row.get("evaluated_cases", "")),
        ]
        cells.extend(fmt_cell(k, dense, bm25) for k in metric_keys)
        lines.append("| " + " | ".join(cells) + " |")

    legend = (
        "\n_D = dense, B = BM25; recall shown as %, MRR as raw._\n"
        "Per-model BM25 should match when same DB / dataset / commit and evaluated_cases match.\n"
    )
    return "\n".join(lines) + legend


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate eval-retrieval-compare JSON reports (summary.dense vs summary.bm25)."
    )
    parser.add_argument(
        "reports",
        nargs="+",
        type=Path,
        help="Paths to report JSON files produced by eval-retrieval-compare --output",
    )
    parser.add_argument(
        "--label",
        dest="labels",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Optional display label for a report path (repeatable). Example: --label voyage=/tmp/a.json",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Write aggregated machine-readable JSON to this path",
    )
    parser.add_argument(
        "--no-markdown",
        action="store_true",
        help="Do not print markdown table to stdout",
    )
    args = parser.parse_args(argv)

    label_map: dict[str, str] = {}
    for item in args.labels:
        if "=" not in item:
            parser.error(f"--label expects NAME=PATH, got {item!r}")
        name, _, path_str = item.partition("=")
        name = name.strip()
        path_str = path_str.strip()
        if not name or not path_str:
            parser.error(f"invalid --label {item!r}")
        label_map[str(Path(path_str).resolve())] = name

    paths = [p.expanduser().resolve() for p in args.reports]
    for p in paths:
        if not p.is_file():
            print(f"error: not a file: {p}", file=sys.stderr)
            return 2

    agg = aggregate(paths, label_map)

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.no_markdown:
        print(markdown_table(agg))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
