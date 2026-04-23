#!/usr/bin/env python3
"""Align workspace manifest + index_metadata with on-disk indices under var/hybrid_indices.

Uses hybrid_platform/index_slug.py as a script (no package __init__ imports).
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HYBRID_ROOT = Path(__file__).resolve().parents[1]
PY = HYBRID_ROOT / "myenv" / "bin" / "python"
SLUG_TOOL = HYBRID_ROOT / "hybrid_platform" / "index_slug.py"
DB_DIR = HYBRID_ROOT / "var" / "hybrid_indices"
METADATA_FILE = HYBRID_ROOT / "var" / "index_metadata.json"
MANIFEST_TARGETS = REPO_ROOT / "workspace" / "manifests" / "test_java_agent_manifest_size_ge_100000.targets.json"
MANIFEST_ROUTES = REPO_ROOT / "workspace" / "manifests" / "test_java_agent_manifest_size_ge_100000.routes.json"


def _slug_lines(repo: str, commit: str) -> tuple[str, str, str]:
    r = subprocess.run(
        [str(PY), str(SLUG_TOOL), repo, commit, "--db-dir", str(DB_DIR)],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [x.strip() for x in r.stdout.strip().splitlines() if x.strip()]
    if len(lines) < 3:
        raise RuntimeError(f"index_slug unexpected output: {r.stdout!r}")
    return lines[0], lines[1], lines[2]


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def main() -> int:
    if not PY.is_file():
        print("missing venv python:", PY, file=sys.stderr)
        return 2
    if not SLUG_TOOL.is_file():
        print("missing:", SLUG_TOOL, file=sys.stderr)
        return 2

    m = _load_json(MANIFEST_TARGETS)
    skipped = set(m.get("summary", {}).get("skipped_targets", []))
    targets = m.get("targets", [])
    batch = [t for t in targets if not t.get("skip") and t.get("slug") not in skipped]
    if not batch:
        print("no non-skipped targets", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc).isoformat()
    entries: list[dict] = []
    for t in sorted(batch, key=lambda x: x["slug"]):
        repo = t["repo"]
        commit = t["base_sha"]
        slug, db_path, mcp_path = _slug_lines(repo, commit)
        if slug != t["slug"]:
            print(f"slug mismatch manifest={t['slug']} tool={slug}", file=sys.stderr)
            return 3
        p = Path(db_path)
        if not p.is_file():
            print(f"missing db for {slug}: {db_path}", file=sys.stderr)
            return 4
        cfg = (t.get("config_path") or "").strip()
        entries.append(
            {
                "slug": slug,
                "repo": repo,
                "commit": commit.lower(),
                "db_path": str(p.resolve()),
                "mcp_path": mcp_path,
                "config_path": str(Path(cfg).resolve()) if cfg else "",
                "status": "ready",
                "updated_at": now,
            }
        )

    _save_json(
        METADATA_FILE,
        {"version": 1, "entries": entries},
    )
    print(f"wrote {len(entries)} entries -> {METADATA_FILE}")

    # Refresh targets.json flags
    for t in targets:
        dbp = Path(t.get("computed_db_path", ""))
        if not str(dbp):
            continue
        lancedb = Path(str(dbp) + ".lancedb")
        if dbp.is_file():
            t["db_exists"] = True
            t["effective_db_path"] = str(dbp.resolve())
            t["index_status"] = "ready"
            if lancedb.is_dir():
                t["lancedb_path"] = str(lancedb.resolve())
        else:
            t["db_exists"] = False
            t["index_status"] = t.get("index_status") or "missing"

    m["targets"] = targets
    status_counts = Counter(t.get("index_status") for t in targets)
    m.setdefault("summary", {})["status_counts"] = dict(status_counts)

    _save_json(MANIFEST_TARGETS, m)
    print("updated", MANIFEST_TARGETS)

    # Routes
    routes_doc = _load_json(MANIFEST_ROUTES)
    for r in routes_doc.get("routes", []):
        dbp = Path(r.get("db_path", ""))
        if dbp.is_file():
            r["db_path"] = str(dbp.resolve())
            r["index_status"] = "ready"
            r["metadata_status"] = "ready"
    _save_json(MANIFEST_ROUTES, routes_doc)
    print("updated", MANIFEST_ROUTES)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
