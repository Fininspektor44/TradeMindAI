//+------------------------------------------------------------------+
//|        AOExtremum_Original11_AllPairs_EXACT_v1_30_PROP_RISK.mq5            |
//| Exact per-pair presets recovered from 11 original .set files.     |
//| Prop-safe portfolio wrapper; AO/grid signal core remains unchanged.   |
//+------------------------------------------------------------------+
#property strict
#property version   "1.300"
#property description "AO Extremum v1.30 prop-risk: basket loss cap, staged daily blocking, robust stops and news TP recovery"

#include <Trade/Trade.mqh>

enum ENUM_GRID_BASE_PRICE
{
   GRID_BASE_LAST_ENTRY = 0,
   GRID_BASE_AVERAGE_PRICE = 1
};

// Original pair index map:
// 0 AUDCAD, 1 AUDUSD, 2 EURCAD, 3 EURGBP, 4 EURUSD, 5 GBPAUD,
// 6 GBPNZD, 7 GBPUSD, 8 NZDCAD, 9 NZDUSD, 10 USDCAD.
// -1 = trade all enabled pairs. Values 0..10 isolate one pair.
// AutoConfigurePairScan=true is only for a separate 11-pass scan.
input int    TestPairIndex                  = -1;
input bool   AutoConfigurePairScan           = false;

input bool   Enable_AUDCAD                  = true;  // index 0, reduced lot
input bool   Enable_AUDUSD                  = true;  // index 1, reduced lot
input bool   Enable_EURCAD                  = true;  // index 2
input bool   Enable_EURGBP                  = true;  // index 3
input bool   Enable_EURUSD                  = true;  // index 4
input bool   Enable_GBPAUD                  = false; // index 5, disabled by tests
input bool   Enable_GBPNZD                  = false; // index 6, disabled by tests
input bool   Enable_GBPUSD                  = true;  // index 7
input bool   Enable_NZDCAD                  = true;  // index 8, reduced lot
input bool   Enable_NZDUSD                  = false; // index 9, disabled by tests
input bool   Enable_USDCAD                  = true;  // index 10

// Base lot and per-pair exposure multipliers. These do not alter the
// original signal/grid geometry. The defaults are a balanced prop candidate,
// not a claim of mathematical optimality without a fresh MT5 optimization.
input double OriginalStartLot               = 0.10;
input double LotScale                       = 1.00;
input double PairLotScale_AUDCAD            = 0.50;
input double PairLotScale_AUDUSD            = 0.60;
input double PairLotScale_EURCAD            = 0.80;
input double PairLotScale_EURGBP            = 0.40;
input double PairLotScale_EURUSD            = 0.60;
input double PairLotScale_GBPAUD            = 0.00;
input double PairLotScale_GBPNZD            = 0.00;
input double PairLotScale_GBPUSD            = 0.80;
input double PairLotScale_NZDCAD            = 0.50;
input double PairLotScale_NZDUSD            = 0.00;
input double PairLotScale_USDCAD            = 0.70;

enum ENUM_CLOSE_SCOPE
{
   CLOSE_EA_PORTFOLIO = 0,
   CLOSE_WHOLE_ACCOUNT = 1
};

// Portfolio and execution wrapper.
input bool   EnableTrading                  = true;
input string SymbolPrefix                   = "";
input string SymbolSuffix                   = "";
input long   StrategyMagicBase              = 772700000;
input int    MaxOpenBaskets                 = 5;
input bool   BlockOrdersAtMidnight          = false;
input int    BlockOpenHour                  = 0;
input int    TimerSeconds                   = 1;
input int    DeviationPoints                = 20;
input bool   ShowInfoPanel                  = true;
input bool   DebugLog                       = false;

// Prop daily-loss guard. The reference is the account BALANCE at the start
// of the server day. Current EQUITY is compared with that reference, so a
// floating loss carried across midnight already consumes today's limit.
input bool             DailyLossGuardEnabled       = true;
input double           BlockNewBasketsAtDailyDD    = 2.00;
input double           BlockNewGridLegsAtDailyDD   = 2.75;
input double           DailyHardStopPercent        = 4.50;
input int              DailyResetHourServer        = 0;
input ENUM_CLOSE_SCOPE DailyHardStopScope          = CLOSE_WHOLE_ACCOUNT;
input bool             DailyDeletePendingOrders    = true;
input bool             PersistDailyStopState       = true;

// Basket-level virtual loss cap. It is measured from the fixed balance at
// the start of the current server day. When hit, only that basket is closed
// and the pair is paused until the next server day.
input double           MaxBasketLossPercent        = 1.50;
input bool             PausePairAfterBasketStop    = true;

// Pair-specific depth limits. Zero keeps the original preset.
input int              MaxLegs_EURUSD              = 6;
input int              MaxLegs_USDCAD              = 5;

// Real broker-side SL for every position. It is volatility/distance based,
// not converted from a percentage of account balance. The account-level
// 4.5% guard remains the true emergency loss controller.
input bool            PhysicalStopEnabled          = true;
input ENUM_TIMEFRAMES PhysicalStopATRTimeframe     = PERIOD_D1;
input int             PhysicalStopATRPeriod        = 14;
input double          PhysicalStopATRMultiplier    = 10.0;
input int             PhysicalStopMinPoints        = 5000;
input int             ProtectionGraceSeconds       = 30;
input bool            CloseIfProtectionMissing     = true;

// Prop-account news protection. Add https://nfs.faireconomy.media to the
// MT5 allowed WebRequest URLs before enabling this mode.
input bool   PropMode                       = true;
input bool   NewsFilterEnabled              = true;
input int    NewsBlockBeforeMinutes         = 30;
input int    NewsBlockAfterMinutes          = 10;
input int    NewsSpeechAfterMinutes         = 120;
input int    NewsRefreshSeconds             = 300;
input int    NewsDataMaxAgeMinutes          = 360;
input int    NewsHttpTimeoutMs              = 5000;
input bool   NewsFailClosed                 = true;
input string ForexFactoryCalendarUrl        =
   "https://nfs.faireconomy.media/ff_calendar_thisweek.json";

input bool             EnableCloseAllButton       = true;
input ENUM_CLOSE_SCOPE CloseButtonScope           = CLOSE_WHOLE_ACCOUNT;
input int              CloseButtonConfirmSeconds  = 5;

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
   int      atr_handle;
   datetime last_bar;
   datetime cooldown_until;
   bool     basket_stop_pending;
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




struct ProtectionWatch
{
   ulong    ticket;
   datetime first_seen;
   datetime last_attempt;
};

struct NewsEventInfo
{
   datetime time_server;
   string   currency;
   string   title;
   bool     speech;
};

CTrade trade;
StrategyConfig  cfg[];
StrategyRuntime rt[];

bool     portfolio_halted         = false;
bool     portfolio_soft_block     = false; // aggregate staged-risk block
bool     daily_new_baskets_block  = false;
bool     daily_grid_legs_block    = false;
bool     protection_fault_block   = false;
datetime portfolio_cooldown_until = 0;

double   daily_start_balance      = 0.0;
datetime daily_period_start       = 0;
datetime daily_period_end         = 0;
bool     daily_stop_triggered     = false;
string   daily_gv_prefix          = "";

ProtectionWatch protection_watch[];

NewsEventInfo news_events[];
bool          news_data_valid       = false;
bool          news_block_active     = false; // trading block: confirmed window or fail-closed
bool          news_tp_window_active = false; // confirmed window only
bool          news_tp_suspended     = false;
bool          news_restore_pending  = false;
datetime      news_last_attempt     = 0;
datetime      news_last_success     = 0;
datetime      news_block_start      = 0;
datetime      news_block_end        = 0;
datetime      news_next_time        = 0;
string        news_next_currency    = "";
string        news_next_title       = "";
string        news_last_error       = "";

datetime close_button_armed_until = 0;
const string UI_CLOSE_BUTTON = "AOX_V130_CLOSE_ALL";

