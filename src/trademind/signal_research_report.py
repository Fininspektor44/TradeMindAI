"""Research dashboard for TradeMind v1.16 shadow signal evidence."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind.signal_evidence import OutcomeObservation, aggregate_evidence, load_outcomes, similarity_key
from trademind.signal_intelligence import PublicationPolicy, SignalCandidate, candidate_from_dict, quality_score
from trademind.signal_shadow import load_candidates

REPORT_VERSION = "1.16.0"
REPORT_FIELDS = (
    "schema_version",
    "setup_key",
    "setup_family",
    "symbol",
    "timeframe",
    "action",
    "candidates",
    "completed",
    "wins",
    "losses",
    "flats",
    "raw_win_rate",
    "smoothed_win_rate",
    "wilson_lower_95",
    "net_r",
    "average_win_r",
    "average_loss_r",
    "profit_factor_r",
    "max_drawdown_r",
    "average_quality_score",
    "expected_value_r",
    "drift_ratio",
    "research_status",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
    )


def _atomic_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(REPORT_FIELDS), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _expected_value(evidence: Any) -> float:
    return (
        evidence.wilson_lower_95 * evidence.average_win_r
        - (1.0 - evidence.wilson_lower_95) * abs(evidence.average_loss_r)
    )


def _status(evidence: Any, policy: PublicationPolicy) -> str:
    if evidence.completed < policy.minimum_completed:
        return "SHADOW_INSUFFICIENT_SAMPLE"
    if evidence.drift_ratio is not None and evidence.drift_ratio < policy.minimum_drift_ratio:
        return "REJECT_EDGE_DEGRADING"
    if evidence.wilson_lower_95 < policy.minimum_wilson_lower:
        return "REJECT_LOW_CONSERVATIVE_RELIABILITY"
    if evidence.profit_factor_r < policy.minimum_profit_factor_r:
        return "REJECT_LOW_PROFIT_FACTOR"
    if _expected_value(evidence) < policy.minimum_expected_value_r:
        return "REJECT_NEGATIVE_EXPECTANCY"
    return "ELIGIBLE_FOR_CANDIDATE_GATE"


def build_report_rows(
    candidates: Sequence[SignalCandidate],
    outcomes: Sequence[OutcomeObservation],
    *,
    policy: PublicationPolicy | None = None,
    captured_at: datetime | None = None,
) -> list[dict[str, Any]]:
    rules = policy or PublicationPolicy()
    now = (captured_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    grouped: dict[str, list[SignalCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[similarity_key(candidate)].append(candidate)

    rows: list[dict[str, Any]] = []
    for key, group in sorted(grouped.items()):
        representative = group[-1]
        evidence = aggregate_evidence(
            representative,
            outcomes,
            captured_at=now,
        )
        scores = [quality_score(candidate, rules) for candidate in group]
        net_r = sum(
            outcome.net_r for outcome in outcomes if outcome.setup_key == key
        )
        pf = evidence.profit_factor_r
        rows.append(
            {
                "schema_version": REPORT_VERSION,
                "setup_key": key,
                "setup_family": representative.setup_family,
                "symbol": representative.symbol,
                "timeframe": representative.timeframe,
                "action": representative.plan.action,
                "candidates": len(group),
                "completed": evidence.completed,
                "wins": evidence.wins,
                "losses": evidence.losses,
                "flats": evidence.flats,
                "raw_win_rate": round(evidence.raw_win_rate, 6),
                "smoothed_win_rate": round(evidence.smoothed_win_rate, 6),
                "wilson_lower_95": round(evidence.wilson_lower_95, 6),
                "net_r": round(net_r, 6),
                "average_win_r": round(evidence.average_win_r, 6),
                "average_loss_r": round(evidence.average_loss_r, 6),
                "profit_factor_r": round(pf, 6) if math.isfinite(pf) else "inf",
                "max_drawdown_r": round(evidence.max_drawdown_r, 6),
                "average_quality_score": round(sum(scores) / len(scores), 3),
                "expected_value_r": round(_expected_value(evidence), 6),
                "drift_ratio": (
                    round(evidence.drift_ratio, 6)
                    if evidence.drift_ratio is not None
                    else ""
                ),
                "research_status": _status(evidence, rules),
            }
        )
    rows.sort(
        key=lambda row: (
            row["research_status"] != "ELIGIBLE_FOR_CANDIDATE_GATE",
            -int(row["completed"]),
            -_float(row["expected_value_r"]),
        )
    )
    return rows


def _render_dashboard(
    rows: Sequence[Mapping[str, Any]],
    *,
    candidates: int,
    outcomes: int,
    captured_at: datetime,
) -> str:
    eligible = sum(row["research_status"] == "ELIGIBLE_FOR_CANDIDATE_GATE" for row in rows)
    table_rows = "".join(
        "<tr>"
        f"<td>{html.escape(_text(row['setup_family']))}</td>"
        f"<td>{html.escape(_text(row['symbol']))} {html.escape(_text(row['action']))}</td>"
        f"<td>{row['completed']}</td>"
        f"<td>{100 * _float(row['raw_win_rate']):.1f}%</td>"
        f"<td>{100 * _float(row['wilson_lower_95']):.1f}%</td>"
        f"<td>{_float(row['net_r']):.2f}R</td>"
        f"<td>{html.escape(_text(row['profit_factor_r']))}</td>"
        f"<td>{_float(row['max_drawdown_r']):.2f}R</td>"
        f"<td>{_float(row['average_quality_score']):.1f}</td>"
        f"<td>{_float(row['expected_value_r']):.3f}R</td>"
        f"<td>{html.escape(_text(row['research_status']))}</td>"
        "</tr>"
        for row in rows
    )
    return f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>TradeMind Signal Research v1.16</title><style>
:root{{color-scheme:dark}}body{{margin:28px;background:#06121c;color:#e8f5ff;
font-family:Arial,sans-serif}}h1{{font-size:38px}}.good{{color:#39e4a8}}.warn{{color:#ffd36d}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px}}
.card{{background:#0b2638;border:1px solid #1e5a78;border-radius:14px;padding:16px}}
.card b{{font-size:28px}}table{{width:100%;border-collapse:collapse;margin-top:24px}}
th,td{{padding:10px;border-bottom:1px solid #194156;text-align:left}}th{{background:#0e3046}}
.note{{background:#102638;border-left:4px solid #ffd36d;padding:13px 16px;margin:18px 0}}
</style></head><body><h1>Signal Research v1.16</h1>
<p class='good'>Теневой режим. Ордера и публикация выключены.</p>
<p>Обновлено: {html.escape(captured_at.isoformat())}</p>
<div class='note'>Статус ELIGIBLE_FOR_CANDIDATE_GATE не означает готовый публичный сигнал.
Он означает только, что историческая группа допускает проверку конкретного свежего кандидата.</div>
<section class='cards'>
<div class='card'><span>Кандидатов</span><br><b>{candidates}</b></div>
<div class='card'><span>Завершённых исходов</span><br><b>{outcomes}</b></div>
<div class='card'><span>Групп сетапов</span><br><b>{len(rows)}</b></div>
<div class='card'><span>Допущено к gate</span><br><b>{eligible}</b></div>
</section>
<table><thead><tr><th>Сетап</th><th>Рынок</th><th>N</th><th>Win</th><th>95% low</th>
<th>Net</th><th>PF</th><th>Max DD</th><th>Quality</th><th>EV</th><th>Статус</th></tr></thead>
<tbody>{table_rows}</tbody></table></body></html>"""


