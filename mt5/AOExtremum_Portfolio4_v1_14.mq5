//+------------------------------------------------------------------+
//|                         AOExtremum_Portfolio4_v1_14.mq5        |
//| Clean-room multi-symbol implementation of the validated          |
//| AO Extremum behavioral clone.                                    |
//+------------------------------------------------------------------+
#property strict
#property version   "1.140"
#property description "AO Extremum portfolio: EURUSD M30, GBPUSD M15, NZDUSD M15, AUDCAD M15"

#include <Trade/Trade.mqh>

enum ENUM_GRID_BASE_PRICE
{
   GRID_BASE_LAST_ENTRY = 0,
   GRID_BASE_AVERAGE_PRICE = 1
};

// Safety and portfolio controls
input bool   EnableTrading                  = false;
input string SymbolPrefix                   = "";
input string SymbolSuffix                   = "";
input long   StrategyMagicBase              = 771200000;
input double StartLot                       = 0.10;  // base lot for every strategy
input double LotScale                       = 1.00;
input int    MaxOpenBaskets                 = 2;     // 0 = unlimited
input double PortfolioSoftBlockPercent      = 5.0;   // block new entries and averaging
input double PortfolioHardStopPercent       = 7.0;   // close all portfolio baskets
input int    PortfolioHardCooldownHours     = 48;    // automatic restart delay
input bool   BlockOrdersAtMidnight          = true;
input int    BlockOpenHour                  = 0;
input int    TimerSeconds                   = 1;
input int    DeviationPoints                = 20;
input bool   ShowInfoPanel                  = true;
input bool   DebugLog                       = false;

struct StrategyConfig
{
   string                 base_symbol;
   ENUM_TIMEFRAMES        timeframe;
   int                    lookback_days;
   double                 start_lot;
   double                 lot_multiplier;
   int                    max_orders;
   double                 max_lot;
   int                    take_profit_points;
   double                 stop_percent;
   bool                   use_physical_stop;
   int                    min_step_points;
   ENUM_GRID_BASE_PRICE   grid_base;
   bool                   close_on_opposite;
   bool                   open_after_opposite_close;
   bool                   enabled;
   int                    cooldown_after_sl_hours;
};

struct StrategyRuntime
{
   string   symbol;
   long     magic;
   int      ao_handle;
   datetime last_bar;
   datetime cooldown_until;
   bool     ready;
};

struct BasketInfo
{
   int                count;
   ENUM_POSITION_TYPE type;
   double             total_volume;
   double             weighted_price;
   double             average_price;
   double             last_entry_price;
   long               last_entry_time_msc;
};

CTrade trade;
StrategyConfig  cfg[];
StrategyRuntime rt[];

bool     portfolio_halted         = false;
bool     portfolio_soft_block     = false;
datetime portfolio_cooldown_until = 0;

//+------------------------------------------------------------------+
//| Helpers                                                          |
//+------------------------------------------------------------------+
string ResolveSymbol(const string base_symbol)
{
   string candidate = SymbolPrefix + base_symbol + SymbolSuffix;
   if(SymbolSelect(candidate, true))
      return candidate;

   return "";
}

string TimeframeLabel(const ENUM_TIMEFRAMES timeframe)
{
   if(timeframe == PERIOD_M15)
      return "M15";
   if(timeframe == PERIOD_M30)
      return "M30";
   return EnumToString(timeframe);
}