// Forward declarations used by protection layers.
bool ReadBasket(const int index, BasketInfo &basket);
bool ModifyBasketStops(const int index);
bool CloseBasket(const int index);
void ReconcilePortfolioProtection();
bool ClosePositionsAndOrders(const ENUM_CLOSE_SCOPE scope, const bool delete_pending);

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

double PairLotScale(const int index)
{
   switch(index)
   {
      case 0:  return PairLotScale_AUDCAD;
      case 1:  return PairLotScale_AUDUSD;
      case 2:  return PairLotScale_EURCAD;
      case 3:  return PairLotScale_EURGBP;
      case 4:  return PairLotScale_EURUSD;
      case 5:  return PairLotScale_GBPAUD;
      case 6:  return PairLotScale_GBPNZD;
      case 7:  return PairLotScale_GBPUSD;
      case 8:  return PairLotScale_NZDCAD;
      case 9:  return PairLotScale_NZDUSD;
      case 10: return PairLotScale_USDCAD;
   }

   return 1.0;
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
               const bool configured_enabled)
{
   cfg[index].base_symbol               = symbol;
   cfg[index].timeframe                 = timeframe;
   cfg[index].lookback_days             = lookback_days;
   cfg[index].start_lot                 = OriginalStartLot;
   cfg[index].lot_multiplier            = 1.40;
   cfg[index].max_orders                = max_orders;
   cfg[index].max_lot                   = 5.00;
   cfg[index].take_profit_points        = take_profit_points;
   cfg[index].stop_percent              = stop_percent;
   cfg[index].use_physical_stop         = PhysicalStopEnabled;
   cfg[index].min_step_points           = min_step_points;
   cfg[index].grid_base                 = GRID_BASE_LAST_ENTRY;
   cfg[index].close_on_opposite         = false;
   cfg[index].open_after_opposite_close = open_after_opposite_close;
   cfg[index].enabled                   =
      (TestPairIndex >= 0 ? TestPairIndex == index : configured_enabled);
   cfg[index].cooldown_after_sl_hours   = 0;
}

void BuildConfigs()
{
   ArrayResize(cfg, 11);
   ArrayResize(rt, 11);

   // Exact current values from the uploaded original .set files:
   // symbol, TF, lookback, max orders, TP, stop %, step,
   // open after opposite close, enabled for all-pairs mode.
   SetConfig(0,  "AUDCAD", PERIOD_M15, 1,  8, 200, 9.0, 300, true,
             Enable_AUDCAD);
   SetConfig(1,  "AUDUSD", PERIOD_M15, 2,  7, 200, 7.0, 250, true,
             Enable_AUDUSD);
   SetConfig(2,  "EURCAD", PERIOD_M30, 3,  5, 200, 8.0, 150, false,
             Enable_EURCAD);
   SetConfig(3,  "EURGBP", PERIOD_M15, 1,  8, 100, 6.0, 150, true,
             Enable_EURGBP);
   SetConfig(4,  "EURUSD", PERIOD_M30, 2, 10, 100, 8.0, 200, false,
             Enable_EURUSD);
   SetConfig(5,  "GBPAUD", PERIOD_M15, 4,  7, 200, 8.0, 150, false,
             Enable_GBPAUD);
   SetConfig(6,  "GBPNZD", PERIOD_M15, 4,  5, 300, 8.0, 200, false,
             Enable_GBPNZD);
   SetConfig(7,  "GBPUSD", PERIOD_M15, 4,  8, 200, 8.0, 250, false,
             Enable_GBPUSD);
   SetConfig(8,  "NZDCAD", PERIOD_M15, 1,  7, 300, 8.0, 150, false,
             Enable_NZDCAD);
   SetConfig(9,  "NZDUSD", PERIOD_M15, 1,  8, 300, 6.0, 200, true,
             Enable_NZDUSD);
   SetConfig(10, "USDCAD", PERIOD_M15, 3,  7, 200, 8.0, 250, false,
             Enable_USDCAD);

   if(MaxLegs_EURUSD > 0 && MaxLegs_EURUSD < cfg[4].max_orders)
      cfg[4].max_orders = MaxLegs_EURUSD;

   if(MaxLegs_USDCAD > 0 && MaxLegs_USDCAD < cfg[10].max_orders)
      cfg[10].max_orders = MaxLegs_USDCAD;
}

string TestSelectionLabel()
{
   if(TestPairIndex < 0)
      return "ALL ENABLED";

   if(TestPairIndex >= 0 && TestPairIndex < ArraySize(cfg))
      return IntegerToString(TestPairIndex) + " " +
             cfg[TestPairIndex].base_symbol;

   return "INVALID";
}

bool IsBlockedHour()
{
   if(!BlockOrdersAtMidnight)
      return false;

   MqlDateTime now;
   TimeToStruct(TimeTradeServer(), now);
   return now.hour == BlockOpenHour;
}


bool IsTesterMode()
{
   return (bool)MQLInfoInteger(MQL_TESTER);
}

bool NewsProtectionConfigured()
{
   return PropMode && NewsFilterEnabled && !IsTesterMode();
}

bool NewsTradingBlocked()
{
   return NewsProtectionConfigured() &&
          (news_block_active || news_restore_pending);
}

bool IsOurMagic(const long magic)
{
   return magic > StrategyMagicBase &&
          magic <= StrategyMagicBase + ArraySize(cfg);
}

string LowerCopy(string value)
{
   StringToLower(value);
   return value;
}

bool IsSpeechTitle(const string title)
{
   string value = LowerCopy(title);

   return StringFind(value, "speaks") >= 0 ||
          StringFind(value, "speech") >= 0 ||
          StringFind(value, "press conference") >= 0 ||
          StringFind(value, "testifies") >= 0 ||
          StringFind(value, "hearing") >= 0;
}

bool IsPortfolioCurrency(const string currency)
{
   for(int i = 0; i < ArraySize(cfg); i++)
   {
      if(!cfg[i].enabled)
         continue;

      string pair = cfg[i].base_symbol;
      if(StringLen(pair) < 6)
         continue;

      if(StringSubstr(pair, 0, 3) == currency ||
         StringSubstr(pair, 3, 3) == currency)
         return true;
   }

   return false;
}

string JsonUnescape(string value)
{
   StringReplace(value, "\\/", "/");
   StringReplace(value, "\\\"", "\"");
   StringReplace(value, "\\\\", "\\");
   return value;
}

bool JsonStringField(const string object_text,
                     const string key,
                     string &value)
{
   string marker = "\"" + key + "\"";
   int key_pos = StringFind(object_text, marker);
   if(key_pos < 0)
      return false;

   int colon_pos = StringFind(object_text, ":", key_pos + StringLen(marker));
   if(colon_pos < 0)
      return false;

   int start_quote = StringFind(object_text, "\"", colon_pos + 1);
   if(start_quote < 0)
      return false;

   bool escaped = false;
   int length = StringLen(object_text);

   for(int i = start_quote + 1; i < length; i++)
   {
      string ch = StringSubstr(object_text, i, 1);

      if(ch == "\"" && !escaped)
      {
         value = JsonUnescape(
            StringSubstr(object_text,
                         start_quote + 1,
                         i - start_quote - 1)
         );
         return true;
      }

      if(ch == "\\" && !escaped)
         escaped = true;
      else
         escaped = false;
   }

   return false;
}

datetime ForexFactoryIsoToServerTime(const string iso)
{
   if(StringLen(iso) < 25)
      return 0;

   string date_part = StringSubstr(iso, 0, 10);
   string time_part = StringSubstr(iso, 11, 8);

   StringReplace(date_part, "-", ".");
   datetime wall_time = StringToTime(date_part + " " + time_part);
   if(wall_time <= 0)
      return 0;

   string sign_text = StringSubstr(iso, 19, 1);
   int offset_hour = (int)StringToInteger(StringSubstr(iso, 20, 2));
   int offset_minute = (int)StringToInteger(StringSubstr(iso, 23, 2));
   int source_offset = offset_hour * 3600 + offset_minute * 60;

   if(sign_text == "-")
      source_offset = -source_offset;

   datetime utc_time = wall_time - source_offset;
   int server_offset = (int)(TimeTradeServer() - TimeGMT());

   return utc_time + server_offset;
}

