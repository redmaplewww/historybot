"""Run every local OCR/vision provider against downloaded real source images."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from historybot.ingestion.providers import RapidOCRProvider, TrOCRProvider


def _providers() -> dict[str, object]:
    return {
        "rapidocr": RapidOCRProvider(),
        "trocr-printed": TrOCRProvider("microsoft/trocr-small-printed"),
        "trocr-handwritten": TrOCRProvider("microsoft/trocr-small-handwritten"),
    }


async def evaluate(manifest: list[dict], models: list[str]) -> list[dict]:
    providers = _providers()
    reports = []
    for source in manifest:
        image = Path(source["local_file"])
        content = image.read_bytes()
        results = []
        for name in models:
            started = time.perf_counter()
            try:
                provider = providers[name]
                result = await provider.recognize(content, name=image.name, media_type="image/jpeg", asset_id=image.stem)
                results.append(
                    {
                        "model": name,
                        "elapsed_seconds": round(time.perf_counter() - started, 3),
                        **result.model_dump(mode="json"),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - reports preserve provider failures.
                results.append(
                    {
                        "model": name,
                        "elapsed_seconds": round(time.perf_counter() - started, 3),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
        reports.append({**source, "image": str(image), "models": results})
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("data/ocr_real/manifest.json"))
    parser.add_argument("--output", type=Path, default=Path("data/ocr_real/evaluation.json"))
    parser.add_argument(
        "--models",
        nargs="+",
        default=["rapidocr", "trocr-printed", "trocr-handwritten"],
        choices=["rapidocr", "trocr-printed", "trocr-handwritten"],
    )
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    reports = asyncio.run(evaluate(manifest, args.models))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)
    for report in reports:
        print(f"{Path(report['image']).name}: {len(report['models'])} model results")


if __name__ == "__main__":
    main()