void SetConfig(const int index,
               const string symbol,
               const ENUM_TIMEFRAMES timeframe,
               const int lookback_days,
               const int max_orders,
               const int take_profit_points,
               const double stop_percent,
               const int min_step_points,
               const bool open_after_opposite_close,
               const int cooldown_after_sl_hours)
{
   cfg[index].base_symbol               = symbol;
   cfg[index].timeframe                 = timeframe;
   cfg[index].lookback_days             = lookback_days;
   cfg[index].start_lot                 = StartLot;
   cfg[index].lot_multiplier            = 1.40;
   cfg[index].max_orders                = max_orders;
   cfg[index].max_lot                   = 5.00;
   cfg[index].take_profit_points        = take_profit_points;
   cfg[index].stop_percent              = stop_percent;
   cfg[index].use_physical_stop         = true;
   cfg[index].min_step_points           = min_step_points;
   cfg[index].grid_base                 = GRID_BASE_LAST_ENTRY;
   cfg[index].close_on_opposite         = false;
   cfg[index].open_after_opposite_close = open_after_opposite_close;
   cfg[index].enabled                   = true;
   cfg[index].cooldown_after_sl_hours   = cooldown_after_sl_hours;
}

void BuildConfigs()
{
   ArrayResize(cfg, 4);
   ArrayResize(rt, 4);

   // symbol, timeframe, lookback days, max orders, TP points,
   // basket stop %, grid step points, open reverse basket, cooldown after SL
   SetConfig(0, "EURUSD", PERIOD_M30, 2, 8, 100, 3.0, 200, false, 24);
   SetConfig(1, "GBPUSD", PERIOD_M15, 4, 8, 200, 8.0, 250, false, 0);
   SetConfig(2, "NZDUSD", PERIOD_M15, 1, 8, 300, 2.0, 200, true, 48);
   SetConfig(3, "AUDCAD", PERIOD_M15, 1, 8, 200, 9.0, 300, true, 0);
}

bool IsBlockedHour()
{
   if(!BlockOrdersAtMidnight)
      return false;

   MqlDateTime now;
   TimeToStruct(TimeTradeServer(), now);
   return now.hour == BlockOpenHour;
}

int VolumeDigits(const string symbol)
{
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0)
      return 2;

   int digits = 0;
   while(digits < 8 && NormalizeDouble(step, digits) != step)
      digits++;

   return digits;
}

double NormalizeVolumeDown(const int index, double raw_volume)
{
   string symbol = rt[index].symbol;

   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   double minv = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxv = MathMin(SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX),
                         cfg[index].max_lot);

   if(step <= 0.0)
      step = 0.01;

   raw_volume *= LotScale;
   raw_volume = MathMin(raw_volume, maxv);

   double volume = MathFloor(raw_volume / step) * step;
   volume = NormalizeDouble(volume, VolumeDigits(symbol));

   if(volume < minv)
      volume = minv;
   if(volume > maxv)
      volume = maxv;

   return volume;
}

double LotForIndex(const int index, const int order_index)
{
   return NormalizeVolumeDown(
      index,
      cfg[index].start_lot *
      MathPow(cfg[index].lot_multiplier, order_index)
   );
}

bool SelectOurPositionByIndex(const int position_index,
                              const int strategy_index,
                              ulong &ticket)
{
   ticket = PositionGetTicket(position_index);
   if(ticket == 0)
      return false;

   if(PositionGetString(POSITION_SYMBOL) != rt[strategy_index].symbol)
      return false;

   if((long)PositionGetInteger(POSITION_MAGIC) != rt[strategy_index].magic)
      return false;

   return true;
}

bool ReadBasket(const int index, BasketInfo &basket)
{
   basket.count               = 0;
   basket.total_volume        = 0.0;
   basket.weighted_price      = 0.0;
   basket.average_price       = 0.0;
   basket.last_entry_price    = 0.0;
   basket.last_entry_time_msc = -1;
   basket.type                = POSITION_TYPE_BUY;

   bool type_set = false;

   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket;
      if(!SelectOurPositionByIndex(i, index, ticket))
         continue;

      ENUM_POSITION_TYPE type =
         (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);

      if(!type_set)
      {
         basket.type = type;
         type_set = true;
      }

      if(type != basket.type)
         continue;

      double volume = PositionGetDouble(POSITION_VOLUME);
      double price  = PositionGetDouble(POSITION_PRICE_OPEN);
      long time_msc = PositionGetInteger(POSITION_TIME_MSC);

      basket.count++;
      basket.total_volume   += volume;
      basket.weighted_price += price * volume;

      if(time_msc >= basket.last_entry_time_msc)
      {
         basket.last_entry_time_msc = time_msc;
         basket.last_entry_price    = price;
      }
   }

   if(basket.count == 0 || basket.total_volume <= 0.0)
      return false;

   basket.average_price = basket.weighted_price / basket.total_volume;
   return true;
}

