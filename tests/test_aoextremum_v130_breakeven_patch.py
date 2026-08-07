from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCHER_PATH = ROOT / "scripts" / "patch_aoextremum_v130_breakeven.py"
SOURCE_PATH = ROOT / "mt5" / "experts" / "AOExtremum_Original11_AllPairs_EXACT_v1_30_PROP_RISK.mq5"

spec = importlib.util.spec_from_file_location("ao_be_patcher", PATCHER_PATH)
assert spec and spec.loader
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)


def patched_source() -> str:
    return patcher.patch_text(SOURCE_PATH.read_text(encoding="utf-8"))


def test_patch_is_idempotent() -> None:
    once = patched_source()
    twice = patcher.patch_text(once)
    assert once == twice


def test_break_even_inputs_and_version_are_added() -> None:
    text = patched_source()
    assert '#property version   "1.301"' in text
    assert "input bool   BreakEvenEnabled               = true;" in text
    assert "input double BreakEvenTriggerTPFraction     = 0.60;" in text
    assert "input int    BreakEvenLockPoints            = 10;" in text


def test_break_even_is_basket_level_and_tp_fraction_based() -> None:
    text = patched_source()
    assert "BasketBreakEvenStopPrice" in text
    assert "basket.average_price + trigger_distance" in text
    assert "basket.average_price - trigger_distance" in text
    assert "cfg[index].take_profit_points * point * BreakEvenTriggerTPFraction" in text


def test_broker_stop_distance_is_respected() -> None:
    text = patched_source()
    assert "double minimum_distance = BrokerMinimumStopDistance(symbol);" in text
    assert "double highest_valid = tick.bid - minimum_distance;" in text
    assert "double lowest_valid = tick.ask + minimum_distance;" in text


def test_break_even_never_moves_back_into_loss_after_latching() -> None:
    text = patched_source()
    assert "PreserveBreakEvenStopForTicket" in text
    assert "current_sl + tick_size * 0.5 >= basket.average_price" in text
    assert "current_sl - tick_size * 0.5 <= basket.average_price" in text
    assert "return TighterStopPrice(basket.type, desired_sl, current_sl);" in text


def test_physical_stop_and_tp_pipeline_are_preserved() -> None:
    text = patched_source()
    assert "double physical_sl = BasketPhysicalStopPrice(index, basket);" in text
    assert "double break_even_sl = BasketBreakEvenStopPrice(index, basket);" in text
    assert "double sl = TighterStopPrice(basket.type, physical_sl, break_even_sl);" in text
    assert "tp = BasketTakeProfitPrice(index, basket);" in text
    assert "ApplyProtectionToTicket(ticket, symbol, ticket_sl, tp)" in text


def test_invalid_break_even_inputs_fail_initialization() -> None:
    text = patched_source()
    assert "BreakEvenTriggerTPFraction <= 0.0 ||" in text
    assert "BreakEvenTriggerTPFraction >= 1.0 ||" in text
    assert "BreakEvenLockPoints < 0 ||" in text
