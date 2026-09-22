from __future__ import annotations

import re
import unicodedata

from ..domain import NormalizedDocument, RecognitionResult


def normalize_document(recognition: RecognitionResult) -> NormalizedDocument:
    original = recognition.text
    text = unicodedata.normalize("NFKC", original).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [(raw.strip(), offset, end) for raw, offset, end in _lines_with_offsets(text)]
    paragraphs: list[dict] = []
    current: list[str] = []
    start = end = 0
    for line, line_start, line_end in lines:
        if not line:
            if current:
                paragraphs.append({"ordinal": len(paragraphs), "text": " ".join(current), "source_start": start, "source_end": end})
                current = []
        else:
            if not current:
                start = line_start
            end = line_end
            current.append(line)
    if current:
        paragraphs.append({"ordinal": len(paragraphs), "text": " ".join(current), "source_start": start, "source_end": end})
    normalized = "\n\n".join(item["text"] for item in paragraphs)
    changes = []
    if normalized != original:
        changes.append({"kind": "whitespace_unicode", "before_hash": _short(original), "after_hash": _short(normalized)})
    return NormalizedDocument(id=f"norm_{recognition.id[4:]}", recognition_id=recognition.id, text=normalized, paragraphs=paragraphs, changes=changes)


def _short(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _lines_with_offsets(text: str):
    offset = 0
    for raw in text.splitlines(keepends=True):
        yield raw.rstrip("\r\n"), offset, offset + len(raw)
        offset += len(raw)
