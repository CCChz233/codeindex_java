"""测试代码路径 / 查询意图的轻量启发式，用于检索后对测试结果降权。"""

from __future__ import annotations

import re
# 查询中出现这些模式时，认为用户在找「测试」相关内容，不对测试结果做降权。
_TEST_QUERY_REGEXES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bjunit\b", re.I),
    re.compile(r"\btestng\b", re.I),
    re.compile(r"\bmockito\b", re.I),
    re.compile(r"\bhamcrest\b", re.I),
    re.compile(r"\bassertj\b", re.I),
    re.compile(r"\beasymock\b", re.I),
    re.compile(r"\bpowermock\b", re.I),
    re.compile(r"\bspock\b", re.I),
    re.compile(r"\bunit\s+tests?\b", re.I),
    re.compile(r"\btest\s+cases?\b", re.I),
    re.compile(r"\btest\s+suites?\b", re.I),
    re.compile(r"\bintegration\s+tests?\b", re.I),
    re.compile(r"\b@before\b", re.I),
    re.compile(r"\b@after\b", re.I),
    re.compile(r"\b@beforeeach\b", re.I),
    re.compile(r"\b@aftereach\b", re.I),
    re.compile(r"\b@pytest\b", re.I),
    re.compile(r"单元测试"),
    re.compile(r"单测"),
    re.compile(r"测试用例"),
    re.compile(r"测试类"),
    re.compile(r"\bmock\b", re.I),
    re.compile(r"\bstub\b", re.I),
    re.compile(r"\bfixture\b", re.I),
    # 英文 whole-word test(s)，避免匹配 contest / latest 等
    re.compile(r"(?<![a-z])tests?(?![a-z])", re.I),
)


def query_implies_test_intent(query_text: str) -> bool:
    if not (query_text or "").strip():
        return False
    text = query_text.strip()
    return any(p.search(text) for p in _TEST_QUERY_REGEXES)


def path_looks_like_test_source(path: str) -> bool:
    """根据路径判断是否大概率位于测试源码树或测试命名文件。"""
    if not path or not path.strip():
        return False
    raw = path.strip()
    p = raw.replace("\\", "/")
    pl = p.lower()
    # 常见目录布局
    if any(
        seg in pl
        for seg in (
            "/src/test/",
            "/test/java/",
            "/test/kotlin/",
            "/test/scala/",
            "/test/resources/",
            "/tests/",
            "/__tests__/",
            "/it/java/",
            "/integration-test/",
            "/integration_tests/",
        )
    ):
        return True
    if pl.startswith("test/") and "/src/" not in pl:
        return True
    base = pl.rsplit("/", 1)[-1]
    # Go
    if base.endswith("_test.go"):
        return True
    # Python
    if base.startswith("test_") and base.endswith(".py"):
        return True
    if base.endswith("_test.py"):
        return True
    # Java / Kotlin：*Test(s).java / Test*.java / *IT.java / *ITCase.java
    for suf in (".java", ".kt"):
        if base.endswith(suf):
            stem = base[: -len(suf)]
            orig_base = raw.rsplit("/", 1)[-1]
            if orig_base.endswith("IT.java") or orig_base.endswith("ITCase.java"):
                return True
            if stem.endswith("tests") and len(stem) > 5:
                return True
            if stem.endswith("test") and len(stem) > 4 and not stem.endswith("latest"):
                return True
            if stem.startswith("test") and len(stem) > len("test"):
                return True
    return False


def _stem_looks_like_test_artifact(stem: str) -> bool:
    s = stem.lower()
    if len(s) < 5:
        return False
    if s.endswith("tests"):
        return True
    if s.endswith("test"):
        return True
    if s.startswith("test") and len(s) > 4:
        return True
    return False


def symbol_id_suggests_test_path(symbol_id: str) -> bool:
    """SCIP symbol 串里常含路径片段或测试类名后缀，与 path 规则互补。"""
    if not symbol_id:
        return False
    sid = symbol_id.replace("\\", "/").lower()
    if any(
        m in sid
        for m in (
            "/src/test/",
            "/test/java/",
            "/test/kotlin/",
            "/tests/",
            "/__tests__/",
            "/it/java/",
        )
    ):
        return True
    tail = sid.rsplit("/", 1)[-1]
    if "#" in tail:
        tail = tail.split("#", 1)[0]
    if "." in tail:
        tail = tail.rsplit(".", 1)[0]
    return _stem_looks_like_test_artifact(tail)