int CountOpenBaskets()
{
   int result = 0;

   for(int i = 0; i < ArraySize(cfg); i++)
   {
      if(!rt[i].ready)
         continue;

      BasketInfo basket;
      if(ReadBasket(i, basket))
         result++;
   }

   return result;
}

double BasketFloatingProfit(const int index)
{
   double result = 0.0;

   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket;
      if(!SelectOurPositionByIndex(i, index, ticket))
         continue;

      result += PositionGetDouble(POSITION_PROFIT);
      result += PositionGetDouble(POSITION_SWAP);
   }

   return result;
}

double BasketStopDistance(const int index, const BasketInfo &basket)
{
   if(cfg[index].stop_percent <= 0.0 || basket.total_volume <= 0.0)
      return 0.0;

   string symbol = rt[index].symbol;

   double balance        = AccountInfoDouble(ACCOUNT_BALANCE);
   double max_loss_money = balance * cfg[index].stop_percent / 100.0;
   double tick_size      = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   double tick_value     = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE_LOSS);

   if(tick_size <= 0.0 || tick_value <= 0.0)
      return 0.0;

   double money_per_price_unit =
      basket.total_volume * tick_value / tick_size;

   if(money_per_price_unit <= 0.0)
      return 0.0;

   return max_loss_money / money_per_price_unit;
}

bool ModifyBasketStops(const int index)
{
   if(!EnableTrading || portfolio_halted)
      return true;

   BasketInfo basket;
   if(!ReadBasket(index, basket))
      return true;

   string symbol = rt[index].symbol;
   double point  = SymbolInfoDouble(symbol, SYMBOL_POINT);
   int digits    = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   double tp_distance = cfg[index].take_profit_points * point;
   double sl_distance = BasketStopDistance(index, basket);

   double tp = 0.0;
   double sl = 0.0;

   if(basket.type == POSITION_TYPE_BUY)
   {
      if(cfg[index].take_profit_points > 0)
         tp = NormalizeDouble(basket.average_price + tp_distance, digits);

      if(cfg[index].use_physical_stop && sl_distance > 0.0)
         sl = NormalizeDouble(basket.average_price - sl_distance, digits);
   }
   else
   {
      if(cfg[index].take_profit_points > 0)
         tp = NormalizeDouble(basket.average_price - tp_distance, digits);

      if(cfg[index].use_physical_stop && sl_distance > 0.0)
         sl = NormalizeDouble(basket.average_price + sl_distance, digits);
   }

   bool ok = true;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket;
      if(!SelectOurPositionByIndex(i, index, ticket))
         continue;

      if(!trade.PositionModify(ticket, sl, tp))
      {
         ok = false;

         if(DebugLog)
            Print("Modify failed ", symbol,
                  " ticket=", ticket,
                  " retcode=", trade.ResultRetcode(),
                  " ", trade.ResultRetcodeDescription());
      }
   }

   return ok;
}

bool CloseBasket(const int index)
{
   if(!EnableTrading)
      return false;

   bool ok = true;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket;
      if(!SelectOurPositionByIndex(i, index, ticket))
         continue;

      if(!trade.PositionClose(ticket, DeviationPoints))
      {
         ok = false;

         if(DebugLog)
            Print("Close failed ", rt[index].symbol,
                  " ticket=", ticket,
                  " retcode=", trade.ResultRetcode(),
                  " ", trade.ResultRetcodeDescription());
      }
   }

   return ok;
}