bool NewsEventExists(NewsEventInfo &events[],
                     const datetime event_time,
                     const string currency,
                     const string title)
{
   for(int i = 0; i < ArraySize(events); i++)
   {
      if(events[i].time_server == event_time &&
         events[i].currency == currency &&
         events[i].title == title)
         return true;
   }

   return false;
}

void SortNewsEvents(NewsEventInfo &events[])
{
   int count = ArraySize(events);

   for(int i = 1; i < count; i++)
   {
      NewsEventInfo current = events[i];
      int j = i - 1;

      while(j >= 0 && events[j].time_server > current.time_server)
      {
         events[j + 1] = events[j];
         j--;
      }

      events[j + 1] = current;
   }
}

bool ParseForexFactoryJson(const string json,
                           NewsEventInfo &parsed[])
{
   ArrayResize(parsed, 0);

   int position = 0;
   int json_length = StringLen(json);

   while(position < json_length)
   {
      int object_start = StringFind(json, "{", position);
      if(object_start < 0)
         break;

      int object_end = StringFind(json, "}", object_start + 1);
      if(object_end < 0)
         return false;

      string object_text =
         StringSubstr(json, object_start, object_end - object_start + 1);

      string title = "";
      string currency = "";
      string date_iso = "";
      string impact = "";

      bool fields_ok =
         JsonStringField(object_text, "title", title) &&
         JsonStringField(object_text, "country", currency) &&
         JsonStringField(object_text, "date", date_iso) &&
         JsonStringField(object_text, "impact", impact);

      if(fields_ok &&
         impact == "High" &&
         IsPortfolioCurrency(currency))
      {
         datetime event_time = ForexFactoryIsoToServerTime(date_iso);

         if(event_time > 0 &&
            !NewsEventExists(parsed, event_time, currency, title))
         {
            int size = ArraySize(parsed);
            ArrayResize(parsed, size + 1);

            parsed[size].time_server = event_time;
            parsed[size].currency    = currency;
            parsed[size].title       = title;
            parsed[size].speech      = IsSpeechTitle(title);
         }
      }

      position = object_end + 1;
   }

   SortNewsEvents(parsed);
   return true;
}

bool DownloadForexFactoryCalendar(string &json)
{
   char request_body[];
   char response[];
   string response_headers = "";

   ArrayResize(request_body, 0);
   ArrayResize(response, 0);

   ResetLastError();

   string headers =
      "Accept: application/json\r\n"
      "User-Agent: MetaTrader5-AOX-v1.30\r\n";

   int http_code =
      WebRequest("GET",
                 ForexFactoryCalendarUrl,
                 headers,
                 NewsHttpTimeoutMs,
                 request_body,
                 response,
                 response_headers);

   if(http_code != 200)
   {
      int error_code = GetLastError();
      news_last_error =
         "HTTP " + IntegerToString(http_code) +
         ", MQL " + IntegerToString(error_code);
      return false;
   }

   uchar utf8_response[];
   ArrayResize(utf8_response, ArraySize(response));

   for(int i = 0; i < ArraySize(response); i++)
      utf8_response[i] = (uchar)response[i];

   json = CharArrayToString(utf8_response, 0,
                            ArraySize(utf8_response), CP_UTF8);

   if(StringLen(json) < 2)
   {
      news_last_error = "Empty Forex Factory response";
      return false;
   }

   return true;
}

bool RefreshNewsCalendar(const bool force=false)
{
   if(!NewsProtectionConfigured())
      return true;

   datetime now = TimeTradeServer();

   if(!force &&
      news_last_attempt > 0 &&
      now - news_last_attempt < NewsRefreshSeconds)
      return news_data_valid;

   news_last_attempt = now;

   string json = "";
   NewsEventInfo parsed[];

   if(!DownloadForexFactoryCalendar(json) ||
      !ParseForexFactoryJson(json, parsed))
   {
      if(news_last_success > 0 &&
         now - news_last_success <= NewsDataMaxAgeMinutes * 60)
      {
         // Keep the last good in-memory calendar during a short outage.
         news_data_valid = true;
         return true;
      }

      news_data_valid = false;
      return false;
   }

   ArrayResize(news_events, ArraySize(parsed));
   for(int i = 0; i < ArraySize(parsed); i++)
      news_events[i] = parsed[i];

   news_data_valid   = true;
   news_last_success = now;
   news_last_error   = "";

   if(DebugLog)
      Print("Forex Factory calendar refreshed. High-impact events=",
            ArraySize(news_events));

   return true;
}

double BasketTakeProfitPrice(const int index,
                             const BasketInfo &basket)
{
   if(cfg[index].take_profit_points <= 0)
      return 0.0;

   double point = SymbolInfoDouble(rt[index].symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(rt[index].symbol, SYMBOL_DIGITS);
   double distance = cfg[index].take_profit_points * point;

   if(basket.type == POSITION_TYPE_BUY)
      return NormalizeDouble(basket.average_price + distance, digits);

   return NormalizeDouble(basket.average_price - distance, digits);
}

bool SuspendAllBasketTakeProfits()
{
   bool ok = true;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      long magic = PositionGetInteger(POSITION_MAGIC);
      if(!IsOurMagic(magic))
         continue;

      double current_tp = PositionGetDouble(POSITION_TP);
      if(current_tp == 0.0)
         continue;

      double current_sl = PositionGetDouble(POSITION_SL);

      if(!trade.PositionModify(ticket, current_sl, 0.0))
      {
         ok = false;
         Print("NEWS TP REMOVE FAILED ticket=", ticket,
               " retcode=", trade.ResultRetcode(),
               " ", trade.ResultRetcodeDescription());
      }
   }

   news_tp_suspended = ok;
   return ok;
}

bool RestoreAllBasketTakeProfits()
{
   bool ok = true;

   for(int i = 0; i < ArraySize(cfg); i++)
   {
      if(!rt[i].ready)
         continue;

      BasketInfo basket;
      if(!ReadBasket(i, basket))
         continue;

      double target = BasketTakeProfitPrice(i, basket);
      if(target <= 0.0)
         continue;

      MqlTick tick;
      if(!SymbolInfoTick(rt[i].symbol, tick))
      {
         ok = false;
         continue;
      }

      bool target_passed =
         (basket.type == POSITION_TYPE_BUY && tick.bid >= target) ||
         (basket.type == POSITION_TYPE_SELL && tick.ask <= target);

      if(target_passed)
      {
         if(!CloseBasket(i))
            ok = false;
      }
      else
      {
         if(!ModifyBasketStops(i))
            ok = false;
      }
   }

   news_restore_pending = !ok;

   if(ok)
      news_tp_suspended = false;

   return ok;
}

string FormatSeconds(const long seconds_value)
{
   long seconds = seconds_value > 0 ? seconds_value : 0;
   long hours = seconds / 3600;
   long minutes = (seconds % 3600) / 60;
   long secs = seconds % 60;

   return StringFormat("%02d:%02d:%02d",
                       (int)hours,
                       (int)minutes,
                       (int)secs);
}

string ShortText(const string value, const int max_length)
{
   if(StringLen(value) <= max_length)
      return value;

   return StringSubstr(value, 0, max_length - 3) + "...";
}

