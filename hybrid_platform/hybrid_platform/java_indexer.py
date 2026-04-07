from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class JavaIndexRequest:
    repo_root: str
    output_path: str
    scip_java_cmd: str = "scip-java"
    build_tool: str = ""
    targetroot: str = ""
    cleanup: bool = True
    verbose: bool = False
    build_args: Sequence[str] = ()
    semanticdb_targetroot: str = ""


@dataclass(frozen=True)
class JavaIndexResult:
    build_tool: str
    command: list[str]
    output_path: str
    elapsed_ms: int
    used_manual_fallback: bool
    stdout: str
    stderr: str


def detect_build_tool(repo_root: str) -> str:
    root = Path(repo_root)
    if (root / "pom.xml").exists():
        return "maven"
    if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        return "gradle"
    if (root / "settings.gradle").exists() or (root / "settings.gradle.kts").exists():
        return "gradle"
    return ""


class JavaIndexer:
    def __init__(self, request: JavaIndexRequest) -> None:
        self.request = request

    def _base_command(self) -> list[str]:
        cmd = shlex.split(self.request.scip_java_cmd.strip())
        if not cmd:
            raise ValueError("scip-java 命令不能为空")
        return cmd

    def _index_command(self, build_tool: str) -> tuple[list[str], bool]:
        cmd = self._base_command()
        used_manual_fallback = bool(self.request.semanticdb_targetroot)
        if used_manual_fallback:
            cmd.extend(
                [
                    "index-semanticdb",
                    self.request.semanticdb_targetroot,
                    "--output",
                    self.request.output_path,
                ]
            )
            return cmd, True

        cmd.extend(["index", "--output", self.request.output_path])
        if build_tool:
            cmd.extend(["--build-tool", build_tool])
        if self.request.targetroot:
            cmd.extend(["--targetroot", self.request.targetroot])
        if self.request.verbose:
            cmd.append("--verbose")
        if not self.request.cleanup:
            cmd.append("--no-cleanup")
        if self.request.build_args:
            cmd.append("--")
            cmd.extend(list(self.request.build_args))
        return cmd, False

    def run(self) -> JavaIndexResult:
        build_tool = self.request.build_tool or detect_build_tool(self.request.repo_root)
        command, used_manual_fallback = self._index_command(build_tool)
        repo_root = Path(self.request.repo_root)
        repo_root.mkdir(parents=True, exist_ok=True)
        start = time.time()
        proc = subprocess.run(
            command,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        elapsed_ms = int((time.time() - start) * 1000)
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            detail = stderr or stdout or f"exit={proc.returncode}"
            raise RuntimeError(f"scip-java 执行失败: {detail}")
        output_path = Path(self.request.output_path)
        if not output_path.is_absolute():
            output_path = repo_root / output_path
        if not output_path.exists():
            raise RuntimeError(f"scip-java 未生成索引文件: {output_path}")
        return JavaIndexResult(
            build_tool=build_tool or "unknown",
            command=command,
            output_path=str(output_path),
            elapsed_ms=elapsed_ms,
            used_manual_fallback=used_manual_fallback,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