void CloseAllStrategies()
{
   for(int i = 0; i < ArraySize(cfg); i++)
   {
      if(rt[i].ready)
         CloseBasket(i);
   }
}

bool OpenPosition(const int index,
                  const ENUM_POSITION_TYPE type,
                  const int order_index)
{
   if(!EnableTrading || portfolio_halted || portfolio_soft_block ||
      IsBlockedHour())
      return false;

   datetime now = TimeTradeServer();
   if(rt[index].cooldown_until > now)
   {
      if(DebugLog)
         Print("Cooldown blocks ", rt[index].symbol,
               " until ", TimeToString(rt[index].cooldown_until,
                                      TIME_DATE|TIME_MINUTES));
      return false;
   }

   BasketInfo existing;
   bool has_basket = ReadBasket(index, existing);

   if(!has_basket &&
      MaxOpenBaskets > 0 &&
      CountOpenBaskets() >= MaxOpenBaskets)
   {
      if(DebugLog)
         Print("Basket limit blocks ", rt[index].symbol);
      return false;
   }

   double lot = LotForIndex(index, order_index);
   if(lot <= 0.0)
      return false;

   string comment =
      "AOX " + cfg[index].base_symbol + " " +
      TimeframeLabel(cfg[index].timeframe);

   trade.SetExpertMagicNumber(rt[index].magic);
   trade.SetDeviationInPoints(DeviationPoints);
   trade.SetTypeFillingBySymbol(rt[index].symbol);

   bool ok = false;

   if(type == POSITION_TYPE_BUY)
      ok = trade.Buy(lot, rt[index].symbol, 0.0, 0.0, 0.0, comment);
   else
      ok = trade.Sell(lot, rt[index].symbol, 0.0, 0.0, 0.0, comment);

   if(!ok)
   {
      if(DebugLog)
         Print("Open failed ", rt[index].symbol,
               " type=", EnumToString(type),
               " lot=", DoubleToString(lot, VolumeDigits(rt[index].symbol)),
               " retcode=", trade.ResultRetcode(),
               " ", trade.ResultRetcodeDescription());

      return false;
   }

   if(DebugLog)
      Print("Opened ", rt[index].symbol,
            " ", EnumToString(type),
            " lot=", DoubleToString(lot, VolumeDigits(rt[index].symbol)),
            " level=", order_index + 1);

   ModifyBasketStops(index);
   return true;
}

//+------------------------------------------------------------------+
//| Validated AO signal                                              |
//+------------------------------------------------------------------+
int GetAOSignal(const int index)
{
   string symbol = rt[index].symbol;
   ENUM_TIMEFRAMES timeframe = cfg[index].timeframe;

   datetime candidate_time = iTime(symbol, timeframe, 1);
   if(candidate_time <= 0)
      return 0;

   datetime cutoff =
      candidate_time - (datetime)(cfg[index].lookback_days * 86400);

   // Include the nearest available bar at or immediately before cutoff.
   // This is required for the validated weekend/holiday behavior.
   int boundary_shift = iBarShift(symbol, timeframe, cutoff, false);
   if(boundary_shift < 1)
      boundary_shift = 1;

   int need = boundary_shift + 2;

   double ao[];
   ArraySetAsSeries(ao, true);

   int copied = CopyBuffer(rt[index].ao_handle, 0, 0, need, ao);
   if(copied <= boundary_shift)
      return 0;

   double candidate = ao[1];
   double minimum   = candidate;
   double maximum   = candidate;

   for(int i = 2; i <= boundary_shift; i++)
   {
      minimum = MathMin(minimum, ao[i]);
      maximum = MathMax(maximum, ao[i]);
   }

   const double epsilon = 1e-12;

   if(candidate < 0.0 && candidate <= minimum + epsilon)
      return 1;

   if(candidate > 0.0 && candidate >= maximum - epsilon)
      return -1;

   return 0;
}

