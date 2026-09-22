from __future__ import annotations

from dataclasses import dataclass, field

from ..domain import AnnotationSet, CompletenessAssessment

CORE_DIMENSIONS = ("source", "time", "place", "person", "event", "evidence")


@dataclass
class TemplateRegistry:
    templates: dict[str, tuple[str, ...]] = field(default_factory=lambda: {"generic": CORE_DIMENSIONS, "historical": CORE_DIMENSIONS + ("author", "source_type")})

    def required(self, name: str) -> tuple[str, ...]:
        return self.templates.get(name, self.templates["generic"])


class CompletenessChecker:
    def __init__(self, registry: TemplateRegistry | None = None):
        self.registry = registry or TemplateRegistry()

    def assess(self, annotation_set: AnnotationSet, template: str = "historical") -> CompletenessAssessment:
        required = list(self.registry.required(template))
        present = sorted({item.label for item in annotation_set.annotations})
        missing = [field for field in required if field not in present]
        conflicts = sorted({item.label for item in annotation_set.annotations if item.metadata.get("conflict")})
        evidence_missing = any(
            item.evidence_kind.value == "inferred"
            and not any(
                other.label == item.label and other.evidence_kind.value != "inferred"
                for other in annotation_set.annotations
            )
            for item in annotation_set.annotations
        )
        score = len([field for field in required if field in present]) / max(len(required), 1)
        blocking = bool(missing or conflicts or evidence_missing)
        rationale = "关键维度齐全且均有原文标注" if not blocking else "；".join(filter(None, [f"缺失：{','.join(missing)}" if missing else "", f"冲突：{','.join(conflicts)}" if conflicts else "", "存在推断字段，需复核" if evidence_missing else ""]))
        return CompletenessAssessment(id=f"assessment_{annotation_set.id[4:]}", annotation_set_id=annotation_set.id, template=template, required_dimensions=required, present_dimensions=present, missing_dimensions=missing, conflict_dimensions=conflicts, score=score, blocking=blocking, rationale=rationale)
