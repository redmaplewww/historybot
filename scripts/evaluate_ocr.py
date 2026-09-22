"""Run local OCR/vision providers against an image and emit a JSON report.

Examples:
  uv run python scripts/evaluate_ocr.py --image scan.png --output data/ocr_report.json
  uv run python scripts/evaluate_ocr.py --image handwritten.png --models rapidocr trocr-handwritten
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from historybot.ingestion.providers import RapidOCRProvider, TrOCRProvider


async def evaluate(image: Path, models: list[str]) -> list[dict]:
    content = image.read_bytes()
    results = []
    for name in models:
        started = time.perf_counter()
        try:
            provider = {
                "rapidocr": RapidOCRProvider(),
                "trocr-printed": TrOCRProvider("microsoft/trocr-small-printed"),
                "trocr-handwritten": TrOCRProvider("microsoft/trocr-small-handwritten"),
            }[name]
            result = await provider.recognize(content, name=image.name, media_type="image/*", asset_id=image.stem)
            results.append({"model": name, "elapsed_seconds": round(time.perf_counter() - started, 3), **result.model_dump(mode="json")})
        except Exception as exc:  # noqa: BLE001  Reports must preserve failures for comparison.
            results.append({"model": name, "elapsed_seconds": round(time.perf_counter() - started, 3), "error_type": type(exc).__name__, "error": str(exc)})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate local OCR and visual OCR providers")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--models", nargs="+", default=["rapidocr", "trocr-printed", "trocr-handwritten"], choices=["rapidocr", "trocr-printed", "trocr-handwritten"])
    args = parser.parse_args()
    report = {"image": str(args.image), "models": asyncio.run(evaluate(args.image, args.models))}
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