void EvaluateNewsProtection()
{
   if(!NewsProtectionConfigured())
   {
      bool had_tp_window = news_tp_window_active;

      news_block_active = false;
      news_tp_window_active = false;
      news_block_start = 0;
      news_block_end = 0;
      news_next_time = 0;
      news_next_currency = "";
      news_next_title = "";

      if(had_tp_window || news_restore_pending || news_tp_suspended)
         RestoreAllBasketTakeProfits();

      return;
   }

   RefreshNewsCalendar(false);

   datetime now = TimeTradeServer();

   bool calendar_block = false;
   datetime active_start = 0;
   datetime active_end = 0;

   news_next_time = 0;
   news_next_currency = "";
   news_next_title = "";

   for(int i = 0; i < ArraySize(news_events); i++)
   {
      int after_minutes =
         news_events[i].speech
         ? NewsSpeechAfterMinutes
         : NewsBlockAfterMinutes;

      datetime window_start =
         news_events[i].time_server - NewsBlockBeforeMinutes * 60;
      datetime window_end =
         news_events[i].time_server + after_minutes * 60;

      if(now >= window_start && now <= window_end)
      {
         calendar_block = true;

         if(active_start == 0 || window_start < active_start)
            active_start = window_start;
         if(window_end > active_end)
            active_end = window_end;
      }

      if(news_events[i].time_server > now &&
         (news_next_time == 0 ||
          news_events[i].time_server < news_next_time))
      {
         news_next_time = news_events[i].time_server;
         news_next_currency = news_events[i].currency;
         news_next_title = news_events[i].title;
      }
   }

   bool fail_closed_block = !news_data_valid && NewsFailClosed;
   bool was_trade_block = news_block_active;
   bool was_tp_window = news_tp_window_active;

   news_tp_window_active = calendar_block;
   news_block_active = calendar_block || fail_closed_block;
   news_block_start = calendar_block ? active_start : 0;
   news_block_end = calendar_block ? active_end : 0;

   if(news_block_active && !was_trade_block)
   {
      Print(calendar_block
            ? "NEWS BLOCK ACTIVE. New baskets and grid additions are frozen."
            : "NEWS FAIL-CLOSED ACTIVE. New trading is frozen; existing TP remains.");
   }

   if(!news_block_active && was_trade_block)
      Print("NEWS BLOCK CLEARED. Normal trading may resume after protection reconciliation.");

   if(news_tp_window_active)
   {
      // Only a confirmed prohibited window removes TP.
      if(!news_tp_suspended)
         SuspendAllBasketTakeProfits();
   }
   else
   {
      if(was_tp_window)
         Print("CONFIRMED NEWS WINDOW ENDED. Restoring basket take profits.");

      if(was_tp_window || news_restore_pending || news_tp_suspended)
         RestoreAllBasketTakeProfits();
   }
}

void ResetCloseButton()
{
   close_button_armed_until = 0;

   if(ObjectFind(0, UI_CLOSE_BUTTON) >= 0)
   {
      string text =
         CloseButtonScope == CLOSE_WHOLE_ACCOUNT
         ? "CLOSE ALL ACCOUNT"
         : "CLOSE EA PORTFOLIO";

      ObjectSetString(0, UI_CLOSE_BUTTON, OBJPROP_TEXT, text);
      ObjectSetInteger(0, UI_CLOSE_BUTTON, OBJPROP_STATE, false);
      ObjectSetInteger(0, UI_CLOSE_BUTTON, OBJPROP_BGCOLOR, clrFireBrick);
      ChartRedraw();
   }
}

bool CreateCloseButton()
{
   if(!EnableCloseAllButton)
      return true;

   if(ObjectFind(0, UI_CLOSE_BUTTON) < 0)
   {
      if(!ObjectCreate(0, UI_CLOSE_BUTTON, OBJ_BUTTON, 0, 0, 0))
      {
         Print("Cannot create close-all button. Error=", GetLastError());
         return false;
      }
   }

   ObjectSetInteger(0, UI_CLOSE_BUTTON, OBJPROP_CORNER, CORNER_RIGHT_UPPER);
   ObjectSetInteger(0, UI_CLOSE_BUTTON, OBJPROP_XDISTANCE, 15);
   ObjectSetInteger(0, UI_CLOSE_BUTTON, OBJPROP_YDISTANCE, 20);
   ObjectSetInteger(0, UI_CLOSE_BUTTON, OBJPROP_XSIZE, 175);
   ObjectSetInteger(0, UI_CLOSE_BUTTON, OBJPROP_YSIZE, 28);
   ObjectSetInteger(0, UI_CLOSE_BUTTON, OBJPROP_COLOR, clrWhite);
   ObjectSetInteger(0, UI_CLOSE_BUTTON, OBJPROP_BORDER_COLOR, clrWhite);
   ObjectSetInteger(0, UI_CLOSE_BUTTON, OBJPROP_FONTSIZE, 9);
   ObjectSetString(0, UI_CLOSE_BUTTON, OBJPROP_FONT, "Arial");
   ObjectSetInteger(0, UI_CLOSE_BUTTON, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, UI_CLOSE_BUTTON, OBJPROP_HIDDEN, true);
   ObjectSetInteger(0, UI_CLOSE_BUTTON, OBJPROP_ZORDER, 100);

   ResetCloseButton();
   return true;
}

bool ClosePositionsAndOrders(const ENUM_CLOSE_SCOPE scope,
                             const bool delete_pending)
{
   bool ok = true;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      long magic = PositionGetInteger(POSITION_MAGIC);

      if(scope == CLOSE_EA_PORTFOLIO && !IsOurMagic(magic))
         continue;

      if(!trade.PositionClose(ticket, DeviationPoints))
      {
         ok = false;
         Print("EMERGENCY CLOSE FAILED ticket=", ticket,
               " retcode=", trade.ResultRetcode(),
               " ", trade.ResultRetcodeDescription());
      }
   }

   if(delete_pending)
   {
      for(int i = OrdersTotal() - 1; i >= 0; i--)
      {
         ulong ticket = OrderGetTicket(i);
         if(ticket == 0)
            continue;

         long magic = OrderGetInteger(ORDER_MAGIC);

         if(scope == CLOSE_EA_PORTFOLIO && !IsOurMagic(magic))
            continue;

         if(!trade.OrderDelete(ticket))
         {
            ok = false;
            Print("EMERGENCY ORDER DELETE FAILED ticket=", ticket,
                  " retcode=", trade.ResultRetcode(),
                  " ", trade.ResultRetcodeDescription());
         }
      }
   }

   return ok;
}

bool CloseByEmergencyButton()
{
   return ClosePositionsAndOrders(CloseButtonScope, true);
}

void UpdateCloseButtonTimer()
{
   if(close_button_armed_until > 0 &&
      TimeLocal() > close_button_armed_until)
      ResetCloseButton();
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
      PairLotScale(index) *
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

double NormalizePriceToTick(const string symbol,
                            const double price,
                            const bool round_up)
{
   double tick_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   if(tick_size <= 0.0)
      tick_size = SymbolInfoDouble(symbol, SYMBOL_POINT);
   if(tick_size <= 0.0)
      return NormalizeDouble(price, digits);

   double steps = price / tick_size;
   double normalized =
      (round_up ? MathCeil(steps - 1e-9)
                : MathFloor(steps + 1e-9)) * tick_size;

   return NormalizeDouble(normalized, digits);
}

double BrokerMinimumStopDistance(const string symbol)
{
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   long stops_level = SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
   long freeze_level = SymbolInfoInteger(symbol, SYMBOL_TRADE_FREEZE_LEVEL);
   long level = MathMax(stops_level, freeze_level) + 2;

   return MathMax(point * level, point * 2.0);
}

double ATRStopDistance(const int index)
{
   string symbol = rt[index].symbol;
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double minimum = MathMax(PhysicalStopMinPoints, 1) * point;
   double atr_distance = 0.0;

   if(rt[index].atr_handle != INVALID_HANDLE)
   {
      double atr[];
      ArraySetAsSeries(atr, true);

      if(CopyBuffer(rt[index].atr_handle, 0, 1, 1, atr) == 1 &&
         atr[0] > 0.0)
         atr_distance = atr[0] * PhysicalStopATRMultiplier;
   }

   return MathMax(minimum, atr_distance);
}

double BasketPhysicalStopPrice(const int index,
                               const BasketInfo &basket)
{
   if(!PhysicalStopEnabled || !cfg[index].use_physical_stop)
      return 0.0;

   string symbol = rt[index].symbol;

   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
      return 0.0;

   double tick_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick_size <= 0.0)
      tick_size = SymbolInfoDouble(symbol, SYMBOL_POINT);

   double minimum_distance = BrokerMinimumStopDistance(symbol);
   double requested_distance =
      MathMax(ATRStopDistance(index), minimum_distance);

   if(basket.type == POSITION_TYPE_BUY)
   {
      double candidate = basket.average_price - requested_distance;
      double highest_valid = tick.bid - minimum_distance;

      candidate = MathMin(candidate, highest_valid);

      // A buy SL must remain positive. If the requested "kilometre SL"
      // crosses zero, place it at the lowest positive tradable tick.
      if(candidate <= tick_size)
         candidate = tick_size;

      return NormalizePriceToTick(symbol, candidate, false);
   }

   double candidate = basket.average_price + requested_distance;
   double lowest_valid = tick.ask + minimum_distance;

   candidate = MathMax(candidate, lowest_valid);
   return NormalizePriceToTick(symbol, candidate, true);
}

