from __future__ import annotations

import argparse
from pathlib import Path

SOURCE_NAME = "AOExtremum_Original11_AllPairs_EXACT_v1_30_PROP_RISK.mq5"
PATCH_SENTINEL = "input bool   BreakEvenEnabled"


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one anchor, found {count}")
    return text.replace(old, new, 1)


def patch_text(source: str) -> str:
    if PATCH_SENTINEL in source:
        return source

    source = _replace_once(
        source,
        '#property version   "1.300"',
        '#property version   "1.301"',
        "version",
    )
    source = _replace_once(
        source,
        '#property description "AO Extremum v1.30 prop-risk: basket loss cap, staged daily blocking, robust stops and news TP recovery"',
        '#property description "AO Extremum v1.30.1 prop-risk: one-way basket break-even, robust stops and news TP recovery"',
        "description",
    )

    input_anchor = "input bool            CloseIfProtectionMissing     = true;"
    input_block = """input bool            CloseIfProtectionMissing     = true;

// One-way basket break-even. The trigger is measured as a fraction of the
// configured basket TP distance. Once the live SL reaches basket average
// price or better, reconciliation never moves it back into loss.
input bool   BreakEvenEnabled               = true;
input double BreakEvenTriggerTPFraction     = 0.60;
input int    BreakEvenLockPoints            = 10;"""
    source = _replace_once(source, input_anchor, input_block, "break-even inputs")

    helper_anchor = """bool PricesEqualEnough(const string symbol,
                       const double left,
                       const double right)
{"""
    helper_block = """double TighterStopPrice(const ENUM_POSITION_TYPE type,
                        const double first,
                        const double second)
{
   if(first <= 0.0)
      return second;
   if(second <= 0.0)
      return first;

   if(type == POSITION_TYPE_BUY)
      return MathMax(first, second);

   return MathMin(first, second);
}

double BasketBreakEvenStopPrice(const int index,
                                const BasketInfo &basket)
{
   if(!BreakEvenEnabled || cfg[index].take_profit_points <= 0)
      return 0.0;

   string symbol = rt[index].symbol;
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   if(point <= 0.0)
      return 0.0;

   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
      return 0.0;

   double trigger_distance =
      cfg[index].take_profit_points * point * BreakEvenTriggerTPFraction;
   double lock_distance = MathMax(BreakEvenLockPoints, 0) * point;
   double minimum_distance = BrokerMinimumStopDistance(symbol);
   double tick_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick_size <= 0.0)
      tick_size = point;

   if(basket.type == POSITION_TYPE_BUY)
   {
      if(tick.bid < basket.average_price + trigger_distance)
         return 0.0;

      double highest_valid = tick.bid - minimum_distance;
      double candidate = MathMin(basket.average_price + lock_distance,
                                 highest_valid);
      double normalized = NormalizePriceToTick(symbol, candidate, false);

      if(normalized + tick_size * 0.5 < basket.average_price)
         return 0.0;

      return normalized;
   }

   if(tick.ask > basket.average_price - trigger_distance)
      return 0.0;

   double lowest_valid = tick.ask + minimum_distance;
   double candidate = MathMax(basket.average_price - lock_distance,
                              lowest_valid);
   double normalized = NormalizePriceToTick(symbol, candidate, true);

   if(normalized - tick_size * 0.5 > basket.average_price)
      return 0.0;

   return normalized;
}

double PreserveBreakEvenStopForTicket(const ulong ticket,
                                      const string symbol,
                                      const BasketInfo &basket,
                                      const double desired_sl)
{
   if(!BreakEvenEnabled || !PositionSelectByTicket(ticket))
      return desired_sl;

   double current_sl = PositionGetDouble(POSITION_SL);
   if(current_sl <= 0.0)
      return desired_sl;

   double tick_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick_size <= 0.0)
      tick_size = SymbolInfoDouble(symbol, SYMBOL_POINT);

   bool break_even_or_better =
      (basket.type == POSITION_TYPE_BUY &&
       current_sl + tick_size * 0.5 >= basket.average_price) ||
      (basket.type == POSITION_TYPE_SELL &&
       current_sl - tick_size * 0.5 <= basket.average_price);

   if(!break_even_or_better)
      return desired_sl;

   return TighterStopPrice(basket.type, desired_sl, current_sl);
}

bool PricesEqualEnough(const string symbol,
                       const double left,
                       const double right)
{"""
    source = _replace_once(source, helper_anchor, helper_block, "break-even helpers")

    modify_anchor = """   string symbol = rt[index].symbol;
   double sl = BasketPhysicalStopPrice(index, basket);
   double tp = 0.0;"""
    modify_block = """   string symbol = rt[index].symbol;
   double physical_sl = BasketPhysicalStopPrice(index, basket);
   double break_even_sl = BasketBreakEvenStopPrice(index, basket);
   double sl = TighterStopPrice(basket.type, physical_sl, break_even_sl);
   double tp = 0.0;"""
    source = _replace_once(source, modify_anchor, modify_block, "basket stop selection")

    apply_anchor = """      if(!ApplyProtectionToTicket(ticket, symbol, sl, tp))
         ok = false;"""
    apply_block = """      double ticket_sl =
         PreserveBreakEvenStopForTicket(ticket, symbol, basket, sl);

      if(!ApplyProtectionToTicket(ticket, symbol, ticket_sl, tp))
         ok = false;"""
    source = _replace_once(source, apply_anchor, apply_block, "one-way SL application")

    validation_anchor = """      PhysicalStopMinPoints <= 0 ||
      ProtectionGraceSeconds < 1 ||
      NewsBlockBeforeMinutes < 0 ||"""
    validation_block = """      PhysicalStopMinPoints <= 0 ||
      ProtectionGraceSeconds < 1 ||
      BreakEvenTriggerTPFraction <= 0.0 ||
      BreakEvenTriggerTPFraction >= 1.0 ||
      BreakEvenLockPoints < 0 ||
      NewsBlockBeforeMinutes < 0 ||"""
    source = _replace_once(source, validation_anchor, validation_block, "input validation")

    return source


def patch_file(path: Path, *, backup: bool = True) -> bool:
    original = path.read_text(encoding="utf-8")
    patched = patch_text(original)
    if patched == original:
        return False

    if backup:
        backup_path = path.with_suffix(path.suffix + ".pre_be_v130")
        if not backup_path.exists():
            backup_path.write_text(original, encoding="utf-8")

    path.write_text(patched, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch AOExtremum v1.30 with one-way basket break-even")
    parser.add_argument("path", nargs="?", default=f"mt5/experts/{SOURCE_NAME}")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.is_file():
        raise SystemExit(f"Source not found: {path}")

    changed = patch_file(path, backup=not args.no_backup)
    print("AOExtremum break-even patch: " + ("APPLIED" if changed else "ALREADY PRESENT"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
