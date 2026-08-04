from __future__ import annotations

from trademind.live_signal_page import render_page


def test_page_has_live_feed_filters_detail_drawer_and_safety_badges() -> None:
    page = render_page()

    assert "TradeMind Live Signal Console" in page
    assert "READ ONLY" in page
    assert "ORDERS OFF" in page
    assert 'id="feed"' in page
    assert 'id="drawer"' in page
    assert 'id="source"' in page
    assert 'id="symbol"' in page
    assert 'id="action"' in page
    assert 'id="status"' in page
    assert 'id="score"' in page
    assert "/api/signals" in page
    assert "/api/summary" in page
    assert "setInterval(refresh,5000)" in page
    assert "encodeURIComponent(id)" in page
    assert "textContent" in page
    assert "innerHTML" not in page