bool PricesEqualEnough(const string symbol,
                       const double left,
                       const double right)
{
   double tick_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick_size <= 0.0)
      tick_size = SymbolInfoDouble(symbol, SYMBOL_POINT);

   return MathAbs(left - right) <= tick_size * 0.5;
}

bool ApplyProtectionToTicket(const ulong ticket,
                             const string symbol,
                             const double desired_sl,
                             const double desired_tp)
{
   if(!PositionSelectByTicket(ticket))
      return false;

   double current_sl = PositionGetDouble(POSITION_SL);
   double current_tp = PositionGetDouble(POSITION_TP);

   bool sl_ok =
      desired_sl <= 0.0 || PricesEqualEnough(symbol, current_sl, desired_sl);
   bool tp_ok =
      PricesEqualEnough(symbol, current_tp, desired_tp);

   if(sl_ok && tp_ok)
      return true;

   // First try the complete protection package, then verify the live
   // position instead of trusting only the local CTrade return value.
   bool combined_sent =
      trade.PositionModify(ticket, desired_sl, desired_tp);

   uint combined_code = trade.ResultRetcode();
   string combined_text = trade.ResultRetcodeDescription();

   if(combined_sent && PositionSelectByTicket(ticket))
   {
      double verified_sl = PositionGetDouble(POSITION_SL);
      double verified_tp = PositionGetDouble(POSITION_TP);

      bool verified_sl_ok =
         desired_sl <= 0.0 ||
         PricesEqualEnough(symbol, verified_sl, desired_sl);
      bool verified_tp_ok =
         PricesEqualEnough(symbol, verified_tp, desired_tp);

      if(verified_sl_ok && verified_tp_ok)
         return true;
   }

   bool result = true;

   // A bad SL must never prevent a valid TP from being installed.
   if(!tp_ok)
   {
      double sl_for_tp = current_sl;

      if(!trade.PositionModify(ticket, sl_for_tp, desired_tp))
         result = false;
   }

   // Re-select after the TP attempt and install the SL while preserving TP.
   if(desired_sl > 0.0)
   {
      if(PositionSelectByTicket(ticket))
      {
         double tp_for_sl = PositionGetDouble(POSITION_TP);

         if(!PricesEqualEnough(symbol,
                               PositionGetDouble(POSITION_SL),
                               desired_sl) &&
            !trade.PositionModify(ticket, desired_sl, tp_for_sl))
            result = false;
      }
      else
      {
         result = false;
      }
   }

   if(!result)
   {
      Print("PROTECTION MODIFY FAILED ", symbol,
            " ticket=", ticket,
            " desired SL=", DoubleToString(desired_sl,
                        (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)),
            " TP=", DoubleToString(desired_tp,
                        (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)),
            " first retcode=", combined_code,
            " ", combined_text,
            " last retcode=", trade.ResultRetcode(),
            " ", trade.ResultRetcodeDescription());
   }

   if(!PositionSelectByTicket(ticket))
      return false;

   current_sl = PositionGetDouble(POSITION_SL);
   current_tp = PositionGetDouble(POSITION_TP);

   sl_ok = desired_sl <= 0.0 ||
           PricesEqualEnough(symbol, current_sl, desired_sl);
   tp_ok = PricesEqualEnough(symbol, current_tp, desired_tp);

   return sl_ok && tp_ok;
}

bool ModifyBasketStops(const int index)
{
   if(!EnableTrading || portfolio_halted)
      return true;

   BasketInfo basket;
   if(!ReadBasket(index, basket))
      return true;

   string symbol = rt[index].symbol;
   double sl = BasketPhysicalStopPrice(index, basket);
   double tp = 0.0;

   // TP is removed only in a CONFIRMED news window. Calendar failure may
   // freeze new trading, but it must never strip TP for hours.
   if(!news_tp_window_active)
      tp = BasketTakeProfitPrice(index, basket);

   bool ok = true;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket;
      if(!SelectOurPositionByIndex(i, index, ticket))
         continue;

      if(!ApplyProtectionToTicket(ticket, symbol, sl, tp))
         ok = false;
   }

   return ok;
}

int FindProtectionWatch(const ulong ticket)
{
   for(int i = 0; i < ArraySize(protection_watch); i++)
   {
      if(protection_watch[i].ticket == ticket)
         return i;
   }

   return -1;
}

void RemoveProtectionWatchAt(const int index)
{
   int size = ArraySize(protection_watch);
   if(index < 0 || index >= size)
      return;

   for(int i = index; i < size - 1; i++)
      protection_watch[i] = protection_watch[i + 1];

   ArrayResize(protection_watch, size - 1);
}

void MarkProtectionMissing(const ulong ticket,
                           const datetime now)
{
   int index = FindProtectionWatch(ticket);

   if(index < 0)
   {
      int size = ArraySize(protection_watch);
      ArrayResize(protection_watch, size + 1);
      protection_watch[size].ticket = ticket;
      protection_watch[size].first_seen = now;
      protection_watch[size].last_attempt = 0;
      return;
   }

   protection_watch[index].last_attempt = now;
}

bool PositionNeedsProtection(const ulong ticket)
{
   if(!PositionSelectByTicket(ticket))
      return false;

   long magic = PositionGetInteger(POSITION_MAGIC);
   if(!IsOurMagic(magic))
      return false;

   bool missing_sl =
      PhysicalStopEnabled &&
      PositionGetDouble(POSITION_SL) <= 0.0;

   bool missing_tp =
      !news_tp_window_active &&
      PositionGetDouble(POSITION_TP) <= 0.0;

   return missing_sl || missing_tp;
}

void CleanupProtectionWatch()
{
   for(int i = ArraySize(protection_watch) - 1; i >= 0; i--)
   {
      if(!PositionNeedsProtection(protection_watch[i].ticket))
         RemoveProtectionWatchAt(i);
   }
}

void ReconcilePortfolioProtection()
{
   if(!EnableTrading || portfolio_halted)
      return;

   datetime now = TimeTradeServer();

   // Recalculate from live basket data every cycle. This restores TP even
   // after a terminal/EA restart, where the old one-shot state is lost.
   for(int i = 0; i < ArraySize(cfg); i++)
   {
      if(!rt[i].ready)
         continue;

      BasketInfo basket;
      if(!ReadBasket(i, basket))
         continue;

      if(!news_tp_window_active)
      {
         double target = BasketTakeProfitPrice(i, basket);
         MqlTick tick;

         if(target > 0.0 && SymbolInfoTick(rt[i].symbol, tick))
         {
            bool target_passed =
               (basket.type == POSITION_TYPE_BUY && tick.bid >= target) ||
               (basket.type == POSITION_TYPE_SELL && tick.ask <= target);

            if(target_passed)
            {
               CloseBasket(i);
               continue;
            }
         }
      }

      ModifyBasketStops(i);
   }

   bool any_missing = false;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      long magic = PositionGetInteger(POSITION_MAGIC);
      if(!IsOurMagic(magic))
         continue;

      if(!PositionNeedsProtection(ticket))
         continue;

      any_missing = true;
      MarkProtectionMissing(ticket, now);

      int watch_index = FindProtectionWatch(ticket);
      if(watch_index < 0)
         continue;

      long age = now - protection_watch[watch_index].first_seen;

      if(CloseIfProtectionMissing &&
         age >= ProtectionGraceSeconds)
      {
         string symbol = PositionGetString(POSITION_SYMBOL);

         Print("UNPROTECTED POSITION CLOSE: ", symbol,
               " ticket=", ticket,
               " missing protection for ", age, " seconds.");

         if(!trade.PositionClose(ticket, DeviationPoints))
         {
            Print("UNPROTECTED CLOSE FAILED ticket=", ticket,
                  " retcode=", trade.ResultRetcode(),
                  " ", trade.ResultRetcodeDescription());
         }
      }
   }

   CleanupProtectionWatch();
   protection_fault_block =
      any_missing || ArraySize(protection_watch) > 0;
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
   if(!EnableTrading || portfolio_halted ||
      protection_fault_block || IsBlockedHour() || NewsTradingBlocked())
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

   if(has_basket && daily_grid_legs_block)
   {
      if(DebugLog)
         Print("DAILY GRID BLOCK: averaging denied for ",
               rt[index].symbol);
      return false;
   }

   if(!has_basket && daily_new_baskets_block)
   {
      if(DebugLog)
         Print("DAILY NEW-BASKET BLOCK: entry denied for ",
               rt[index].symbol);
      return false;
   }

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

