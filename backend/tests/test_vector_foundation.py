"""Pure foundation checks: no provider, network, or database."""
import pytest

from app.core.config import Settings
from app.core.search_content import search_content


def test_semantic_feature_is_disabled_by_default():
    assert Settings(_env_file=None).semantic_search_enabled is False


def test_unicode_and_whitespace_are_normalized_without_losing_meaning():
    content = search_content("  Не\u00a0звонить\nСемёну  в 7 ")
    assert content.canonical_text == "Не звонить Семёну в 7"
    assert content.content_hash == search_content("Не звонить Семёну в 7").content_hash
    assert search_content("и\u0306").canonical_text == "й"


@pytest.mark.parametrize("left,right", [
    ("все", "всё"), ("Семен", "Семён"), ("не звонить", "звонить"),
    ("в 7", "в 8"), ("Иван", "Иванов"),
])
def test_distinct_content_has_distinct_cache_key(left, right):
    assert search_content(left).content_hash != search_content(right).content_hash


@pytest.mark.parametrize("value", ["", " \t\n\u00a0"])
def test_empty_content_is_rejected(value):
    with pytest.raises(ValueError):
        search_content(value)


def test_hash_collision_must_not_reuse_different_canonical_text():
    from app.models.search import EmbeddingCache

    content = search_content("всё")
    cached = EmbeddingCache(content_hash=content.content_hash, canonical_text="все",
                            model="text-embedding-3-small", dimensions=1536, text_version=1)
    with pytest.raises(ValueError, match="canonical"):
        cached.assert_content(content)
