"""
小规模回归测试（不依赖网络）
用于验证筛选配置与历史指纹逻辑的正确性。
"""

from __future__ import annotations

import sys
import yaml
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from utils.filter_profile import get_filter_config, get_ai_match_persona
from utils.helpers import paper_to_hash


def _load_config():
    repo_root = Path(__file__).resolve().parent.parent
    config_path = repo_root / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_filter_profile_parsing():
    cfg = _load_config()
    fc = get_filter_config(cfg)

    assert "profile" in fc and isinstance(fc["profile"], dict)
    assert "thresholds" in fc and isinstance(fc["thresholds"], dict)

    th = fc["thresholds"]
    assert isinstance(th["rule_recall_min_score"], int)
    assert isinstance(th["rule_precision_min_score"], int)
    assert isinstance(th["ai_min_confidence"], float)
    assert isinstance(th["max_ai_candidates"], int)
    assert isinstance(th["max_ai_calls_per_job"], int)

    persona = get_ai_match_persona(cfg)
    assert persona and isinstance(persona, str)
    # 验证 persona 确实来自 filter.profile（你在 config.yaml 里配置了 ai_match_persona）
    assert "CS/AI" in persona or "计算语言学" in persona


def test_paper_hash_fingerprint_stable():
    # 同 DOI 不因标题变化而改变
    p1 = {"doi": "10.1000/xyz123", "title": "A", "authors": ["Alice"], "published": "2026-01-01"}
    p2 = {"doi": "10.1000/xyz123", "title": "Different title", "authors": ["Bob"], "published": "2026-01-02"}
    assert paper_to_hash(p1) == paper_to_hash(p2)

    # 同 arXiv id 也应稳定
    p3 = {"arxiv_id": "1234.5678", "title": "T1", "authors": ["C"], "published": "2026-01-01"}
    p4 = {"arxiv_id": "1234.5678", "title": "T2", "authors": ["D"], "published": "2026-02-01"}
    assert paper_to_hash(p3) == paper_to_hash(p4)


if __name__ == "__main__":
    test_filter_profile_parsing()
    test_paper_hash_fingerprint_stable()
    print("test_filter_logic: OK")