bool ManageBasketLossLimit(const int index)
{
   BasketInfo basket;
   bool has_basket = ReadBasket(index, basket);

   if(!has_basket)
   {
      if(rt[index].basket_stop_pending)
      {
         rt[index].basket_stop_pending = false;

         if(PausePairAfterBasketStop)
         {
            rt[index].cooldown_until = daily_period_end;
            Print("BASKET LOSS PAUSE: ", rt[index].symbol,
                  " blocked until ",
                  TimeToString(rt[index].cooldown_until,
                               TIME_DATE|TIME_MINUTES));
         }
      }

      return false;
   }

   if(MaxBasketLossPercent <= 0.0 || daily_start_balance <= 0.0)
      return false;

   double floating = BasketFloatingProfit(index);
   double loss_limit =
      daily_start_balance * MaxBasketLossPercent / 100.0;

   if(!rt[index].basket_stop_pending && floating > -loss_limit)
      return false;

   if(!rt[index].basket_stop_pending)
   {
      rt[index].basket_stop_pending = true;
      Print("BASKET LOSS STOP: ", rt[index].symbol,
            " floating=", DoubleToString(floating, 2),
            ", limit=-", DoubleToString(loss_limit, 2),
            " (", DoubleToString(MaxBasketLossPercent, 2),
            "% of day-start balance). Closing basket.");
   }

   CloseBasket(index);

   BasketInfo remaining;
   if(ReadBasket(index, remaining))
   {
      Print("CRITICAL: basket-stop close incomplete for ",
            rt[index].symbol,
            ". Remaining positions=", remaining.count,
            ". Retrying next cycle.");
      return true;
   }

   rt[index].basket_stop_pending = false;

   if(PausePairAfterBasketStop)
   {
      rt[index].cooldown_until = daily_period_end;
      Print("BASKET LOSS PAUSE: ", rt[index].symbol,
            " blocked until ",
            TimeToString(rt[index].cooldown_until,
                         TIME_DATE|TIME_MINUTES));
   }

   return true;
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
   if(daily_grid_legs_block ||
      basket.count >= cfg[index].max_orders ||
      IsBlockedHour() || NewsTradingBlocked())
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
   if(NewsTradingBlocked())
      return;

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

datetime ServerRiskPeriodStart(const datetime now)
{
   MqlDateTime parts;
   TimeToStruct(now, parts);

   parts.hour = DailyResetHourServer;
   parts.min = 0;
   parts.sec = 0;

   datetime start = StructToTime(parts);
   if(now < start)
      start -= 86400;

   return start;
}

string DailyStateKey(const string suffix,
                     const datetime period_start)
{
   return daily_gv_prefix + suffix + "_" +
          IntegerToString((int)period_start);
}

double RealizedAccountResultSince(const datetime from_time,
                                  const datetime to_time)
{
   if(!HistorySelect(from_time, to_time))
      return 0.0;

   double result = 0.0;
   int total = HistoryDealsTotal();

   for(int i = 0; i < total; i++)
   {
      ulong deal = HistoryDealGetTicket(i);
      if(deal == 0)
         continue;

      ENUM_DEAL_TYPE type =
         (ENUM_DEAL_TYPE)HistoryDealGetInteger(deal, DEAL_TYPE);

      if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL)
         continue;

      result += HistoryDealGetDouble(deal, DEAL_PROFIT);
      result += HistoryDealGetDouble(deal, DEAL_SWAP);
      result += HistoryDealGetDouble(deal, DEAL_COMMISSION);
      result += HistoryDealGetDouble(deal, DEAL_FEE);
   }

   return result;
}

double ReconstructPeriodStartBalance(const datetime period_start,
                                     const datetime now)
{
   double current_balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double realized =
      RealizedAccountResultSince(period_start, now);

   return current_balance - realized;
}

void SaveDailyRiskState()
{
   if(!PersistDailyStopState || IsTesterMode() || daily_period_start <= 0)
      return;

   GlobalVariableSet(
      DailyStateKey("BALANCE", daily_period_start),
      daily_start_balance
   );
   GlobalVariableSet(
      DailyStateKey("STOP", daily_period_start),
      daily_stop_triggered ? 1.0 : 0.0
   );
}

void InitializeDailyRiskState(const bool force_rebuild=false)
{
   datetime now = TimeTradeServer();
   datetime period_start = ServerRiskPeriodStart(now);

   if(!force_rebuild &&
      daily_period_start == period_start &&
      daily_start_balance > 0.0)
      return;

   daily_period_start = period_start;
   daily_period_end = period_start + 86400;

   string balance_key = DailyStateKey("BALANCE", period_start);
   string stop_key = DailyStateKey("STOP", period_start);

   if(PersistDailyStopState && !IsTesterMode() &&
      GlobalVariableCheck(balance_key))
   {
      daily_start_balance = GlobalVariableGet(balance_key);
   }
   else
   {
      daily_start_balance =
         ReconstructPeriodStartBalance(period_start, now);
   }

   if(daily_start_balance <= 0.0)
      daily_start_balance = AccountInfoDouble(ACCOUNT_BALANCE);

   daily_stop_triggered =
      PersistDailyStopState && !IsTesterMode() &&
      GlobalVariableCheck(stop_key) &&
      GlobalVariableGet(stop_key) > 0.5;

   portfolio_halted = daily_stop_triggered;
   portfolio_cooldown_until =
      daily_stop_triggered ? daily_period_end : 0;

   SaveDailyRiskState();

   Print("DAILY RISK BASE: server period ",
         TimeToString(daily_period_start, TIME_DATE|TIME_MINUTES),
         ", start balance=", DoubleToString(daily_start_balance, 2),
         ", stop=", daily_stop_triggered ? "TRIGGERED" : "READY");
}

double DailyLossMoney()
{
   if(daily_start_balance <= 0.0)
      return 0.0;

   return MathMax(
      0.0,
      daily_start_balance - AccountInfoDouble(ACCOUNT_EQUITY)
   );
}

double PortfolioDrawdownPercent()
{
   if(daily_start_balance <= 0.0)
      return 0.0;

   return DailyLossMoney() / daily_start_balance * 100.0;
}

