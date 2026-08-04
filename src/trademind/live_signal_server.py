"""Local read-only JSON service for the TradeMind live signal console."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlparse

from trademind.live_signal_ideas import collapse_signal_ideas
from trademind.live_signal_page import render_page
from trademind.live_signal_repository import LiveSignalRepository, RepositorySnapshot

READ_ONLY_METHODS = {"GET", "HEAD"}


@dataclass(frozen=True, slots=True)
class SignalQuery:
    sources: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    scenarios: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    min_score: int = 0
    limit: int = 200

    @classmethod
    def from_params(cls, params: Mapping[str, list[str]]) -> SignalQuery:
        def values(name: str) -> tuple[str, ...]:
            output: list[str] = []
            for raw in params.get(name, []):
                output.extend(item.strip() for item in raw.split(",") if item.strip())
            return tuple(output)

        try:
            min_score = max(0, min(100, int(params.get("min_score", ["0"])[0])))
            limit = max(1, min(2000, int(params.get("limit", ["200"])[0])))
        except (TypeError, ValueError) as exc:
            raise ValueError("min_score and limit must be integers") from exc
        return cls(
            sources=values("source"),
            symbols=values("symbol"),
            actions=values("action"),
            scenarios=values("scenario"),
            statuses=values("status"),
            min_score=min_score,
            limit=limit,
        )


class LiveSignalService:
    """Build API payloads from a fresh repository snapshot on every request."""

    def __init__(self, repository: LiveSignalRepository) -> None:
        self.repository = repository

    def snapshot(self) -> RepositorySnapshot:
        return collapse_signal_ideas(self.repository.load())

    def health(self, snapshot: RepositorySnapshot | None = None) -> dict[str, object]:
        current = snapshot or self.snapshot()
        stale = sum(record.stale for record in current.records)
        state = "WARN" if current.errors or stale else "OK"
        if not current.records and not current.errors:
            state = "EMPTY"
        return {
            "state": state,
            "read_only": True,
            "orders_enabled": False,
            "loaded_at": current.loaded_at.isoformat(),
            "signals": len(current.records),
            "stale_signals": stale,
            "errors": list(current.errors),
        }

    def signals(
        self,
        query: SignalQuery,
        snapshot: RepositorySnapshot | None = None,
    ) -> dict[str, object]:
        current = snapshot or self.snapshot()
        records = self.repository.list_records(
            current,
            sources=query.sources,
            symbols=query.symbols,
            actions=query.actions,
            scenarios=query.scenarios,
            statuses=query.statuses,
            min_score=query.min_score,
            limit=query.limit,
        )
        return {
            "loaded_at": current.loaded_at.isoformat(),
            "count": len(records),
            "signals": [record.as_dict() for record in records],
            "errors": list(current.errors),
        }

    def detail(
        self,
        event_id: str,
        snapshot: RepositorySnapshot | None = None,
    ) -> dict[str, object] | None:
        current = snapshot or self.snapshot()
        record = self.repository.get(current, event_id)
        if record is None:
            return None
        return record.as_dict()

    def summary(self, snapshot: RepositorySnapshot | None = None) -> dict[str, object]:
        current = snapshot or self.snapshot()
        return {
            "loaded_at": current.loaded_at.isoformat(),
            "total": len(current.records),
            "by_source": dict(Counter(record.source for record in current.records)),
            "by_status": dict(Counter(record.status for record in current.records)),
            "by_symbol": dict(Counter(record.symbol for record in current.records)),
            "stale": sum(record.stale for record in current.records),
            "errors": list(current.errors),
        }


def handler_factory(service: LiveSignalService) -> type[BaseHTTPRequestHandler]:
    class LiveSignalHandler(BaseHTTPRequestHandler):
        server_version = "TradeMindLiveSignal/1.12"

        def _write_body(self, body: bytes) -> None:
            if self.command == "HEAD":
                return
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return

        def _send_json(self, status: HTTPStatus, payload: object) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self._write_body(body)

        def _send_html(self, status: HTTPStatus, content: str) -> None:
            body = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self._write_body(body)

        def _method_not_allowed(self) -> None:
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", ", ".join(sorted(READ_ONLY_METHODS)))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    self._send_html(HTTPStatus.OK, render_page())
                    return
                if parsed.path == "/api/health":
                    self._send_json(HTTPStatus.OK, service.health())
                    return
                if parsed.path == "/api/summary":
                    self._send_json(HTTPStatus.OK, service.summary())
                    return
                if parsed.path == "/api/signals":
                    query = SignalQuery.from_params(parse_qs(parsed.query))
                    self._send_json(HTTPStatus.OK, service.signals(query))
                    return
                prefix = "/api/signals/"
                if parsed.path.startswith(prefix):
                    event_id = unquote(parsed.path[len(prefix) :])
                    payload = service.detail(event_id)
                    if payload is None:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": "signal not found"})
                    else:
                        self._send_json(HTTPStatus.OK, payload)
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "route not found"})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_POST(self) -> None:  # noqa: N802
            self._method_not_allowed()

        def do_PUT(self) -> None:  # noqa: N802
            self._method_not_allowed()

        def do_PATCH(self) -> None:  # noqa: N802
            self._method_not_allowed()

        def do_DELETE(self) -> None:  # noqa: N802
            self._method_not_allowed()

    return LiveSignalHandler


def build_repository(args: argparse.Namespace) -> LiveSignalRepository:
    status_paths = {}
    if args.mt5_status:
        status_paths["MT5"] = args.mt5_status
    if args.bybit_status:
        status_paths["BYBIT"] = args.bybit_status
    return LiveSignalRepository(
        unified_path=args.unified_signals,
        bybit_paths=args.bybit_signals,
        status_paths=status_paths,
        stale_after_seconds=args.stale_after_seconds,
        new_window_seconds=args.new_window_seconds,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind v1.12 read-only live signal API")
    parser.add_argument(
        "--unified-signals",
        type=Path,
        default=Path(os.getenv("TRADEMIND_UNIFIED_SIGNALS", "data/unified/signals.csv")),
    )
    parser.add_argument(
        "--bybit-signals",
        type=Path,
        action="append",
        default=[],
        help="Repeat for CONTROL, BUY_ONLY and STRICT_SELL journals.",
    )
    parser.add_argument("--mt5-status", type=Path)
    parser.add_argument("--bybit-status", type=Path)
    parser.add_argument("--stale-after-seconds", type=int, default=600)
    parser.add_argument("--new-window-seconds", type=int, default=600)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    repository = build_repository(args)
    service = LiveSignalService(repository)
    server = ThreadingHTTPServer((args.host, args.port), handler_factory(service))
    print(f"TradeMind live signal API: http://{args.host}:{args.port}")
    print("Read-only. OrdersEnabled=False.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