void ManageVirtualMoneyStop(const int index)
{
   if(cfg[index].use_physical_stop || cfg[index].stop_percent <= 0.0)
      return;

   BasketInfo basket;
   if(!ReadBasket(index, basket))
      return;

   double loss_limit =
      AccountInfoDouble(ACCOUNT_BALANCE) *
      cfg[index].stop_percent / 100.0;

   if(BasketFloatingProfit(index) <= -loss_limit)
      CloseBasket(index);
}

void ManageGridOnNewBar(const int index, const BasketInfo &basket)
{
   if(basket.count >= cfg[index].max_orders || IsBlockedHour())
      return;

   MqlTick tick;
   if(!SymbolInfoTick(rt[index].symbol, tick))
      return;

   double point =
      SymbolInfoDouble(rt[index].symbol, SYMBOL_POINT);

   double step_distance =
      cfg[index].min_step_points * point;

   double base =
      cfg[index].grid_base == GRID_BASE_AVERAGE_PRICE
      ? basket.average_price
      : basket.last_entry_price;

   bool adverse = false;

   if(basket.type == POSITION_TYPE_BUY)
      adverse = tick.ask <= base - step_distance;
   else
      adverse = tick.bid >= base + step_distance;

   if(adverse)
      OpenPosition(index, basket.type, basket.count);
}

void ProcessNewBar(const int index)
{
   string symbol = rt[index].symbol;
   ENUM_TIMEFRAMES timeframe = cfg[index].timeframe;

   datetime current_bar = iTime(symbol, timeframe, 0);

   if(current_bar <= 0 || current_bar == rt[index].last_bar)
      return;

   rt[index].last_bar = current_bar;

   BasketInfo basket;
   bool has_basket = ReadBasket(index, basket);
   int signal = GetAOSignal(index);

   if(has_basket)
   {
      if(signal == 0)
         return;

      ENUM_POSITION_TYPE signal_type =
         signal > 0 ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;

      // Grid addition requires both an adverse price step and
      // a fresh AO signal in the existing basket direction.
      if(basket.type == signal_type)
      {
         ManageGridOnNewBar(index, basket);
         return;
      }

      if(!cfg[index].close_on_opposite)
         return;

      if(CloseBasket(index) && cfg[index].open_after_opposite_close)
         OpenPosition(index, signal_type, 0);

      return;
   }

   if(signal == 0)
      return;

   ENUM_POSITION_TYPE signal_type =
      signal > 0 ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;

   OpenPosition(index, signal_type, 0);
}

double PortfolioDrawdownPercent()
{
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);

   if(balance <= 0.0)
      return 0.0;

   return MathMax(0.0, (balance - equity) / balance * 100.0);
}

void CheckPortfolioRisk()
{
   datetime now = TimeTradeServer();

   if(portfolio_halted)
   {
      if(portfolio_cooldown_until > 0 &&
         now >= portfolio_cooldown_until)
      {
         portfolio_halted         = false;
         portfolio_soft_block     = false;
         portfolio_cooldown_until = 0;

         Print("PORTFOLIO TRADING RESUMED after emergency cooldown.");
      }
      else
      {
         return;
      }
   }

   double drawdown_percent = PortfolioDrawdownPercent();

   if(PortfolioHardStopPercent > 0.0 &&
      drawdown_percent >= PortfolioHardStopPercent)
   {
      portfolio_halted     = true;
      portfolio_soft_block = true;

      int cooldown_hours = PortfolioHardCooldownHours;
      if(cooldown_hours < 0)
         cooldown_hours = 0;

      portfolio_cooldown_until =
         now + (datetime)(cooldown_hours * 3600);

      Print("PORTFOLIO HARD STOP: drawdown=",
            DoubleToString(drawdown_percent, 2),
            "%, cooldown until ",
            TimeToString(portfolio_cooldown_until,
                         TIME_DATE|TIME_MINUTES));

      CloseAllStrategies();
      return;
   }

   bool should_soft_block =
      PortfolioSoftBlockPercent > 0.0 &&
      drawdown_percent >= PortfolioSoftBlockPercent;

   if(should_soft_block != portfolio_soft_block)
   {
      portfolio_soft_block = should_soft_block;

      if(portfolio_soft_block)
      {
         Print("PORTFOLIO SOFT BLOCK: drawdown=",
               DoubleToString(drawdown_percent, 2),
               "%. New baskets and averaging are blocked.");
      }
      else
      {
         Print("PORTFOLIO SOFT BLOCK CLEARED: drawdown=",
               DoubleToString(drawdown_percent, 2), "%.");
      }
   }
}

