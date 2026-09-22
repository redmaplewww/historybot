"""Download reproducible, real historical manuscript images from Wikimedia Commons."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import httpx

SAMPLES = [
    {
        "slug": "sanguozhi_buzhi_fragment",
        "file_title": "File:A Fragment of Biography of Bu Zhi History Books of Three Kingdoms 01 2012-12.JPG",
        "source_page": "https://commons.wikimedia.org/wiki/File:A_Fragment_of_Biography_of_Bu_Zhi_History_Books_of_Three_Kingdoms_01_2012-12.JPG",
        "description": "东晋隶书《三国志·步骘传》残卷，敦煌研究院藏。",
        "source_institution": "敦煌研究院",
    },
    {
        "slug": "zhu_yuanzhang_edict",
        "file_title": "File:Manuscript of Zhu Yuanzhang, Collection of the Wuxi Museum.jpg",
        "source_page": "https://commons.wikimedia.org/wiki/File:Manuscript_of_Zhu_Yuanzhang,_Collection_of_the_Wuxi_Museum.jpg",
        "description": "无锡博物院藏朱元璋吴王手谕卷。",
        "source_institution": "无锡博物院",
    },
    {
        "slug": "pelliot_chinois_4646",
        "file_title": "File:Pelliot chinois 4646.jpg",
        "source_page": "https://commons.wikimedia.org/wiki/File:Pelliot_chinois_4646.jpg",
        "description": "Pelliot chinois 4646 敦煌写本图像。",
        "source_institution": "法国国家图书馆（馆藏编号 Pelliot chinois 4646）",
    },
]


def _plain(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value or "")
    return " ".join(value.split())


def resolve_file(client: httpx.Client, title: str) -> dict:
    params = {
        "action": "query",
        "titles": title,
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "format": "json",
    }
    payload = None
    for attempt in range(3):
        response = client.get("https://commons.wikimedia.org/w/api.php", params=params)
        response.raise_for_status()
        candidate = response.json()
        if "query" in candidate:
            payload = candidate
            break
        time.sleep(1 + attempt)
    if payload is None:
        raise RuntimeError(f"Commons API returned no query for {title}: {response.text[:300]}")
    pages = payload["query"]["pages"]
    page = next(iter(pages.values()))
    if "imageinfo" not in page:
        raise RuntimeError(f"Commons file was not found: {title}")
    info = page["imageinfo"][0]
    metadata = info.get("extmetadata", {})
    return {
        "page_id": page.get("pageid"),
        "source_file_url": info["url"],
        "mime": info.get("mime"),
        "width": info.get("width"),
        "height": info.get("height"),
        "size_bytes": info.get("size"),
        "license": _plain(metadata.get("LicenseShortName", {}).get("value", "见来源页")),
        "creator": _plain(metadata.get("Artist", {}).get("value", "")),
        "commons_description": _plain(metadata.get("ImageDescription", {}).get("value", "")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("data/ocr_real"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    manifest = []
    headers = {"User-Agent": "historybot-local-ocr-evaluation/0.1 (research tooling)"}
    with httpx.Client(timeout=120, follow_redirects=True, headers=headers) as client:
        for sample in SAMPLES:
            resolved = resolve_file(client, sample["file_title"])
            suffix = Path(resolved["source_file_url"].split("?")[0]).suffix or ".jpg"
            filename = f"{sample['slug']}{suffix.lower()}"
            destination = args.output_dir / filename
            if not destination.exists() or destination.stat().st_size != resolved["size_bytes"]:
                data = client.get(resolved["source_file_url"])
                data.raise_for_status()
                destination.write_bytes(data.content)
            manifest.append(
                {
                    **sample,
                    **resolved,
                    "local_file": str(destination),
                    "downloaded_bytes": destination.stat().st_size,
                }
            )

    output = args.output_dir / "manifest.json"
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    for item in manifest:
        print(f"{item['slug']}: {item['width']}x{item['height']} {item['license']} -> {item['local_file']}")


if __name__ == "__main__":
    main()
