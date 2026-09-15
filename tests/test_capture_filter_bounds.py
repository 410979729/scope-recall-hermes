"""Capture hygiene must not spend a quadratic scan on a long source token."""
import time

import pytest

from scope_recall.capture_filters import sanitize_source_capture_text


def test_long_cjk_source_is_preserved_within_capture_budget():
    source = "中文内容" * 16500
    started = time.monotonic()
    assert sanitize_source_capture_text(source) == source
    assert time.monotonic() - started < 2.0


@pytest.mark.parametrize(("source", "expected"), [
    ("前文 缓存/image_cache/img_TEST-01.png 后文", "前文  后文"),
    ("前文 X:\\缓存\\image_cache\\img_TEST-01.PNG 后文", "前文  后文"),
    ("前文]缓存/image_cache/img_TEST-01.webp 后文", "前文] 后文"),
    ("前文\n/image_cache/img_TEST-01.gif\n后文", "前文\n\n后文"),
    ("缓存/image_cache/img_TEST-01.png/又一项/image_cache/img_TEST-02.png", ""),
    ("前文 缓存/image_cache/not_an_image.png 后文", "前文 缓存/image_cache/not_an_image.png 后文"),
])
def test_cache_token_filter_keeps_existing_removal_boundaries(source, expected):
    assert sanitize_source_capture_text(source) == expected