void ProcessAll()
{
   CheckPortfolioRisk();

   for(int i = 0; i < ArraySize(cfg); i++)
   {
      if(!rt[i].ready)
         continue;

      ManageVirtualMoneyStop(i);
      ProcessNewBar(i);
   }

   if(ShowInfoPanel)
   {
      string panel = "AO Extremum Portfolio4 v1.14\n";

      string trading_state = "DISABLED";
      if(EnableTrading)
      {
         if(portfolio_halted)
            trading_state = "EMERGENCY COOLDOWN";
         else if(portfolio_soft_block)
            trading_state = "SOFT BLOCK";
         else
            trading_state = "ENABLED";
      }

      panel += "Trading: " + trading_state + "\n";
      panel += "Portfolio DD: " +
               DoubleToString(PortfolioDrawdownPercent(), 2) + "%\n";
      panel += "Baskets: " +
               IntegerToString(CountOpenBaskets()) + "/" +
               (MaxOpenBaskets > 0
                ? IntegerToString(MaxOpenBaskets)
                : "unlimited") + "\n";
      panel += "Start lot: " +
               DoubleToString(StartLot * LotScale, 2) + "\n";

      if(portfolio_halted && portfolio_cooldown_until > 0)
      {
         panel += "Resume: " +
                  TimeToString(portfolio_cooldown_until,
                               TIME_DATE|TIME_MINUTES) + "\n";
      }

      panel += "\n";

      for(int i = 0; i < ArraySize(cfg); i++)
      {
         if(!cfg[i].enabled)
            continue;

         panel += cfg[i].base_symbol + " " +
                  TimeframeLabel(cfg[i].timeframe) + ": ";

         if(!rt[i].ready)
         {
            panel += "SYMBOL ERROR\n";
            continue;
         }

         datetime now = TimeTradeServer();
         if(rt[i].cooldown_until > now)
         {
            panel += "COOLDOWN until " +
                     TimeToString(rt[i].cooldown_until,
                                  TIME_DATE|TIME_MINUTES) +
                     "\n";
            continue;
         }

         BasketInfo basket;
         if(ReadBasket(i, basket))
         {
            panel +=
               string(basket.type == POSITION_TYPE_BUY ? "BUY " : "SELL ") +
               IntegerToString(basket.count) + "/" +
               IntegerToString(cfg[i].max_orders) + "\n";
         }
         else
         {
            panel += "FLAT\n";
         }
      }

      Comment(panel);
   }
   else
   {
      Comment("");
   }
}

//+------------------------------------------------------------------+
//| Events                                                           |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD || trans.deal == 0)
      return;

   if(!HistoryDealSelect(trans.deal))
      return;

   ENUM_DEAL_ENTRY entry =
      (ENUM_DEAL_ENTRY)HistoryDealGetInteger(trans.deal, DEAL_ENTRY);

   if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY)
      return;

   ENUM_DEAL_REASON reason =
      (ENUM_DEAL_REASON)HistoryDealGetInteger(trans.deal, DEAL_REASON);

   if(reason != DEAL_REASON_SL)
      return;

   long magic = HistoryDealGetInteger(trans.deal, DEAL_MAGIC);
   datetime deal_time =
      (datetime)HistoryDealGetInteger(trans.deal, DEAL_TIME);

   for(int i = 0; i < ArraySize(rt); i++)
   {
      if(rt[i].magic != magic || cfg[i].cooldown_after_sl_hours <= 0)
         continue;

      datetime until_time =
         deal_time + cfg[i].cooldown_after_sl_hours * 3600;

      if(until_time > rt[i].cooldown_until)
         rt[i].cooldown_until = until_time;

      Print("SL cooldown: ", rt[i].symbol,
            " blocked until ",
            TimeToString(rt[i].cooldown_until,
                         TIME_DATE|TIME_MINUTES));
      break;
   }
}

