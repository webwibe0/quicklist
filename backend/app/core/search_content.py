"""Versioned cache identity only; no indexing or search execution."""
import hashlib
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class SearchContent:
    canonical_text: str
    content_hash: str
    text_version: int = 1


def search_content(title: str) -> SearchContent:
    # Preserve case, е/ё, names, numbers and negations in v1.
    canonical = " ".join(unicodedata.normalize("NFC", title).split())
    if not canonical:
        raise ValueError("Search content must not be empty")
    return SearchContent(canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest())
