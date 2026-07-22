//+------------------------------------------------------------------+
//|                         AOExtremum_Portfolio4_v1_16.mq5          |
//| Manual per-symbol start lot, grid step and basket stop controls. |
//+------------------------------------------------------------------+
#property strict
#property version   "1.160"
#property description "AO Extremum portfolio v1.16: manual start lot, grid step and stop per symbol"

// -------------------------------------------------------------------
// MANUAL SYMBOL SETTINGS
// These values override the old global StartLot and the hardcoded
// step/stop values from v1.14 after initialization.
// Grid step is specified in broker points.
// Stop is the maximum basket loss as a percentage of account balance.
// Set stop to 0.0 to disable the basket stop for that symbol.
// -------------------------------------------------------------------
input bool   LimitUSDGroupToOneBasket = true; // EURUSD/GBPUSD/NZDUSD: only one basket at a time

input double EURUSDStartLot           = 0.10; // EURUSD starting lot
input int    EURUSDGridStepPoints     = 200;  // EURUSD grid step, points
input double EURUSDStopPercent        = 3.00; // EURUSD basket stop, % of balance

input double GBPUSDStartLot           = 0.10; // GBPUSD starting lot
input int    GBPUSDGridStepPoints     = 250;  // GBPUSD grid step, points
input double GBPUSDStopPercent        = 8.00; // GBPUSD basket stop, % of balance

input double NZDUSDStartLot           = 0.05; // NZDUSD starting lot
input int    NZDUSDGridStepPoints     = 200;  // NZDUSD grid step, points
input double NZDUSDStopPercent        = 3.00; // NZDUSD basket stop, % of balance

input double AUDCADStartLot           = 0.10; // AUDCAD starting lot
input int    AUDCADGridStepPoints     = 300;  // AUDCAD grid step, points
input double AUDCADStopPercent        = 9.00; // AUDCAD basket stop, % of balance

// Rename the original event handlers so v1.16 can provide its own
// event loop while reusing the validated v1.14 trading engine.
#define OnInit             OnInit_v114
#define OnDeinit           OnDeinit_v114
#define OnTick             OnTick_v114
#define OnTimer            OnTimer_v114
#define OnTradeTransaction OnTradeTransaction_v114
#include "AOExtremum_Portfolio4_v1_14.mq5"
#undef OnInit
#undef OnDeinit
#undef OnTick
#undef OnTimer
#undef OnTradeTransaction

bool ValidateManualSettingsV116()
{
   if(EURUSDStartLot <= 0.0 || GBPUSDStartLot <= 0.0 ||
      NZDUSDStartLot <= 0.0 || AUDCADStartLot <= 0.0)
   {
      Print("v1.16: every starting lot must be greater than zero.");
      return false;
   }

   if(EURUSDGridStepPoints <= 0 || GBPUSDGridStepPoints <= 0 ||
      NZDUSDGridStepPoints <= 0 || AUDCADGridStepPoints <= 0)
   {
      Print("v1.16: every grid step must be greater than zero.");
      return false;
   }

   if(EURUSDStopPercent < 0.0 || GBPUSDStopPercent < 0.0 ||
      NZDUSDStopPercent < 0.0 || AUDCADStopPercent < 0.0)
   {
      Print("v1.16: stop percent cannot be negative.");
      return false;
   }

   return true;
}

void ApplyManualSettingsV116()
{
   if(ArraySize(cfg) < 4)
      return;

   // Strategy 0: EURUSD M30
   cfg[0].start_lot        = EURUSDStartLot;
   cfg[0].min_step_points  = EURUSDGridStepPoints;
   cfg[0].stop_percent     = EURUSDStopPercent;

   // Strategy 1: GBPUSD M15
   cfg[1].start_lot        = GBPUSDStartLot;
   cfg[1].min_step_points  = GBPUSDGridStepPoints;
   cfg[1].stop_percent     = GBPUSDStopPercent;

   // Strategy 2: NZDUSD M15
   cfg[2].start_lot        = NZDUSDStartLot;
   cfg[2].min_step_points  = NZDUSDGridStepPoints;
   cfg[2].stop_percent     = NZDUSDStopPercent;
   cfg[2].cooldown_after_sl_hours = 48;

   // Strategy 3: AUDCAD M15
   cfg[3].start_lot        = AUDCADStartLot;
   cfg[3].min_step_points  = AUDCADGridStepPoints;
   cfg[3].stop_percent     = AUDCADStopPercent;
}