int OnInit()
{
   BuildConfigs();

   if(StartLot <= 0.0 || LotScale <= 0.0 || MaxOpenBaskets < 0 ||
      PortfolioSoftBlockPercent < 0.0 ||
      PortfolioHardStopPercent < 0.0 ||
      PortfolioHardCooldownHours < 0)
   {
      Print("Invalid portfolio input parameters.");
      return INIT_PARAMETERS_INCORRECT;
   }

   if(PortfolioSoftBlockPercent > 0.0 &&
      PortfolioHardStopPercent > 0.0 &&
      PortfolioHardStopPercent <= PortfolioSoftBlockPercent)
   {
      Print("PortfolioHardStopPercent must be greater than PortfolioSoftBlockPercent.");
      return INIT_PARAMETERS_INCORRECT;
   }

   ENUM_ACCOUNT_MARGIN_MODE margin_mode =
      (ENUM_ACCOUNT_MARGIN_MODE)AccountInfoInteger(ACCOUNT_MARGIN_MODE);

   if(margin_mode != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
   {
      Print("AO Extremum Portfolio4 requires a hedging account.");
      return INIT_FAILED;
   }

   int ready_count = 0;

   for(int i = 0; i < ArraySize(cfg); i++)
   {
      rt[i].symbol         = "";
      rt[i].magic          = StrategyMagicBase + i + 1;
      rt[i].ao_handle      = INVALID_HANDLE;
      rt[i].last_bar       = 0;
      rt[i].cooldown_until = 0;
      rt[i].ready          = false;

      if(!cfg[i].enabled)
         continue;

      rt[i].symbol = ResolveSymbol(cfg[i].base_symbol);

      if(rt[i].symbol == "")
      {
         Print("Symbol unavailable: ",
               SymbolPrefix + cfg[i].base_symbol + SymbolSuffix);
         continue;
      }

      rt[i].ao_handle =
         iAO(rt[i].symbol, cfg[i].timeframe);

      if(rt[i].ao_handle == INVALID_HANDLE)
      {
         Print("Cannot create AO handle for ",
               rt[i].symbol, " ",
               TimeframeLabel(cfg[i].timeframe),
               ". Error=", GetLastError());
         continue;
      }

      rt[i].ready = true;
      ready_count++;

      if(DebugLog)
         Print("Ready: ", rt[i].symbol,
               " ", TimeframeLabel(cfg[i].timeframe),
               " magic=", rt[i].magic);
   }

   if(ready_count == 0)
   {
      Print("No configured strategies are ready.");
      return INIT_FAILED;
   }

   if(TimerSeconds > 0)
      EventSetTimer(TimerSeconds);

   trade.SetAsyncMode(false);

   Print("AO Extremum Portfolio4 v1.14 initialized. Ready strategies=",
         ready_count,
         ", trading=", EnableTrading ? "ENABLED" : "DISABLED");

   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();

   for(int i = 0; i < ArraySize(rt); i++)
   {
      if(rt[i].ao_handle != INVALID_HANDLE)
         IndicatorRelease(rt[i].ao_handle);
   }

   Comment("");
}

void OnTick()
{
   ProcessAll();
}

void OnTimer()
{
   ProcessAll();
}
//+------------------------------------------------------------------+
