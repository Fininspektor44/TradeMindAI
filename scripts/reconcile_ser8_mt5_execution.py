#!/usr/bin/env python3
"""SER8 Automatic MT5 Execution Reconciliation CLI V1.

ONE production entrypoint that continuously (or once, via ``--once``)
inspects authoritative MT5 broker evidence -- the unified executor's own
read-only order/deal history exports -- and automatically transitions
SER8's own persisted PENDING execution legs to their true terminal state
(FILLED/CANCELLED/EXPIRED/REJECTED), for EVERY claim on the configured
demo account, without any manual command and without ever resending an
order.

This script:

  * discovers every persisted PENDING leg for ``--account`` generically,
    via ``SER8DemoOrderSendControl.list_pending_leg_ids_for_account`` --
    it never hard-codes a claim or ticket;
  * reads ``mt5_risk_orders_utc_<account>.csv`` and
    ``mt5_risk_deals_utc_<account>.csv`` from ``--mt5-export-dir`` (the
    unified executor's own new read-only exports -- see
    trademind.ser8_mt5_execution_reconciliation's own module docstring
    for the audit behind exactly these two files) and evaluates every
    pending leg against them via
    trademind.ser8_mt5_execution_reconciliation.run_reconciliation_cycle;
  * calls ONLY SER8DemoOrderSendControl.reconcile_pending_leg to persist
    any state change -- the SAME strict, already-tested primitive the
    manual recovery CLI uses -- and NEVER constructs a transport that
    could actually send anything (a bare FakeDemoOrderTransport with no
    result_factory is wired in, exactly like
    scripts/recover_ser8_pending_limit_legs.py);
  * NEVER infers a fill or cancellation merely because an order's ticket
    is absent from a given cycle's evidence -- absence is always
    "ambiguous, skip this leg this cycle", never a guess;
  * emits ONE concise, structured summary line per cycle (account,
    pending_legs_seen, pending_still_open, newly_filled,
    newly_cancelled_or_rejected, ambiguous, broker_sends=0, cycle_status)
    -- no per-field spam;
  * with ``--dry-run``, evaluates every leg and reports exactly what
    WOULD change, but calls no mutating method at all;
  * with ``--once``, runs exactly one cycle and exits (the mode the
    Windows Scheduled Task installer/wrapper in this same directory
    drives, mirroring scripts/run_v121_live_signal_watch.ps1's own
    proven "periodic external trigger + --once" architecture); without
    ``--once``, loops continuously every ``--poll-interval-seconds``;
  * guards against overlapping runs with a simple, portable lock file
    (defense in depth -- the primary single-instance guarantee for
    scheduled/automatic operation is the Windows Global Mutex in
    scripts/run_ser8_mt5_reconciliation.ps1, the SAME mechanism
    scripts/run_v121_live_signal_watch.ps1 already uses).
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trademind.discovery.hypothesis_registry import HypothesisRegistry  # noqa: E402
from trademind.ser8_mt5_demo_order_send import (  # noqa: E402
    FakeDemoOrderTransport,
    SER8DemoOrderSendControl,
)
from trademind.ser8_mt5_execution_reconciliation import (  # noqa: E402
    ReconciliationCycleResult,
    run_reconciliation_cycle,
)

DEFAULT_POLL_INTERVAL_SECONDS = 30.0


class _AlreadyRunningError(RuntimeError):
    """Raised when this script's own lock file already exists -- fails
    closed rather than risking two reconciliation processes racing on
    the same registry."""


class _LockFile:
    """A simple, portable exclusive-create lock file. Never "steals" a
    stale lock automatically (matching this whole system's fail-closed
    philosophy) -- if a prior process crashed without cleaning up, an
    operator must confirm no other instance is genuinely running and
    remove the lock file by hand."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._acquired = False

    def acquire(self) -> None:
        try:
            handle = self.path.open("x", encoding="utf-8")
        except FileExistsError as exc:
            raise _AlreadyRunningError(
                f"lock file already exists: {self.path} -- another reconciliation process may already be "
                "running; if you are certain none is, remove this file by hand and retry"
            ) from exc
        with handle:
            handle.write(f"pid={__import__('os').getpid()} started_at={datetime.now(timezone.utc).isoformat()}\n")
        self._acquired = True

    def release(self) -> None:
        if self._acquired and self.path.is_file():
            self.path.unlink()
        self._acquired = False

    def __enter__(self) -> "_LockFile":
        self.acquire()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()


