from __future__ import annotations

import re
from functools import lru_cache

from opencc import OpenCC


@lru_cache(maxsize=1)
def _converter() -> OpenCC:
    return OpenCC("t2s")


@lru_cache(maxsize=100_000)
def normalize_surface(value: str) -> str:
    """Return a stable simplified-Chinese surface for identity matching.

    Original surfaces remain in entity payloads and evidence quotes. This function is
    only for matching and projection, so a source's orthography is never overwritten.
    """

    value = value.strip()
    # Some source mirrors preserve lightweight wiki/Markdown markup inside names
    # (for example ``徐-{干}-``).  Strip only that markup before matching; raw
    # source text and evidence quotes remain untouched in storage.
    previous = None
    while previous != value:
        previous = value
        value = re.sub(r"-\{([^{}]+)\}-", r"\1", value)
        value = re.sub(r"\{([^{}]+)\}", r"\1", value)
    value = re.sub(r"\s+", "", value)
    return _converter().convert(value)
