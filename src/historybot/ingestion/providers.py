from __future__ import annotations

import io
import mimetypes
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree

from ..domain import RecognitionResult, RecognitionSpan
from ..ingest import clean_wikitext, fetch_html


class InputReader(Protocol):
    def supports(self, media_type: str, name: str) -> bool: ...
    async def read(self, content: bytes, *, name: str, media_type: str, asset_id: str) -> RecognitionResult: ...


class OCRProvider(Protocol):
    async def recognize(self, content: bytes, *, name: str, media_type: str, asset_id: str) -> RecognitionResult: ...


@dataclass
class PlainTextReader:
    processor_version: str = "1"

    def supports(self, media_type: str, name: str) -> bool:
        suffix = Path(name).suffix.lower()
        return media_type.startswith("text/") or suffix in {".txt", ".md", ".csv", ".json"}

    async def read(self, content: bytes, *, name: str, media_type: str, asset_id: str) -> RecognitionResult:
        text = content.decode("utf-8", errors="replace")
        confidence = 1.0 if "\ufffd" not in text else 0.65
        return _result(asset_id, text, "text", confidence, self.processor_version)


@dataclass
class HtmlReader:
    processor_version: str = "1"

    def supports(self, media_type: str, name: str) -> bool:
        return media_type in {"text/html", "application/xhtml+xml"} or Path(name).suffix.lower() in {".html", ".htm"}

    async def read(self, content: bytes, *, name: str, media_type: str, asset_id: str) -> RecognitionResult:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(content.decode("utf-8", errors="replace"), "html.parser")
        for node in soup(["script", "style", "noscript"]):
            node.decompose()
        text = clean_wikitext(soup.get_text("\n", strip=True))
        return _result(asset_id, text, "html", 0.98, self.processor_version)


@dataclass
class UrlReader:
    processor_version: str = "1"

    def supports(self, media_type: str, name: str) -> bool:
        return name.startswith(("http://", "https://"))

    async def read(self, content: bytes, *, name: str, media_type: str, asset_id: str) -> RecognitionResult:
        _, text = await fetch_html(name)
        return _result(asset_id, text, "url", 0.98, self.processor_version)


@dataclass
class PdfReader:
    processor_version: str = "1"

    def supports(self, media_type: str, name: str) -> bool:
        return media_type == "application/pdf" or Path(name).suffix.lower() == ".pdf"

    async def read(self, content: bytes, *, name: str, media_type: str, asset_id: str) -> RecognitionResult:
        try:
            from pypdf import PdfReader as Reader
        except ImportError as exc:
            raise RuntimeError("PDF input requires optional dependency pypdf") from exc
        pages = [page.extract_text() or "" for page in Reader(content).pages]
        spans = [RecognitionSpan(text=page, page=i + 1, confidence=0.9, source="pdf") for i, page in enumerate(pages)]
        text = "\n\n".join(pages)
        return RecognitionResult(id=f"rec_{asset_id}", raw_asset_id=asset_id, text=text, spans=spans, language="und", processor="pdf", processor_version=self.processor_version, confidence=0.9, needs_review=not bool(text.strip()))


@dataclass
class OfficeReader:
    """Dependency-light reader for DOCX and XLSX package formats."""

    processor_version: str = "1"

    def supports(self, media_type: str, name: str) -> bool:
        suffix = Path(name).suffix.lower()
        return suffix in {".docx", ".xlsx", ".xlsm"} or media_type in {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }

    async def read(self, content: bytes, *, name: str, media_type: str, asset_id: str) -> RecognitionResult:
        suffix = Path(name).suffix.lower()
        with zipfile.ZipFile(io.BytesIO(content)) as package:
            if suffix == ".docx" or "word/document.xml" in package.namelist():
                root = ElementTree.fromstring(package.read("word/document.xml"))
                ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                paragraphs = ["".join(node.text or "" for node in paragraph.findall(".//w:t", ns)) for paragraph in root.findall(".//w:p", ns)]
                text = "\n\n".join(item for item in paragraphs if item)
                return _result(asset_id, text, "docx", 0.96, self.processor_version)
            shared = []
            if "xl/sharedStrings.xml" in package.namelist():
                root = ElementTree.fromstring(package.read("xl/sharedStrings.xml"))
                ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                shared = ["".join(node.text or "" for node in item.findall(".//a:t", ns)) for item in root.findall("a:si", ns)]
            sheet = next((item for item in package.namelist() if item.startswith("xl/worksheets/sheet") and item.endswith(".xml")), None)
            if not sheet:
                raise ValueError("XLSX contains no worksheet")
            root = ElementTree.fromstring(package.read(sheet))
            ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            rows = []
            for row in root.findall(".//a:row", ns):
                values = []
                for cell in row.findall("a:c", ns):
                    value = cell.find("a:v", ns)
                    text_value = value.text if value is not None else ""
                    if cell.get("t") == "s" and text_value.isdigit() and int(text_value) < len(shared):
                        text_value = shared[int(text_value)]
                    values.append(text_value)
                rows.append(" | ".join(values))
            return _result(asset_id, "\n".join(rows), "xlsx", 0.94, self.processor_version)