def run_report(
    candidates_path: Path,
    outcomes_path: Path,
    output_dir: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidates = load_candidates(candidates_path)
    outcomes = load_outcomes(outcomes_path)
    rows = build_report_rows(candidates, outcomes, captured_at=captured_at)
    eligible = sum(row["research_status"] == "ELIGIBLE_FOR_CANDIDATE_GATE" for row in rows)
    status = {
        "schema_version": REPORT_VERSION,
        "state": "OK",
        "updated_at": captured_at.isoformat(),
        "candidates": len(candidates),
        "outcomes": len(outcomes),
        "setup_groups": len(rows),
        "eligible_groups": eligible,
        "orders_enabled": False,
        "publication_enabled": False,
        "dashboard": str(output_dir / "dashboard" / "index.html"),
    }
    _atomic_csv(output_dir / "setup_evidence.csv", rows)
    _atomic_json(output_dir / "status.json", status)
    _atomic_text(
        output_dir / "dashboard" / "index.html",
        _render_dashboard(
            rows,
            candidates=len(candidates),
            outcomes=len(outcomes),
            captured_at=captured_at,
        ),
    )
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build TradeMind v1.16 shadow signal research dashboard"
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=Path("data/signal_intelligence_v1_16/candidates.jsonl"),
    )
    parser.add_argument(
        "--outcomes",
        type=Path,
        default=Path("data/signal_intelligence_v1_16/outcomes.jsonl"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/signal_intelligence_v1_16/report"),
    )
    parser.add_argument("--open-dashboard", action="store_true")
    args = parser.parse_args(argv)
    try:
        status = run_report(
            args.candidates.expanduser().resolve(),
            args.outcomes.expanduser().resolve(),
            args.output_dir.expanduser().resolve(),
        )
    except (OSError, ValueError, csv.Error, json.JSONDecodeError) as exc:
        print(f"Signal research report failed: {exc}")
        return 1

    print("TradeMind v1.16 Signal Research Report")
    print("Shadow only. Orders OFF. Publication OFF.")
    print(
        f"Candidates/outcomes/groups: {status['candidates']}/"
        f"{status['outcomes']}/{status['setup_groups']}"
    )
    print(f"Groups eligible for candidate gate: {status['eligible_groups']}")
    print(f"Dashboard: {status['dashboard']}")
    if args.open_dashboard:
        if hasattr(os, "startfile"):
            os.startfile(status["dashboard"])  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
