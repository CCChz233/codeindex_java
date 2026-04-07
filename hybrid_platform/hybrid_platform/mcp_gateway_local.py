"""本机多 MCP 后端 + 单端口 Nginx：读 index_metadata.json，起子进程与 nginx。"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

_SS_PID_RE = re.compile(r"pid=(\d+)")

from .index_metadata import (
    IndexMetadataFile,
    cli_assign_backend_ports,
    default_metadata_path,
    load_metadata,
    render_nginx_gateway_conf,
)


def _diagnose_no_backends(meta: IndexMetadataFile, meta_path: Path) -> str:
    lines = [
        f"metadata_file={meta_path} exists={meta_path.is_file()}",
        f"entries_in_file={len(meta.entries)}",
    ]
    if not meta.entries:
        lines.append(
            "hint: after index build, register with:\n"
            "  cd hybrid_platform && ./myenv/bin/python -m hybrid_platform.index_metadata upsert \\\n"
            "    --repo '<org/repo>' --commit '<40hex>' --config ./config/default_config.json \\\n"
            "    --output-dir ./var/hybrid_indices\n"
            "Or run: ./scripts/repo_commit_to_index.sh ... (it calls upsert at the end)."
        )
        return "\n".join(lines)
    for e in meta.entries:
        bits: list[str] = []
        if e.status != "ready":
            bits.append(f"status={e.status!r} (need 'ready')")
        p = Path(e.db_path)
        if not p.is_file():
            bits.append(f"db not found: {p}")
        lines.append(f"  [{e.slug}] " + ("; ".join(bits) if bits else "would be used"))
    return "\n".join(lines)


def _hybrid_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_runtime_dir() -> Path:
    return _hybrid_root() / "var" / "mcp_gateway" / "runtime"


def _default_config_fallback() -> Path:
    return _hybrid_root() / "config" / "default_config.json"


def _stop_gateway(runtime_dir: Path) -> None:
    pids_file = runtime_dir / "backend_pids.txt"
    if pids_file.is_file():
        for line in pids_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.isdigit():
                continue
            pid = int(line)
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        pids_file.unlink(missing_ok=True)
    nginx_pid_file = runtime_dir / "logs" / "nginx.pid"
    if nginx_pid_file.is_file():
        try:
            subprocess.run(
                ["nginx", "-s", "quit", "-p", str(runtime_dir)],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        try:
            pid = int(nginx_pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError, OSError):
            pass
        time.sleep(0.5)


def _arg_path(s: str, default: Path) -> Path:
    t = (s or "").strip()
    return Path(t).resolve() if t else default


def _pids_listening_tcp(port: int) -> list[int]:
    """Linux：查找在 TCP port 上 LISTEN 的进程（ss 优先，其次 lsof）。"""
    pids: set[int] = set()
    port_colon = re.compile(rf":{int(port)}(?:\s|$)")
    sport_expr = f"sport = :{int(port)}"
    for ss_args in (
        ["ss", "-H", "-ltnp", sport_expr],
        ["ss", "-ltnp", sport_expr],
    ):
        try:
            r = subprocess.run(ss_args, capture_output=True, text=True, timeout=5)
        except FileNotFoundError:
            break
        except subprocess.TimeoutExpired:
            continue
        if r.returncode != 0:
            continue
        for line in r.stdout.splitlines():
            if not port_colon.search(line):
                continue
            for m in _SS_PID_RE.finditer(line):
                pids.add(int(m.group(1)))
        if pids:
            return sorted(pids)
    try:
        r = subprocess.run(
            ["ss", "-ltnp"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if "LISTEN" not in line or not port_colon.search(line):
                    continue
                for m in _SS_PID_RE.finditer(line):
                    pids.add(int(m.group(1)))
    except FileNotFoundError:
        pass
    if not pids:
        try:
            r = subprocess.run(
                ["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                for x in r.stdout.split():
                    if x.isdigit():
                        pids.add(int(x))
        except FileNotFoundError:
            pass
    return sorted(pids)


def _free_tcp_listen_port(port: int) -> None:
    """对占用该端口的 LISTEN 进程发 SIGTERM，避免 nginx bind 失败。"""
    pids = _pids_listening_tcp(port)
    if not pids:
        return
    print(f"freeing listen port {port}: SIGTERM pids={pids}", file=sys.stderr)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(1.0)


def cmd_start(args: argparse.Namespace) -> None:
    root = _hybrid_root()
    runtime = _arg_path(getattr(args, "runtime_dir", "") or "", _default_runtime_dir())
    meta_path = _arg_path(getattr(args, "metadata_file", "") or "", default_metadata_path())
    listen = int(args.listen)
    base_port = int(args.backend_base)

    if not shutil.which("nginx"):
        print("ERROR: nginx not found in PATH (install nginx)", file=sys.stderr)
        sys.exit(1)

    meta = load_metadata(meta_path)
    pairs = cli_assign_backend_ports(meta.entries, backend_base_port=base_port)
    if not pairs:
        print("ERROR: no ready entries with existing .db (see below)", file=sys.stderr)
        print(_diagnose_no_backends(meta, meta_path), file=sys.stderr)
        sys.exit(1)

    if getattr(args, "kill_existing", True):
        _stop_gateway(runtime)

    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "logs").mkdir(parents=True, exist_ok=True)

    conf = render_nginx_gateway_conf(
        meta.entries,
        listen_port=listen,
        backend_base_port=base_port,
    )
    (runtime / "nginx.conf").write_text(conf, encoding="utf-8")

    py = sys.executable
    backend_pids: list[int] = []
    logs_dir = runtime / "logs"
    for entry, port in pairs:
        cfg = entry.config_path or str(_default_config_fallback())
        if not Path(cfg).is_file():
            print(f"ERROR: config not found for {entry.slug}: {cfg}", file=sys.stderr)
            sys.exit(1)
        env = os.environ.copy()
        env["HYBRID_DB"] = entry.db_path
        env["HYBRID_CONFIG"] = cfg
        env["HYBRID_MCP_HOST"] = "127.0.0.1"
        env["HYBRID_MCP_PORT"] = str(port)
        env["HYBRID_MCP_PATH"] = entry.mcp_path
        log_path = logs_dir / f"mcp_{entry.slug}.log"
        log_f = open(log_path, "a", encoding="utf-8")
        proc = subprocess.Popen(
            [py, "-m", "hybrid_platform.cli", "mcp-streamable", "--db", entry.db_path, "--mcp-path", entry.mcp_path, "--config", cfg],
            cwd=str(root),
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_f.close()
        backend_pids.append(proc.pid)
        print(f"started backend slug={entry.slug} pid={proc.pid} port={port} log={log_path}", file=sys.stderr)

    (runtime / "backend_pids.txt").write_text("\n".join(str(p) for p in backend_pids) + "\n", encoding="utf-8")

    time.sleep(0.3)
    if getattr(args, "free_listen_port", True):
        _free_tcp_listen_port(listen)
    r = subprocess.run(
        ["nginx", "-p", str(runtime), "-c", "nginx.conf"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        print(r.stderr or r.stdout, file=sys.stderr)
        print("ERROR: nginx failed to start; killing backends", file=sys.stderr)
        for pid in backend_pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        sys.exit(1)

    print(f"nginx listening on 0.0.0.0:{listen} (prefix /mcp/<slug> per entry)", file=sys.stderr)
    print(f"runtime_dir={runtime}", file=sys.stderr)


def cmd_stop(args: argparse.Namespace) -> None:
    runtime = _arg_path(getattr(args, "runtime_dir", "") or "", _default_runtime_dir())
    _stop_gateway(runtime)
    print("stopped backends + nginx (if were running)", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="local Nginx + multi-MCP from index_metadata.json")
    p.add_argument("--metadata-file", default="", help="path to index_metadata.json")
    p.add_argument("--runtime-dir", default="", help="nginx prefix + logs + pid files")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="先停掉本 runtime-dir 内旧进程（默认），再起 MCP 后端 + nginx")
    s.add_argument("--listen", type=int, default=8765)
    s.add_argument("--backend-base", type=int, default=28065)
    s.add_argument(
        "--no-stop-first",
        action="store_true",
        help="不先停旧网关（若端口/进程冲突会失败）",
    )
    s.add_argument(
        "--no-free-listen-port",
        action="store_true",
        help="不在起 nginx 前清理监听端口上的其它进程（默认会 SIGTERM 占用 --listen 端口的进程）",
    )
    s.set_defaults(func=cmd_start)

    sub.add_parser("stop", help="stop backends and nginx").set_defaults(func=cmd_stop)

    args = p.parse_args(argv)
    if args.cmd == "start":
        args.kill_existing = not getattr(args, "no_stop_first", False)
        args.free_listen_port = not getattr(args, "no_free_listen_port", False)
    args.func(args)


if __name__ == "__main__":
    main()