@dataclass
class TextOCRProvider:
    """Safe fallback for image input; it never invents text."""

    processor_version: str = "1"

    async def recognize(self, content: bytes, *, name: str, media_type: str, asset_id: str) -> RecognitionResult:
        return RecognitionResult(id=f"rec_{asset_id}", raw_asset_id=asset_id, text="", spans=[], processor="ocr-unconfigured", processor_version=self.processor_version, confidence=0.0, needs_review=True, metadata={"reason": "no OCR provider configured", "media_type": media_type})


@dataclass
class RapidOCRProvider:
    """Local OCR using RapidOCR's bundled ONNX detection/recognition models."""

    processor_version: str = "rapidocr-onnxruntime-1.4"
    _engine: object | None = None

    def _get_engine(self):
        if self._engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as exc:
                raise RuntimeError("RapidOCR provider requires rapidocr-onnxruntime") from exc
            self._engine = RapidOCR()
        return self._engine

    async def recognize(self, content: bytes, *, name: str, media_type: str, asset_id: str) -> RecognitionResult:
        result, elapsed = self._get_engine()(content)
        spans: list[RecognitionSpan] = []
        if result:
            for item in result:
                box, text, score = item[0], str(item[1]), float(item[2])
                flat_box = [float(value) for point in box for value in point]
                spans.append(RecognitionSpan(text=text, bbox=flat_box, confidence=max(0.0, min(1.0, score)), source="rapidocr"))
        text = "\n".join(item.text for item in spans)
        confidence = sum(item.confidence for item in spans) / len(spans) if spans else 0.0
        return RecognitionResult(id=f"rec_{asset_id}", raw_asset_id=asset_id, text=text, spans=spans, processor="rapidocr", processor_version=self.processor_version, confidence=confidence, needs_review=confidence < 0.8 or not spans, metadata={"elapsed": elapsed, "engine": "onnxruntime"})


@dataclass
class TrOCRProvider:
    """Local vision OCR backed by Microsoft's TrOCR checkpoints."""

    model_name: str = "microsoft/trocr-small-printed"
    processor_version: str = "transformers-trocr"
    device: str | None = None
    _processor: object | None = None
    _model: object | None = None
    _tokenizer: object | None = None

    def _load(self) -> tuple[object, object, object, str]:
        if self._processor is None or self._model is None or self._tokenizer is None:
            try:
                import torch
                from transformers import TrOCRProcessor, VisionEncoderDecoderModel
            except ImportError as exc:
                raise RuntimeError("TrOCR provider requires torch, transformers and Pillow") from exc
            self._processor = TrOCRProcessor.from_pretrained(self.model_name)
            self._tokenizer = self._processor.tokenizer
            self._model = VisionEncoderDecoderModel.from_pretrained(self.model_name)
            selected = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
            self._model.to(selected)
            self._model.eval()
            self.device = selected
        return self._processor, self._model, self._tokenizer, self.device or "cpu"

    async def recognize(self, content: bytes, *, name: str, media_type: str, asset_id: str) -> RecognitionResult:
        try:
            from io import BytesIO

            import torch
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("TrOCR provider requires torch, transformers and Pillow") from exc
        processor, model, tokenizer, device = self._load()
        image = Image.open(BytesIO(content)).convert("RGB")
        inputs = processor(images=image, return_tensors="pt")
        pixel_values = inputs.pixel_values.to(device)
        with torch.inference_mode():
            generated = model.generate(
                pixel_values,
                max_new_tokens=128,
                output_scores=True,
                return_dict_in_generate=True,
            )
        text = tokenizer.batch_decode(generated.sequences, skip_special_tokens=True)[0].strip()
        confidence = 0.0
        if text and generated.scores:
            transition = model.compute_transition_scores(
                generated.sequences,
                generated.scores,
                normalize_logits=True,
            )
            confidence = float(torch.exp(transition).mean().item())
            confidence = max(0.0, min(1.0, confidence))
        span = RecognitionSpan(text=text, confidence=confidence, source=self.model_name) if text else None
        return RecognitionResult(
            id=f"rec_{asset_id}",
            raw_asset_id=asset_id,
            text=text,
            spans=[span] if span else [],
            processor="trocr",
            processor_version=self.processor_version,
            confidence=confidence,
            needs_review=True,
            metadata={
                "model": self.model_name,
                "device": device,
                "note": "TrOCR is line-level OCR; layout detection remains separate",
                "confidence_method": "mean generated-token probability",
            },
        )


def _result(asset_id: str, text: str, processor: str, confidence: float, version: str) -> RecognitionResult:
    spans = [RecognitionSpan(text=text, confidence=confidence, source=processor)] if text else []
    return RecognitionResult(id=f"rec_{asset_id}", raw_asset_id=asset_id, text=text, spans=spans, processor=processor, processor_version=version, confidence=confidence, needs_review=confidence < 0.8 or not text.strip())


def detect_media_type(name: str, content: bytes) -> str:
    if name.startswith(("http://", "https://")):
        return "text/html"
    media_type = mimetypes.guess_type(name)[0]
    if media_type:
        return media_type
    if content.startswith(b"%PDF"):
        return "application/pdf"
    return "application/octet-stream"


def default_ocr_provider() -> OCRProvider:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return TextOCRProvider()
    return RapidOCRProvider()
