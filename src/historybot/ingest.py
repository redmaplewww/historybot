from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from datetime import UTC, datetime

import httpx
from bs4 import BeautifulSoup

from .domain import Passage, Source, SourceKind
from .policies import assess_source
from .store import KnowledgeStore

# Templates whose rendered output carries source text, mapped to the argument
# that is displayed. ``clean_wikitext`` keeps these and drops only
# presentational wrappers.
#
# Verified against the MediaWiki parser (scripts/probe_template_rendering.py):
#   {{YL|興平元年|194年}} -> 興平元年   (the chronicle's era-year anchor)
#   {{ProperNoun|曹操}}    -> 曹操
#
# Dropping ``yl`` removed every absolute-year anchor, so 是歲 / 二年春
# statements lost the era-year context they must inherit and the temporal
# repair had nothing to resolve against.
#
# ``{{*|…}}`` (裴松之注) and ``{{quote|…}}`` also render text and are currently
# dropped. Restoring them is deliberately out of scope here: 裴注 is a later
# commentator whose words must not be merged into 陳壽's base text without an
# explicit provenance decision. See artifacts/ingest_text_loss.json.
CONTENT_TEMPLATES: dict[str, int] = {
    "propernoun": 0,
    "proper noun": 0,
    "yl": 0,
}

# The mapping used before ``yl`` was preserved. Kept so a repair can recompute
# the exact text that produced the rows already in a database, which is the
# prerequisite for restoring era-year text without moving passage boundaries.
LEGACY_CONTENT_TEMPLATES: dict[str, int] = {
    "propernoun": 0,
    "proper noun": 0,
}

# Template names the ingest cleaner makes a keep/drop decision about. Kept as a
# list so the audit scripts can verify each decision against real definitions.
WIKISOURCE_TEMPLATE_NAMES = (
    "YL",
    "*",
    "\u53e6",
    "quote",
    "gap",
    "ul",
    "WavyBookMark",
    "red",
    "blue",
    "green",
    "ProperNoun",
)


def stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def split_passages(text: str, *, source_id: str) -> list[Passage]:
    chunks = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"\n+|(?<=[。！？])", text)]
    # Short chronicle entries are often semantically complete year boundaries
    # (for example, "四年春，军鄄城。"). Dropping them silently removes the
    # temporal context required by the following passages, so retain every
    # non-empty normalized chunk.
    chunks = [part for part in chunks if part]
    return [Passage(id=stable_id("p", f"{source_id}:{index}:{part}"), source_id=source_id, ordinal=index, text=part, locator={"ordinal": index}, text_hash=hashlib.sha256(part.encode()).hexdigest()) for index, part in enumerate(chunks)]


def split_passages_with_spans(text: str) -> list[tuple[str, int, int]]:
    """Split text exactly like :func:`split_passages`, also returning offsets.

    The returned ``(normalized_text, start, end)`` triples let a repair map a
    character position in the source back to the passage that contains it,
    which is what keeps an in-place text fix from moving passage boundaries.
    """
    boundaries = [match.end() for match in re.finditer(r"\n+|(?<=[。！？])", text)]
    spans = []
    previous = 0
    for boundary in [*boundaries, len(text)]:
        chunk = text[previous:boundary]
        normalized = re.sub(r"\s+", " ", chunk).strip()
        if normalized:
            spans.append((normalized, previous, boundary))
        previous = boundary
    return spans


def raw_to_stored_offset(chunk: str, raw_index: int) -> int:
    """Map an index in a raw chunk to its position in the stored passage text.

    ``split_passages`` stores whitespace-collapsed, stripped text, while
    insertions are located in the raw cleaned text. Walking the normalization
    explicitly keeps the two coordinate spaces aligned, so an inserted anchor
    lands between the right characters instead of mid-word.
    """
    normalized_prefix = re.sub(r"\s+", " ", chunk[:raw_index])
    whole = re.sub(r"\s+", " ", chunk)
    leading = len(whole) - len(whole.lstrip())
    return max(0, len(normalized_prefix) - min(leading, len(normalized_prefix)))


