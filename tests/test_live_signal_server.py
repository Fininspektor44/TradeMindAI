from __future__ import annotations

import csv
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from trademind.live_signal_repository import LiveSignalRepository
from trademind.live_signal_server import LiveSignalService, SignalQuery, handler_factory


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = list(dict.fromkeys(key for row in rows for key in row))
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _row(event_id: str, symbol: str, action: str, score: int, outcome: str = "") -> dict[str, object]:
    return {
        "event_id": event_id,
        "signal_key": event_id.replace("event", "signal"),
        "signal_time": "2026-08-04T08:00:00+00:00",
        "source": "FX_RESEARCH",
        "source_id": event_id.replace("event", "obs"),
        "symbol": symbol,
        "timeframe": "M5",
        "action": action,
        "scenario": "BASE_SIGNAL",
        "scenario_family": "BASE",
        "quality_score": str(score),
        "entry_price": "100",
        "stop_price": "99",
        "target_price": "102",
        "rr": "2",
        "outcome": outcome,
        "result": "2" if outcome == "WIN" else "-1" if outcome == "LOSS" else "",
        "completed": "1" if outcome else "0",
    }


def _json_get(url: str) -> tuple[int, dict[str, object]]:
    with urlopen(url, timeout=3) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_service_health_summary_filters_and_detail(tmp_path: Path) -> None:
    signals = tmp_path / "signals.csv"
    _write_csv(
        signals,
        [
            _row("event-1", "XAUUSD", "BUY", 85, "WIN"),
            _row("event-2", "EURUSD", "SELL", 65, "LOSS"),
        ],
    )
    service = LiveSignalService(LiveSignalRepository(unified_path=signals))
    snapshot = service.snapshot()

    health = service.health(snapshot)
    summary = service.summary(snapshot)
    filtered = service.signals(SignalQuery(actions=("BUY",), min_score=80), snapshot)
    detail = service.detail("event-2", snapshot)

    assert health["state"] == "OK"
    assert health["read_only"] is True
    assert health["orders_enabled"] is False
    assert summary["by_status"] == {"LOSS": 1, "WIN": 1}
    assert filtered["count"] == 1
    assert filtered["signals"][0]["event_id"] == "event-1"
    assert detail["symbol"] == "EURUSD"


def test_http_api_is_live_read_only_and_reflects_file_updates(tmp_path: Path) -> None:
    signals = tmp_path / "signals.csv"
    first = _row("event-1", "XAUUSD", "BUY", 85, "WIN")
    _write_csv(signals, [first])
    service = LiveSignalService(LiveSignalRepository(unified_path=signals))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, payload = _json_get(f"{base}/api/signals?action=BUY&min_score=80")
        assert status == 200
        assert payload["count"] == 1

        detail_id = quote("event-1", safe="")
        _, detail = _json_get(f"{base}/api/signals/{detail_id}")
        assert detail["target_price"] == 102.0

        second = _row("event-2", "EURUSD", "SELL", 75, "")
        _write_csv(signals, [first, second])
        _, refreshed = _json_get(f"{base}/api/summary")
        assert refreshed["total"] == 2
        assert refreshed["by_symbol"] == {"EURUSD": 1, "XAUUSD": 1}

        request = Request(f"{base}/api/signals", method="POST", data=b"{}")
        try:
            urlopen(request, timeout=3)
        except HTTPError as exc:
            assert exc.code == 405
            assert exc.headers["Allow"] == "GET, HEAD"
        else:
            raise AssertionError("POST must be rejected")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_bad_query_and_missing_detail_return_json_errors(tmp_path: Path) -> None:
    signals = tmp_path / "signals.csv"
    _write_csv(signals, [_row("event-1", "XAUUSD", "BUY", 85)])
    service = LiveSignalService(LiveSignalRepository(unified_path=signals))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for path, expected in (
            ("/api/signals?limit=banana", 400),
            ("/api/signals/missing", 404),
        ):
            try:
                urlopen(f"{base}{path}", timeout=3)
            except HTTPError as exc:
                payload = json.loads(exc.read().decode("utf-8"))
                assert exc.code == expected
                assert "error" in payload
            else:
                raise AssertionError(f"{path} must fail")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_server_module_contains_get_endpoints_and_no_trading_code() -> None:
    module = Path(__file__).resolve().parents[1] / "src" / "trademind" / "live_signal_server.py"
    text = module.read_text(encoding="utf-8")
    for route in ("/api/health", "/api/signals", "/api/summary"):
        assert route in text
    for method in ("do_POST", "do_PUT", "do_PATCH", "do_DELETE"):
        assert method in text
    forbidden = (
        "OrderSend",
        "OrderSendAsync",
        "CTrade",
        "trade.Buy",
        "trade.Sell",
        "PositionClose",
        "place_order",
        "create_order",
    )
    assert not any(token in text for token in forbidden)
    assert "OrdersEnabled=False" in text
