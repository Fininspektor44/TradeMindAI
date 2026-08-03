"""Read-only health checks for auto-discovered crypto volume streams."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from trademind.watchdog import WatchdogCheck, _age_minutes, _last_csv_row


def _safe_symbol(symbol: str) -> str:
    value = symbol
    for item in (".", "/", "\\", ":"):
        value = value.replace(item, "_")
    return value


def _resolve_manifest_path(manifest_path: Path) -> Path:
    """Prefer the universal ECN manifest for the legacy default path.

    An explicitly named manifest remains authoritative. The compatibility lookup never
    selects a Cent manifest.
    """
    if manifest_path.name != "crypto_manifest.csv":
        return manifest_path
    candidates = (
        manifest_path.parent / "ecn_manifest.csv",
        manifest_path.parent / "crypto_manifest_ecn.csv",
        manifest_path,
    )
    return next((candidate for candidate in candidates if candidate.is_file()), manifest_path)


def inspect_crypto_streams(
    manifest_path: Path,
    source_dir: Path,
    *,
    maximum_age: int,
    now: datetime,
) -> WatchdogCheck:
    """Check crypto symbols resolved by either the legacy or universal ECN exporter.

    A missing manifest means crypto monitoring has not been installed yet and is a WARN,
    not a failure of the existing market pipeline. Universal ECN manifests also contain
    non-crypto rows, which are ignored by their schema version.
    """
    manifest_path = _resolve_manifest_path(manifest_path)
    if not manifest_path.is_file():
        return WatchdogCheck(
            "Crypto MT5 streams",
            "WARN",
            f"crypto exporter not installed: {manifest_path}",
        )

    resolved: list[tuple[str, str]] = []
    missing: list[str] = []
    malformed = 0
    try:
        with manifest_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                schema_version = str(row.get("schema_version") or "").strip()
                if schema_version and schema_version != "1.7":
                    continue
                canonical = str(row.get("canonical_symbol") or "").strip().upper()
                broker = str(row.get("broker_symbol") or "").strip()
                status = str(row.get("status") or "").strip().upper()
                if not canonical or status not in {"RESOLVED", "MISSING"}:
                    malformed += 1
                    continue
                if status == "RESOLVED" and broker:
                    resolved.append((canonical, broker))
                else:
                    missing.append(canonical)
    except OSError as exc:
        return WatchdogCheck("Crypto MT5 streams", "ERROR", f"invalid manifest: {exc}")

    if malformed:
        return WatchdogCheck(
            "Crypto MT5 streams",
            "ERROR",
            f"manifest has {malformed} malformed rows",
        )
    if not resolved:
        detail = f"no broker crypto symbols resolved; missing={','.join(missing)}"
        return WatchdogCheck("Crypto MT5 streams", "WARN", detail)

    stale: list[str] = []
    bad: list[str] = []
    ages: list[float] = []
    rows_total = 0
    for canonical, broker in resolved:
        path = source_dir / f"volume_{_safe_symbol(canonical)}_M5.csv"
        if not path.is_file():
            bad.append(f"{canonical}=missing_file")
            continue
        try:
            row, rows = _last_csv_row(path)
            rows_total += rows
            if not row:
                bad.append(f"{canonical}=empty")
                continue
            if str(row.get("symbol") or "").strip().upper() != canonical:
                bad.append(f"{canonical}=wrong_symbol")
            if str(row.get("tick_copy_status") or "").strip().upper() != "OK":
                bad.append(f"{canonical}={row.get('tick_copy_status', 'missing')}")
            try:
                if int(row.get("tick_count", "0") or "0") <= 0:
                    bad.append(f"{canonical}=zero_ticks")
            except ValueError:
                bad.append(f"{canonical}=invalid_ticks")
            age = _age_minutes(path, now)
            ages.append(age)
            if age > maximum_age:
                stale.append(f"{canonical}={age:.1f}m")
        except (OSError, TypeError, ValueError) as exc:
            bad.append(f"{canonical}={exc}")

    status = "OK"
    messages = [f"resolved {len(resolved)} crypto symbols"]
    if missing:
        messages.append("unavailable: " + ",".join(missing))
    if bad:
        status = "ERROR"
        messages.append("bad: " + ",".join(bad))
    if stale:
        status = "ERROR"
        messages.append("stale 24/7 streams: " + ",".join(stale))
    return WatchdogCheck(
        "Crypto MT5 streams",
        status,
        "; ".join(messages),
        age_minutes=max(ages) if ages else None,
        details={
            "manifest": str(manifest_path),
            "resolved": len(resolved),
            "unavailable": len(missing),
            "rows": rows_total,
            "mapping": {canonical: broker for canonical, broker in resolved},
        },
    )
