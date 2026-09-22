from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from .domain import SourceKind

WIKISOURCE_API = "https://zh.wikisource.org/w/api.php?action=parse&prop=wikitext&format=json&page="

# The 65 volumes are the complete transmitted text of Chen Shou's Sanguozhi.
SAN_GUO_ZHI_VOLUMES = tuple(range(1, 66))

# A bounded literary sample gives the evidence layer independent material to compare
# without pretending that a full novel is a historical source.
FOLK_COMPARISON_CHAPTERS = (1, 5, 21, 25, 27, 38, 43, 44, 46, 49, 50, 57, 77, 78, 85, 86)


@dataclass(frozen=True)
class CorpusSource:
    page: str
    url: str
    title: str
    author: str
    kind: SourceKind
    collection: str
    sequence: int


def wikisource_url(page: str) -> str:
    return f"{WIKISOURCE_API}{quote(page, safe='')}"


def complete_corpus() -> tuple[CorpusSource, ...]:
    historical = tuple(
        CorpusSource(
            page=f"三國志/卷{volume:02d}",
            url=wikisource_url(f"三國志/卷{volume:02d}"),
            title=f"三国志·卷{volume:02d}",
            author="陈寿",
            kind=SourceKind.COMPILATION,
            collection="三国志",
            sequence=volume,
        )
        for volume in SAN_GUO_ZHI_VOLUMES
    )
    folk = tuple(
        CorpusSource(
            page=f"三國演義/第{chapter:03d}回",
            url=wikisource_url(f"三國演義/第{chapter:03d}回"),
            title=f"三国演义·第{chapter:03d}回",
            author="罗贯中",
            kind=SourceKind.FOLK,
            collection="三国演义对照样本",
            sequence=chapter,
        )
        for chapter in FOLK_COMPARISON_CHAPTERS
    )
    return historical + folk