bool IsUSDGroupStrategyV116(const int index)
{
   // EURUSD, GBPUSD and NZDUSD share a large common USD factor.
   return index >= 0 && index <= 2;
}

bool OtherUSDGroupBasketOpenV116(const int index)
{
   if(!LimitUSDGroupToOneBasket || !IsUSDGroupStrategyV116(index))
      return false;

   for(int i = 0; i < 3 && i < ArraySize(cfg); i++)
   {
      if(i == index || !rt[i].ready)
         continue;

      BasketInfo basket;
      if(ReadBasket(i, basket))
         return true;
   }

   return false;
}

void DrawPanelV116()
{
   if(!ShowInfoPanel)
   {
      Comment("");
      return;
   }

   string panel = "AO Extremum Portfolio4 v1.16\n";

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
   panel += "USD group: " +
            string(LimitUSDGroupToOneBasket ? "ONE BASKET" : "UNLIMITED") + "\n";

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
               " lot=" + DoubleToString(cfg[i].start_lot * LotScale, 2) +
               " step=" + IntegerToString(cfg[i].min_step_points) +
               " stop=" + DoubleToString(cfg[i].stop_percent, 2) + "%: ";

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
                               TIME_DATE|TIME_MINUTES) + "\n";
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
      else if(OtherUSDGroupBasketOpenV116(i))
      {
         panel += "USD GROUP BLOCK\n";
      }
      else
      {
         panel += "FLAT\n";
      }
   }

   Comment(panel);
}

void ProcessAllV116()
{
   CheckPortfolioRisk();

   for(int i = 0; i < ArraySize(cfg); i++)
   {
      if(!rt[i].ready)
         continue;

      ManageVirtualMoneyStop(i);

      BasketInfo own_basket;
      bool has_own_basket = ReadBasket(i, own_basket);

      // Existing baskets continue to receive TP, SL and grid management.
      // Only a new basket is blocked while another USD-group basket exists.
      if(!has_own_basket && OtherUSDGroupBasketOpenV116(i))
      {
         if(DebugLog)
            Print("USD group block: ", rt[i].symbol,
                  " cannot open while another USD basket is active.");
         continue;
      }

      ProcessNewBar(i);
   }

   DrawPanelV116();
}

int OnInit()
{
   if(!ValidateManualSettingsV116())
      return INIT_PARAMETERS_INCORRECT;

   int result = OnInit_v114();
   if(result != INIT_SUCCEEDED)
      return result;

   ApplyManualSettingsV116();

   Print("AO Extremum Portfolio4 v1.16 initialized. ",
         "EURUSD lot/step/stop=", DoubleToString(EURUSDStartLot, 2), "/",
         EURUSDGridStepPoints, "/", DoubleToString(EURUSDStopPercent, 2), "%; ",
         "GBPUSD=", DoubleToString(GBPUSDStartLot, 2), "/",
         GBPUSDGridStepPoints, "/", DoubleToString(GBPUSDStopPercent, 2), "%; ",
         "NZDUSD=", DoubleToString(NZDUSDStartLot, 2), "/",
         NZDUSDGridStepPoints, "/", DoubleToString(NZDUSDStopPercent, 2), "%; ",
         "AUDCAD=", DoubleToString(AUDCADStartLot, 2), "/",
         AUDCADGridStepPoints, "/", DoubleToString(AUDCADStopPercent, 2), "%.");

   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   OnDeinit_v114(reason);
}

void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
   OnTradeTransaction_v114(trans, request, result);
}

void OnTick()
{
   ProcessAllV116();
}

void OnTimer()
{
   ProcessAllV116();
}
//+------------------------------------------------------------------+