void CheckPortfolioRisk()
{
   datetime now = TimeTradeServer();
   datetime current_period = ServerRiskPeriodStart(now);

   if(daily_period_start != current_period ||
      daily_start_balance <= 0.0)
   {
      // A new server day clears yesterday's halt and fixes a fresh balance
      // reference. Any carried floating loss is automatically included
      // because the live EQUITY is compared with this BALANCE reference.
      daily_stop_triggered = false;
      portfolio_halted = false;
      portfolio_soft_block = false;
      daily_new_baskets_block = false;
      daily_grid_legs_block = false;
      portfolio_cooldown_until = 0;
      InitializeDailyRiskState(true);
   }

   if(!DailyLossGuardEnabled)
   {
      portfolio_halted = false;
      portfolio_soft_block = false;
      daily_new_baskets_block = false;
      daily_grid_legs_block = false;
      return;
   }

   if(daily_stop_triggered)
   {
      portfolio_halted = true;
      portfolio_soft_block = true;
      daily_new_baskets_block = true;
      daily_grid_legs_block = true;
      portfolio_cooldown_until = daily_period_end;

      // Retry every cycle until the requested scope is flat.
      ClosePositionsAndOrders(
         DailyHardStopScope,
         DailyDeletePendingOrders
      );
      return;
   }

   double loss_percent = PortfolioDrawdownPercent();

   if(DailyHardStopPercent > 0.0 &&
      loss_percent >= DailyHardStopPercent)
   {
      daily_stop_triggered = true;
      portfolio_halted = true;
      portfolio_soft_block = true;
      daily_new_baskets_block = true;
      daily_grid_legs_block = true;
      portfolio_cooldown_until = daily_period_end;
      SaveDailyRiskState();

      Print("DAILY HARD STOP: start balance=",
            DoubleToString(daily_start_balance, 2),
            ", equity=",
            DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2),
            ", loss=",
            DoubleToString(DailyLossMoney(), 2),
            " (", DoubleToString(loss_percent, 2),
            "%). Closing scope and pausing until ",
            TimeToString(daily_period_end,
                         TIME_DATE|TIME_MINUTES));

      ClosePositionsAndOrders(
         DailyHardStopScope,
         DailyDeletePendingOrders
      );
      return;
   }

   bool should_block_new_baskets =
      BlockNewBasketsAtDailyDD > 0.0 &&
      loss_percent >= BlockNewBasketsAtDailyDD;

   bool should_block_grid_legs =
      BlockNewGridLegsAtDailyDD > 0.0 &&
      loss_percent >= BlockNewGridLegsAtDailyDD;

   if(should_block_new_baskets != daily_new_baskets_block)
   {
      daily_new_baskets_block = should_block_new_baskets;
      Print(daily_new_baskets_block
            ? "DAILY RISK: new baskets are blocked."
            : "DAILY RISK: new-basket block cleared.");
   }

   if(should_block_grid_legs != daily_grid_legs_block)
   {
      daily_grid_legs_block = should_block_grid_legs;
      Print(daily_grid_legs_block
            ? "DAILY RISK: new averaging legs are blocked."
            : "DAILY RISK: grid-leg block cleared.");
   }

   portfolio_soft_block =
      daily_new_baskets_block || daily_grid_legs_block;
}

