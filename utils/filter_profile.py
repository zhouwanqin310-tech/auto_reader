"""
筛选画像与阈值读取/标准化工具
用于“召回更全 + 精筛更准”，保留 AI 画像筛选能力。
"""

from __future__ import annotations

from typing import Any, Dict


DEFAULT_THRESHOLDS = {
    "rule_recall_min_score": 10,
    "rule_precision_min_score": 18,
    "ai_min_confidence": 0.6,
    # 每个 job 的 AI 候选/调用硬限制（避免成本失控）
    "max_ai_candidates": 30,
    "max_ai_calls_per_job": 30,
}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def get_filter_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    从 CONFIG 读取 filter.profile 与 filter.thresholds，并做类型标准化。
    """
    filter_cfg = config.get("filter") or {}
    profile = filter_cfg.get("profile") or {}
    thresholds = {**DEFAULT_THRESHOLDS, **(filter_cfg.get("thresholds") or {})}

    # 类型标准化
    thresholds["rule_recall_min_score"] = _as_int(thresholds.get("rule_recall_min_score"), DEFAULT_THRESHOLDS["rule_recall_min_score"])
    thresholds["rule_precision_min_score"] = _as_int(
        thresholds.get("rule_precision_min_score"),
        DEFAULT_THRESHOLDS["rule_precision_min_score"],
    )
    thresholds["ai_min_confidence"] = _as_float(thresholds.get("ai_min_confidence"), DEFAULT_THRESHOLDS["ai_min_confidence"])
    thresholds["max_ai_candidates"] = _as_int(thresholds.get("max_ai_candidates"), DEFAULT_THRESHOLDS["max_ai_candidates"])
    thresholds["max_ai_calls_per_job"] = _as_int(thresholds.get("max_ai_calls_per_job"), DEFAULT_THRESHOLDS["max_ai_calls_per_job"])

    return {
        "profile": profile,
        "thresholds": thresholds,
    }


def get_ai_match_persona(config: Dict[str, Any]) -> str:
    """
    获取用于 ai_topic_matches_paper 的 persona。
    优先使用 filter.profile.ai_match_persona，其次回退到 config.ai_match.persona。
    """
    filter_cfg = config.get("filter") or {}
    profile = filter_cfg.get("profile") or {}
    persona = (profile.get("ai_match_persona") or "").strip()
    if persona:
        return persona
    return (config.get("ai_match") or {}).get("persona", "").strip()

