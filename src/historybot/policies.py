from __future__ import annotations

from .domain import SourceKind

SOURCE_POLICY_VERSION = "source-policy-v1"


def assess_source(kind: SourceKind) -> dict:
    """Transparent baseline assessment; factual claims still require passage evidence."""
    policy = {
        SourceKind.PRIMARY: (0.82, "接近事件发生时形成的原始记录，仍需核查作者立场与传本"),
        SourceKind.CONTEMPORARY: (0.78, "与事件时代接近的记录，需核查是否为独立来源"),
        SourceKind.COMPILATION: (0.68, "后世编纂或正史，具有史料价值但受编纂取舍影响"),
        SourceKind.MODERN_STUDY: (0.62, "现代研究可提供解释与引文，但不能替代原始证据"),
        SourceKind.FOLK: (0.35, "文学或民间传统，适合研究叙事传播，不能直接当作史实"),
        SourceKind.UNKNOWN: (0.25, "来源类型未知，需人工核验后才能提高评级"),
    }
    score, rationale = policy[kind]
    return {"policy_version": SOURCE_POLICY_VERSION, "baseline_score": score, "rationale": rationale, "requires_corroboration": score < 0.7}


def preliminary_claim_confidence(extraction_score: float, source_assessments: list[dict]) -> dict:
    """Combine model extraction quality with the weakest bound source baseline."""

    source_reliability = min(
        (float(item.get("baseline_score", 0.25)) for item in source_assessments),
        default=0.25,
    )
    score = min(float(extraction_score), source_reliability)
    rationale = "候选主张已绑定原文段落，等待证据审查"
    if source_assessments:
        rationale += f"；当前来源基线取最低值 {source_reliability:.2f}"
    else:
        rationale += "；未找到来源评估，按未知来源基线 0.25 处理"
    return {
        "score": score,
        "source_reliability": source_reliability,
        "rationale": rationale,
    }
