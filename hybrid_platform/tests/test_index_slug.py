from pathlib import Path

import pytest

from hybrid_platform.index_slug import (
    index_db_path,
    mcp_http_path,
    normalize_commit_sha,
    repo_commit_slug,
    sanitize_repo_name,
)


def test_sanitize_repo_name() -> None:
    assert sanitize_repo_name("a/b/c") == "a_b_c"
    assert sanitize_repo_name("../x") == "x"
    assert sanitize_repo_name("") == "repo"


def test_normalize_commit_sha() -> None:
    assert normalize_commit_sha("ABCDEF0" * 5 + "ABCDE") == "abcdef0" * 5 + "abcde"
    with pytest.raises(ValueError):
        normalize_commit_sha("GGGGGGG")
    with pytest.raises(ValueError):
        normalize_commit_sha("abc")


def test_repo_commit_slug() -> None:
    assert repo_commit_slug("my/repo", "a" * 40) == f"my_repo_{'a' * 40}"


def test_index_db_path_and_mcp_path() -> None:
    p = index_db_path("org/tool", "b" * 40, "/tmp/idx")
    assert p == Path("/tmp/idx") / f"org_tool_{'b' * 40}.db"
    assert mcp_http_path("org/tool", "b" * 40) == f"/mcp/org_tool_{'b' * 40}"