def inserted_spans(old: str, new: str) -> list[tuple[int, str]]:
    """Return ``(offset_in_old, inserted_text)`` for text added to ``old``.

    Both strings are cleanings of the same wiki page, so preserving a template
    only adds what that template renders. The insertions are recovered by a
    block alignment that requires every difference to be a pure insertion;
    any delete or replace raises, so a rewrite can never be mistaken for an
    insertion. A naive character walk would do, but repeated characters would
    let it match the wrong occurrence and place an anchor mid-word.
    """
    insertions: list[tuple[int, str]] = []
    for tag, old_start, old_end, new_start, new_end in SequenceMatcher(
        None, old, new, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            insertions.append((old_start, new[new_start:new_end]))
            continue
        raise ValueError(
            f"{tag} at old[{old_start}:{old_end}] rewrites text instead of "
            "inserting into it"
        )
    return insertions


def strip_markup_residue(text: str) -> str:
    """Remove leftover wiki markup from a stored passage's text.

    Language-variant markers (``-{才}-``) and behaviour switches
    (``__FORCETOC__``) render as nothing but survived the original cleaning.
    Both are self-contained, so they are stripped per passage: no passage
    boundary moves and no passage ID changes owner.
    """
    text = re.sub(r"__[A-Z][A-Z0-9_]*__", "", strip_language_variants(text))
    # Section headings ("==張遼==", "===子 譚、熙、尚===") are display structure,
    # not content. Removing the "=" delimiters keeps the heading text, which
    # names the biography a passage belongs to.
    text = re.sub(r"={2,}\s*(.*?)\s*={2,}", r"\1", text)
    return text.strip()


async def fetch_html(url: str) -> tuple[str, str]:
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        response = await client.get(url, headers={"User-Agent": "historybot-research/0.1"})
        response.raise_for_status()
    decoded = response.content.decode("utf-8", errors="replace")
    if "application/json" in response.headers.get("content-type", "") and "w/api.php" in url:
        payload = response.json()
        parsed = payload.get("parse", {})
        return parsed.get("title", url), clean_wikitext(parsed.get("wikitext", {}).get("*", ""))
    soup = BeautifulSoup(decoded, "html.parser")
    for node in soup(["script", "style", "noscript"]): node.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else url
    text = soup.get_text("\n", strip=True)
    return title, text


def clean_wikitext(text: str, *, templates: dict[str, int] | None = None) -> str:
    """Convert public MediaWiki source into readable passages without inventing text."""
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = strip_templates(text, templates if templates is not None else CONTENT_TEMPLATES)
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[https?://[^ ]+ ([^\]]+)\]", r"\1", text)
    text = strip_language_variants(text)
    # Behaviour switches such as __FORCETOC__ render as nothing but remain in
    # the wikitext. Left in a passage they corrupt the text and, on at least
    # one gateway, trip the upstream safety filter.
    text = re.sub(r"__[A-Z][A-Z0-9_]*__", "", text)
    # Section headings are display structure; keep the heading text, drop the
    # "=" delimiters that would otherwise sit inside a passage.
    text = re.sub(r"={2,}\s*(.*?)\s*={2,}", r"\1", text)
    text = text.replace("'''", "").replace("''", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def strip_language_variants(text: str) -> str:
    """Resolve MediaWiki language-conversion markup to the displayed form.

    Wikisource marks script variants as ``-{才}-`` and can carry per-variant
    text as ``-{zh-hans:后;zh-hant:後}-``. The parser renders one variant; the
    cleaner previously left the raw markup in the passage text.
    """
    while "-{" in text:
        start = text.find("-{")
        end = text.find("}-", start + 2)
        if end == -1:
            text = text[:start] + text[start + 2 :]
            continue
        body = text[start + 2 : end]
        replacement = body
        if "|" in body:
            replacement = body.split("|")[-1]
        elif ":" in body and ";" in body:
            # -{zh-hans:后;zh-hant:後}- shows the simplified form by default.
            for part in body.split(";"):
                if ":" in part:
                    _, _, value = part.partition(":")
                    replacement = value
                    break
        text = text[:start] + replacement + text[end + 2 :]
    return text


def split_template_arguments(body: str) -> list[str]:
    """Split a template body on top-level pipes only.

    Wikisource annotations embed wiki links whose targets and labels can
    themselves contain ``|``; splitting naively would slice those arguments in
    half and leak markup into the corpus text.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for index, char in enumerate(body):
        pair = body[index : index + 2]
        if pair in {"{{", "[["}:
            depth += 1
        elif pair in {"}}", "]]"}:
            depth -= 1
        if char == "|" and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def strip_templates(text: str, templates: dict[str, int] | None = None) -> str:
    """Remove non-display templates while preserving templates that render text."""
    mapping = templates if templates is not None else CONTENT_TEMPLATES
    while "{{" in text:
        start = text.find("{{")
        depth = 0
        end = None
        for index in range(start, len(text) - 1):
            token = text[index:index + 2]
            if token == "{{":
                depth += 1
            elif token == "}}":
                depth -= 1
                if depth == 0:
                    end = index + 2
                    break
        if end is None:
            text = text[:start]
            break
        body = text[start + 2 : end - 2]
        parts = split_template_arguments(body)
        template_name = parts[0].strip().casefold()
        arguments = parts[1:]
        argument_index = mapping.get(template_name)
        replacement = (
            arguments[argument_index].strip()
            if argument_index is not None and argument_index < len(arguments)
            else ""
        )
        text = text[:start] + replacement + text[end:]
    return text


async def ingest_url(url: str, *, title: str | None = None, author: str | None = None, kind: SourceKind = SourceKind.COMPILATION, store: KnowledgeStore, task_id: str) -> Source:
    fetched_title, text = await fetch_html(url)
    source = Source(id=stable_id("src", url), title=title or fetched_title, author=author, url=url, kind=kind, access_date=datetime.now(UTC), metadata={"assessment": assess_source(kind)})
    store.save_source(source, task_id)
    store.save_passages(split_passages(text, source_id=source.id), task_id)
    return source
