from __future__ import annotations

from typing import Iterable


# 你可以直接修改本文件来调整 prompt 行为。
# - 函数级 intent：FUNCTION_INTENT_SYSTEM + render_function_intent_user_prompt
# - 模块级 intent：MODULE_INTENT_SYSTEM + render_module_intent_user_prompt

FUNCTION_INTENT_SYSTEM = (
    "You are a senior C/C++ code analyst. "
    "Infer function intent from signature, call topology, and code evidence. "
    "Be precise, avoid vague wording, and do not hallucinate behavior."
)

MODULE_INTENT_SYSTEM = (
    "You are a software architect summarizing clustered function communities. "
    "Produce module-level intent that captures shared responsibility and boundaries."
)


def _line_list(items: Iterable[str], max_items: int = 8) -> str:
    vals = [str(x).strip() for x in items if str(x).strip()]
    if not vals:
        return "- (none)"
    return "\n".join(f"- {x}" for x in vals[:max_items])


def _neighbor_detail_list(items: list[dict[str, str]], max_items: int = 5) -> str:
    if not items:
        return "- (none)"
    rows: list[str] = []
    for item in items[:max_items]:
        node_id = item.get("node_id", "").strip() or "(unknown)"
        signature = item.get("signature", "").strip() or "(signature unavailable)"
        role = item.get("role", "").strip() or "unknown-role"
        intent = item.get("intent_text", "").strip() or "(intent unavailable)"
        rows.append(f"- {node_id} | role={role} | sig={signature} | intent={intent}")
    return "\n".join(rows)


def render_function_intent_user_prompt(
    *,
    signature: str,
    path: str,
    fan_in: int,
    fan_out: int,
    role_hint: str,
    callers: list[str],
    callees: list[str],
    caller_details: list[dict[str, str]],
    callee_details: list[dict[str, str]],
    code_snippet: str,
    neighbor_top_k: int = 5,
) -> str:
    k = max(1, neighbor_top_k)
    return (
        "Task: Generate ONE concise function-level intent sentence.\n"
        "Constraints:\n"
        "- 18-40 words.\n"
        "- Must include: primary action + key object/data + control role (entry/orchestrator/leaf/core).\n"
        "- Must rely only on provided evidence.\n"
        "- No markdown, no bullet points.\n\n"
        "Function Evidence:\n"
        f"Signature: {signature}\n"
        f"Path: {path}\n"
        f"FanIn/FanOut: {fan_in}/{fan_out}\n"
        f"Topology Role Hint: {role_hint}\n"
        "Top Callers:\n"
        f"{_line_list(callers, max_items=k)}\n"
        "Caller Details:\n"
        f"{_neighbor_detail_list(caller_details, max_items=k)}\n"
        "Top Callees:\n"
        f"{_line_list(callees, max_items=k)}\n"
        "Callee Details:\n"
        f"{_neighbor_detail_list(callee_details, max_items=k)}\n"
        "Code Snippet:\n"
        f"{code_snippet[:1800]}"
    )


def render_module_intent_user_prompt(
    *,
    community_size: int,
    dominant_role: str,
    sample_roles: list[str],
    sample_paths: list[str],
    representative_function_intents: list[str],
) -> str:
    return (
        "Task: Generate ONE concise module-level intent sentence for this clustered community.\n"
        "Constraints:\n"
        "- 18-45 words.\n"
        "- Describe shared responsibility, not individual implementation details.\n"
        "- Mention one scope/boundary hint (e.g., parser/matcher/runtime/io).\n"
        "- Must be grounded in evidence below.\n"
        "- No markdown, no bullet points.\n\n"
        "Community Evidence:\n"
        f"Community Size: {community_size}\n"
        f"Dominant Role: {dominant_role}\n"
        "Sample Roles:\n"
        f"{_line_list(sample_roles, max_items=8)}\n"
        "Sample Paths:\n"
        f"{_line_list(sample_paths, max_items=8)}\n"
        "Representative Function Intents:\n"
        f"{_line_list(representative_function_intents, max_items=10)}"
    )

