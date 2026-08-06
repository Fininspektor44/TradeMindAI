"""TradeMind Product UI v1.26.2 with collision-safe price labels."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind import product_ui_v126 as previous

VERSION = "1.26.2"
MIN_TAG_GAP = 24.0
TAG_TOP = 23.0
TAG_BOTTOM = 179.0


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _spread_centers(
    centers: Sequence[tuple[str, float]],
    *,
    minimum_gap: float = MIN_TAG_GAP,
    top: float = TAG_TOP,
    bottom: float = TAG_BOTTOM,
) -> dict[str, float]:
    """Spread label centres while preserving their vertical price order."""
    if not centers:
        return {}
    ordered = sorted(centers, key=lambda item: item[1])
    adjusted = [value for _, value in ordered]

    for index in range(1, len(adjusted)):
        adjusted[index] = max(adjusted[index], adjusted[index - 1] + minimum_gap)

    overflow = adjusted[-1] - bottom
    if overflow > 0:
        adjusted = [value - overflow for value in adjusted]

    for index in range(len(adjusted) - 2, -1, -1):
        adjusted[index] = min(adjusted[index], adjusted[index + 1] - minimum_gap)

    underflow = top - adjusted[0]
    if underflow > 0:
        adjusted = [value + underflow for value in adjusted]

    return {
        css: max(top, min(bottom, value))
        for (css, _), value in zip(ordered, adjusted, strict=True)
    }


def _price_scale_svg(candidate: Mapping[str, Any]) -> str:
    svg = previous._price_scale_svg(candidate)
    if "price-scale-chart" not in svg:
        return svg

    line_pattern = re.compile(
        r"<line class='trade-level (?P<css>target|entry|stop)'[^>]*"
        r"y1='(?P<y>[0-9.]+)' y2='[0-9.]+'/>"
    )
    centres = [
        (match.group("css"), float(match.group("y")))
        for match in line_pattern.finditer(svg)
    ]
    adjusted = _spread_centers(centres)
    if not adjusted:
        return svg

    for css, line_y in centres:
        tag_y = adjusted.get(css, line_y)
        if math.isclose(tag_y, line_y, abs_tol=0.25):
            continue

        line_token = re.compile(
            rf"(<line class='trade-level {css}'[^>]*"
            rf"y1='{line_y:.2f}' y2='{line_y:.2f}'/>)"
        )
        connector = (
            rf"<path class='trade-tag-connector {css}' "
            rf"d='M 488 {line_y:.2f} L 495 {tag_y:.2f}'/>"
        )
        svg = line_token.sub(
            lambda match, connector=connector: match.group(1) + connector,
            svg,
            count=1,
        )
        svg = re.sub(
            rf"(<rect class='trade-tag {css}' x='[^']+' )y='[^']+'",
            rf"\1y='{tag_y - 10:.2f}'",
            svg,
            count=1,
        )
        svg = re.sub(
            rf"(<text class='trade-tag-text {css}' x='[^']+' )y='[^']+'",
            rf"\1y='{tag_y + 4:.2f}'",
            svg,
            count=1,
        )
    return svg


LABEL_CSS = r"""
<style>
.price-scale-chart .trade-tag-connector{fill:none;stroke-width:1.25;opacity:.95}
.price-scale-chart .trade-tag-connector.entry{stroke:#a18dff}
.price-scale-chart .trade-tag-connector.stop{stroke:#ff6678}
.price-scale-chart .trade-tag-connector.target{stroke:#2ed6a6}
.price-scale-chart .trade-tag{stroke:#ffffff24;stroke-width:.7}
</style>
"""


def render(data: Mapping[str, Any]) -> str:
    original_scale = previous._price_scale_svg
    try:
        previous._price_scale_svg = _price_scale_svg
        page = previous.render(data)
    finally:
        previous._price_scale_svg = original_scale

    page = page.replace("TradeMind Product UI v1.26.1", "TradeMind Product UI v1.26.2")
    page = page.replace("</head>", LABEL_CSS + "</head>", 1)
    return page


def run_product_ui(
    runtime_root: Path,
    crypto_root: Path,
    bars_path: Path,
    *,
    fx_limit: int = 24,
    crypto_limit: int = 24,
    candle_limit: int = 48,
) -> tuple[Path, Mapping[str, Any]]:
    original_render = previous.render
    original_version = previous.VERSION
    try:
        previous.render = render
        previous.VERSION = VERSION
        index, payload = previous.run_product_ui(
            runtime_root,
            crypto_root,
            bars_path,
            fx_limit=fx_limit,
            crypto_limit=crypto_limit,
            candle_limit=candle_limit,
        )
    finally:
        previous.render = original_render
        previous.VERSION = original_version

    status_path = runtime_root.expanduser().resolve() / "product" / "status.json"
    status = dict(previous.base.read_json(status_path))
    status.update(
        {
            "schema_version": VERSION,
            "crypto_price_tag_collision_avoidance": True,
            "read_only": True,
        }
    )
    previous.base.atomic_write(
        status_path,
        json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True),
    )
    return index, payload


def safety_contract() -> Mapping[str, Any]:
    return previous.safety_contract()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TradeMind Product UI v1.26.2 collision-safe price labels"
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=Path("data/live_signal_runtime_v1"),
    )
    parser.add_argument(
        "--crypto-root",
        type=Path,
        default=Path("data/crypto_signal_intelligence_v1_26"),
    )
    parser.add_argument(
        "--bybit-bars",
        type=Path,
        default=Path("data/bybit_v1_9/bybit_bars.csv"),
    )
    parser.add_argument("--fx-limit", type=int, default=24)
    parser.add_argument("--crypto-limit", type=int, default=24)
    parser.add_argument("--candle-limit", type=int, default=48)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args(argv)

    try:
        index, payload = run_product_ui(
            args.runtime_root,
            args.crypto_root,
            args.bybit_bars,
            fx_limit=args.fx_limit,
            crypto_limit=args.crypto_limit,
            candle_limit=args.candle_limit,
        )
    except (OSError, ValueError, TypeError, csv.Error, json.JSONDecodeError) as exc:
        print(f"TradeMind Product UI v1.26.2 failed: {exc}")
        return 1

    summary = previous.base._mapping(payload.get("summary"))
    print("TradeMind Product UI v1.26.2")
    print("Collision-safe price labels. Read-only. Orders OFF. Publication OFF.")
    print(f"Forex displayed: {previous.base.integer(summary.get('forex_displayed'))}")
    print(f"Crypto displayed: {previous.base.integer(summary.get('crypto_displayed'))}")
    print("Crypto position sizing: NOT CALCULATED")
    print(f"Product UI: {index}")
    if args.open and hasattr(os, "startfile"):
        os.startfile(index)  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