void ProcessAll()
{
   EvaluateNewsProtection();
   UpdateCloseButtonTimer();
   CheckPortfolioRisk();
   ReconcilePortfolioProtection();

   for(int i = 0; i < ArraySize(cfg); i++)
   {
      if(!rt[i].ready)
         continue;

      if(ManageBasketLossLimit(i))
         continue;

      ManageVirtualMoneyStop(i);
      ProcessNewBar(i);
   }

   if(ShowInfoPanel)
   {
      string panel =
         "AO Extremum Original11 EXACT v1.30 PROP RISK\n";

      string trading_state = "DISABLED";
      if(EnableTrading)
      {
         if(portfolio_halted)
            trading_state = "EMERGENCY COOLDOWN";
         else if(daily_grid_legs_block)
            trading_state = "GRID LEGS BLOCKED";
         else if(daily_new_baskets_block)
            trading_state = "NEW BASKETS BLOCKED";
         else if(protection_fault_block)
            trading_state = "PROTECTION FAULT";
         else if(NewsTradingBlocked())
            trading_state = "NEWS FROZEN";
         else
            trading_state = "ENABLED";
      }

      panel += "Trading: " + trading_state + "\n";
      panel += "Prop mode: " + string(PropMode ? "ON" : "OFF") + "\n";

      if(IsTesterMode() && PropMode && NewsFilterEnabled)
      {
         panel += "News: DISABLED IN STRATEGY TESTER\n";
      }
      else if(NewsProtectionConfigured())
      {
         panel += "FF calendar: " +
                  string(news_data_valid ? "OK" : "ERROR") + "\n";

         if(!news_data_valid && news_last_error != "")
            panel += "Calendar error: " +
                     ShortText(news_last_error, 55) + "\n";

         if(news_block_active)
         {
            if(news_block_end > 0)
            {
               panel += "News block: ACTIVE, left " +
                        FormatSeconds(news_block_end -
                                      TimeTradeServer()) + "\n";
               panel += "Block until: " +
                        TimeToString(news_block_end,
                                     TIME_DATE|TIME_MINUTES) + "\n";
            }
            else
            {
               panel += "News block: FAIL-CLOSED / NO DATA\n";
            }

            panel += news_tp_window_active
                     ? "Baskets: FROZEN, TP: SUSPENDED\n"
                     : "Baskets: FROZEN, TP: KEPT (calendar error)\n";
         }
         else if(news_restore_pending)
         {
            panel += "News block: RESTORING TP\n";
            panel += "Baskets: FROZEN until restore succeeds\n";
         }
         else
         {
            panel += "News block: OFF\n";
         }

         if(news_next_time > 0)
         {
            panel += "Next red: " +
                     TimeToString(news_next_time,
                                  TIME_DATE|TIME_MINUTES) +
                     " " + news_next_currency + "\n";
            panel += "Event: " +
                     ShortText(news_next_title, 58) + "\n";
            panel += "Block starts in: " +
                     FormatSeconds(
                        news_next_time -
                        NewsBlockBeforeMinutes * 60 -
                        TimeTradeServer()
                     ) + "\n";
         }
      }
      else
      {
         panel += "News protection: OFF\n";
      }

      panel += "Day start balance: " +
               DoubleToString(daily_start_balance, 2) + "\n";
      panel += "Daily loss: " +
               DoubleToString(DailyLossMoney(), 2) +
               " / " +
               DoubleToString(daily_start_balance *
                              DailyHardStopPercent / 100.0, 2) +
               " (" +
               DoubleToString(PortfolioDrawdownPercent(), 2) + "%)\n";
      panel += "Daily stages: new " +
               DoubleToString(BlockNewBasketsAtDailyDD, 2) +
               "%, grid " +
               DoubleToString(BlockNewGridLegsAtDailyDD, 2) +
               "%, hard " +
               DoubleToString(DailyHardStopPercent, 2) + "%\n";
      panel += "Basket stop: " +
               DoubleToString(MaxBasketLossPercent, 2) +
               "% of day-start balance\n";
      panel += "Protection fault: " +
               string(protection_fault_block ? "YES" : "NO") + "\n";
      panel += "Baskets: " +
               IntegerToString(CountOpenBaskets()) + "/" +
               (MaxOpenBaskets > 0
                ? IntegerToString(MaxOpenBaskets)
                : "unlimited") + "\n";
      panel += "Selection: " + TestSelectionLabel() + "\n";
      panel += "Base lot: " +
               DoubleToString(OriginalStartLot * LotScale, 2) +
               ", grid multiplier 1.40\n";

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
                  TimeframeLabel(cfg[i].timeframe) +
                  " L" + IntegerToString(cfg[i].lookback_days) +
                  " O" + IntegerToString(cfg[i].max_orders) +
                  " TP" + IntegerToString(cfg[i].take_profit_points) +
                  " S" + IntegerToString(cfg[i].min_step_points) +
                  " lotx" + DoubleToString(PairLotScale(i), 2) +
                  ": ";

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
               IntegerToString(cfg[i].max_orders) +
               " P/L " + DoubleToString(BasketFloatingProfit(i), 2);

            if(NewsTradingBlocked())
               panel += " FROZEN";

            panel += "\n";
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

   if(TestPairIndex < -1 || TestPairIndex >= ArraySize(cfg) ||
      OriginalStartLot <= 0.0 || LotScale <= 0.0 ||
      MaxOpenBaskets < 0 ||
      BlockNewBasketsAtDailyDD < 0.0 ||
      BlockNewGridLegsAtDailyDD < 0.0 ||
      MaxBasketLossPercent < 0.0 ||
      MaxLegs_EURUSD < 0 ||
      MaxLegs_USDCAD < 0 ||
      DailyHardStopPercent < 0.0 ||
      DailyHardStopPercent >= 100.0 ||
      DailyResetHourServer < 0 ||
      DailyResetHourServer > 23 ||
      PhysicalStopATRPeriod <= 0 ||
      PhysicalStopATRMultiplier <= 0.0 ||
      PhysicalStopMinPoints <= 0 ||
      ProtectionGraceSeconds < 1 ||
      NewsBlockBeforeMinutes < 0 ||
      NewsBlockAfterMinutes < 0 ||
      NewsSpeechAfterMinutes < NewsBlockAfterMinutes ||
      NewsRefreshSeconds <= 0 ||
      NewsDataMaxAgeMinutes < 0 ||
      NewsHttpTimeoutMs <= 0 ||
      CloseButtonConfirmSeconds <= 0)
   {
      Print("Invalid portfolio input parameters.");
      return INIT_PARAMETERS_INCORRECT;
   }

   for(int i = 0; i < ArraySize(cfg); i++)
   {
      if(cfg[i].lookback_days <= 0 ||
         cfg[i].start_lot <= 0.0 ||
         cfg[i].lot_multiplier <= 0.0 ||
         cfg[i].max_orders <= 0 ||
         cfg[i].max_lot <= 0.0 ||
         cfg[i].take_profit_points <= 0 ||
         cfg[i].stop_percent < 0.0 ||
         cfg[i].min_step_points <= 0)
      {
         Print("Invalid original preset for ", cfg[i].base_symbol);
         return INIT_PARAMETERS_INCORRECT;
      }
   }

   if(BlockNewBasketsAtDailyDD > 0.0 &&
      BlockNewGridLegsAtDailyDD > 0.0 &&
      BlockNewGridLegsAtDailyDD <= BlockNewBasketsAtDailyDD)
   {
      Print("BlockNewGridLegsAtDailyDD must be greater than BlockNewBasketsAtDailyDD.");
      return INIT_PARAMETERS_INCORRECT;
   }

   if(DailyHardStopPercent > 0.0 &&
      ((BlockNewBasketsAtDailyDD > 0.0 &&
        DailyHardStopPercent <= BlockNewBasketsAtDailyDD) ||
       (BlockNewGridLegsAtDailyDD > 0.0 &&
        DailyHardStopPercent <= BlockNewGridLegsAtDailyDD) ||
       (MaxBasketLossPercent > 0.0 &&
        DailyHardStopPercent <= MaxBasketLossPercent)))
   {
      Print("DailyHardStopPercent must be greater than all staged and basket limits.");
      return INIT_PARAMETERS_INCORRECT;
   }

   for(int i = 0; i < ArraySize(cfg); i++)
   {
      if(PairLotScale(i) < 0.0)
      {
         Print("Pair lot scale cannot be negative for ",
               cfg[i].base_symbol);
         return INIT_PARAMETERS_INCORRECT;
      }

      if(cfg[i].enabled && PairLotScale(i) <= 0.0)
      {
         Print("Enabled pair has zero lot scale: ",
               cfg[i].base_symbol);
         return INIT_PARAMETERS_INCORRECT;
      }
   }

   ENUM_ACCOUNT_MARGIN_MODE margin_mode =
      (ENUM_ACCOUNT_MARGIN_MODE)AccountInfoInteger(ACCOUNT_MARGIN_MODE);

   if(margin_mode != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
   {
      Print("AO Extremum Original11 AllPairs EXACT requires a hedging account.");
      return INIT_FAILED;
   }

   int ready_count = 0;

   for(int i = 0; i < ArraySize(cfg); i++)
   {
      rt[i].symbol         = "";
      rt[i].magic          = StrategyMagicBase + i + 1;
      rt[i].ao_handle      = INVALID_HANDLE;
      rt[i].atr_handle     = INVALID_HANDLE;
      rt[i].last_bar       = 0;
      rt[i].cooldown_until    = 0;
      rt[i].basket_stop_pending = false;
      rt[i].ready             = false;

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

      if(PhysicalStopEnabled)
      {
         rt[i].atr_handle =
            iATR(rt[i].symbol,
                 PhysicalStopATRTimeframe,
                 PhysicalStopATRPeriod);

         if(rt[i].atr_handle == INVALID_HANDLE)
         {
            Print("ATR handle unavailable for ", rt[i].symbol,
                  ". Physical SL will use minimum-point fallback.");
         }
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
   CreateCloseButton();

   daily_gv_prefix =
      "AOX_DAILY_" +
      StringFormat("%I64d",
                   AccountInfoInteger(ACCOUNT_LOGIN)) +
      "_" +
      StringFormat("%I64d", StrategyMagicBase) +
      "_";

   InitializeDailyRiskState(true);

   if(NewsProtectionConfigured())
   {
      if(!RefreshNewsCalendar(true) && NewsFailClosed)
         Print("WARNING: Forex Factory calendar unavailable. "
               "Trading will remain fail-closed until data is received.");
   }
   else if(IsTesterMode() && PropMode && NewsFilterEnabled)
   {
      Print("News protection is disabled in Strategy Tester because "
            "WebRequest is unavailable there.");
   }

   EvaluateNewsProtection();
   ReconcilePortfolioProtection();

   Print("AO Extremum Original11 AllPairs EXACT v1.30 initialized. Selection=",
         TestSelectionLabel(),
         ", ready strategies=",
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
      if(rt[i].atr_handle != INVALID_HANDLE)
         IndicatorRelease(rt[i].atr_handle);
   }

   Comment("");
   ObjectDelete(0, UI_CLOSE_BUTTON);
}

void OnChartEvent(const int id,
                  const long &lparam,
                  const double &dparam,
                  const string &sparam)
{
   if(id != CHARTEVENT_OBJECT_CLICK ||
      sparam != UI_CLOSE_BUTTON ||
      !EnableCloseAllButton)
      return;

   ObjectSetInteger(0, UI_CLOSE_BUTTON, OBJPROP_STATE, false);

   datetime now = TimeLocal();

   if(close_button_armed_until == 0 ||
      now > close_button_armed_until)
   {
      close_button_armed_until =
         now + CloseButtonConfirmSeconds;

      ObjectSetString(0, UI_CLOSE_BUTTON, OBJPROP_TEXT,
                      "CLICK AGAIN TO CONFIRM");
      ObjectSetInteger(0, UI_CLOSE_BUTTON,
                       OBJPROP_BGCOLOR, clrOrangeRed);
      ChartRedraw();

      Print("Emergency close button armed for ",
            CloseButtonConfirmSeconds, " seconds.");
      return;
   }

   bool ok = CloseByEmergencyButton();
   Print(ok
         ? "Emergency close-all command completed."
         : "Emergency close-all command finished with errors.");

   ResetCloseButton();
}

void OnTick()
{
   ProcessAll();
}

void OnTimer()
{
   ProcessAll();
}

// When enabled, MT5 optimization automatically performs 11 passes:
// TestPairIndex = 0, 1, ... 10. Other parameters remain fixed.
void OnTesterInit()
{
   if(!AutoConfigurePairScan)
      return;

   if(!ParameterSetRange("TestPairIndex", true, 0, 0, 1, 10))
      Print("Cannot configure TestPairIndex optimization range. Error=",
            GetLastError());
}

// MQL5 requires OnTesterDeinit() whenever OnTesterInit() is declared.
// No tester-agent resources need explicit cleanup in this EA.
void OnTesterDeinit()
{
}

// Choose "Custom max" as the MT5 optimization criterion.
// The score rewards profit, penalizes equity drawdown and discounts tiny samples.
double OnTester()
{
   double profit = TesterStatistics(STAT_PROFIT);
   double equity_dd_percent =
      TesterStatistics(STAT_EQUITY_DDREL_PERCENT);
   double trades = TesterStatistics(STAT_TRADES);

   if(trades <= 0.0)
      return -1.0e12;

   double sample_weight = MathMin(1.0, trades / 200.0);
   double dd_denominator = MathMax(0.25, equity_dd_percent);

   if(profit <= 0.0)
      return profit - equity_dd_percent * 1000.0;

   return profit / dd_denominator * sample_weight;
}
//+------------------------------------------------------------------+