def run_one_cycle(
    control: SER8DemoOrderSendControl, *, account: str, orders_csv: Path, deals_csv: Path, dry_run: bool
) -> ReconciliationCycleResult:
    result = run_reconciliation_cycle(
        control, account=account, orders_csv=orders_csv, deals_csv=deals_csv, dry_run=dry_run,
    )
    print(("DRY-RUN " if dry_run else "") + "SER8 MT5 RECONCILIATION CYCLE -- " + result.summary_line())
    for outcome in result.outcomes:
        if outcome.status in ("NEWLY_FILLED", "NEWLY_TERMINAL", "AMBIGUOUS"):
            print(f"  leg_id={outcome.leg_id} status={outcome.status} detail={outcome.detail}")
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, required=True, help="Path to the SER8 HypothesisRegistry SQLite file")
    parser.add_argument("--account", required=True, help="MT5 demo login/account id to reconcile")
    parser.add_argument(
        "--mt5-export-dir", type=Path, required=True,
        help="Directory containing mt5_risk_orders_utc_<account>.csv and mt5_risk_deals_utc_<account>.csv",
    )
    parser.add_argument("--dry-run", action="store_true", help="Evaluate and report only -- never mutate state")
    parser.add_argument("--once", action="store_true", help="Run exactly one cycle and exit")
    parser.add_argument(
        "--poll-interval-seconds", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"Continuous-mode cadence (default {DEFAULT_POLL_INTERVAL_SECONDS}s); ignored with --once",
    )
    parser.add_argument(
        "--lock-file", type=Path, default=None,
        help="Override the single-instance lock file path (default: <db>.reconcile.lock)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.poll_interval_seconds <= 0:
        print("--poll-interval-seconds must be positive", file=sys.stderr)
        return 2

    db_path = Path(args.db).expanduser()
    orders_csv = Path(args.mt5_export_dir).expanduser() / f"mt5_risk_orders_utc_{args.account}.csv"
    deals_csv = Path(args.mt5_export_dir).expanduser() / f"mt5_risk_deals_utc_{args.account}.csv"
    lock_path = Path(args.lock_file).expanduser() if args.lock_file else db_path.with_suffix(".reconcile.lock")

    registry = HypothesisRegistry(db_path)
    # A deliberately non-functional transport: reconciliation NEVER calls
    # transport.send() at all (run_reconciliation_cycle/evaluate_pending_leg
    # never receive a transport reference), but wiring in one that would
    # raise immediately if it somehow WERE invoked is a second,
    # independent guarantee this script can never place a real order --
    # exactly the same pattern scripts/recover_ser8_pending_limit_legs.py
    # already uses.
    transport = FakeDemoOrderTransport()
    control = SER8DemoOrderSendControl(registry=registry, transport=transport)

    try:
        with _LockFile(lock_path):
            if args.once:
                result = run_one_cycle(
                    control, account=args.account, orders_csv=orders_csv, deals_csv=deals_csv, dry_run=args.dry_run,
                )
                return 0 if result.cycle_status == "OK" else 1
            print(
                f"SER8 MT5 reconciliation starting -- account={args.account} "
                f"poll_interval_seconds={args.poll_interval_seconds}"
            )
            while True:
                run_one_cycle(
                    control, account=args.account, orders_csv=orders_csv, deals_csv=deals_csv, dry_run=args.dry_run,
                )
                time.sleep(args.poll_interval_seconds)
    except _AlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("SER8 MT5 reconciliation stopped (KeyboardInterrupt)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
