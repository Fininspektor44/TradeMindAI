//+------------------------------------------------------------------+
//|                 AOExtremum_Portfolio4_v1_16_manual.mq5          |
//| Portfolio4 v1.15 logic with manual lot, grid step and stop       |
//| settings for every strategy.                                     |
//+------------------------------------------------------------------+
#property strict
#property version   "1.160"
#property description "AO Extremum Portfolio4 v1.16: manual lot, grid step and stop per strategy"

// Manual strategy controls. Lot values are still multiplied by LotScale.
input double EURUSD_StartLot          = 0.10;
input int    EURUSD_GridStepPoints    = 200;
input double EURUSD_StopPercent       = 3.00;  // 0 = disabled

input double GBPUSD_StartLot          = 0.10;
input int    GBPUSD_GridStepPoints    = 250;
input double GBPUSD_StopPercent       = 8.00;  // 0 = disabled

input double NZDUSD_StartLot          = 0.07;
input int    NZDUSD_GridStepPoints    = 200;
input double NZDUSD_StopPercent       = 3.00;  // 0 = disabled

input double AUDCAD_StartLot          = 0.10;
input int    AUDCAD_GridStepPoints    = 300;
input double AUDCAD_StopPercent       = 9.00;  // 0 = disabled

input bool   LimitUSDGroupToOneBasket = true;

// Rename the original v1.14 event handlers so this file can provide
// the v1.16 event loop while reusing the validated v1.14 engine.
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

void ApplyManualStrategySettingsV116()
{
   // Strategy indexes from the validated v1.14 engine:
   // 0 EURUSD M30, 1 GBPUSD M15, 2 NZDUSD M15, 3 AUDCAD M15.
   cfg[0].start_lot        = EURUSD_StartLot;
   cfg[0].min_step_points  = EURUSD_GridStepPoints;
   cfg[0].stop_percent     = EURUSD_StopPercent;

   cfg[1].start_lot        = GBPUSD_StartLot;
   cfg[1].min_step_points  = GBPUSD_GridStepPoints;
   cfg[1].stop_percent     = GBPUSD_StopPercent;

   cfg[2].start_lot        = NZDUSD_StartLot;
   cfg[2].min_step_points  = NZDUSD_GridStepPoints;
   cfg[2].stop_percent     = NZDUSD_StopPercent;
   cfg[2].cooldown_after_sl_hours = 48;

   cfg[3].start_lot        = AUDCAD_StartLot;
   cfg[3].min_step_points  = AUDCAD_GridStepPoints;
   cfg[3].stop_percent     = AUDCAD_StopPercent;
}

void DrawPanelV116()
{
   if(!ShowInfoPanel)
   {
      Comment("");
      return;
   }

   string panel = "AO Extremum Portfolio4 v1.16 manual\n";

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

   panel += "\nManual settings, lot x LotScale:\n";
   panel += "EURUSD: lot " + DoubleToString(EURUSD_StartLot, 2) +
            ", step " + IntegerToString(EURUSD_GridStepPoints) +
            ", stop " + DoubleToString(EURUSD_StopPercent, 2) + "%\n";
   panel += "GBPUSD: lot " + DoubleToString(GBPUSD_StartLot, 2) +
            ", step " + IntegerToString(GBPUSD_GridStepPoints) +
            ", stop " + DoubleToString(GBPUSD_StopPercent, 2) + "%\n";
   panel += "NZDUSD: lot " + DoubleToString(NZDUSD_StartLot, 2) +
            ", step " + IntegerToString(NZDUSD_GridStepPoints) +
            ", stop " + DoubleToString(NZDUSD_StopPercent, 2) + "%\n";
   panel += "AUDCAD: lot " + DoubleToString(AUDCAD_StartLot, 2) +
            ", step " + IntegerToString(AUDCAD_GridStepPoints) +
            ", stop " + DoubleToString(AUDCAD_StopPercent, 2) + "%\n\n";

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
      // Only a new basket is blocked when another USD-group basket exists.
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

bool ManualInputsValidV116()
{
   if(EURUSD_StartLot <= 0.0 || GBPUSD_StartLot <= 0.0 ||
      NZDUSD_StartLot <= 0.0 || AUDCAD_StartLot <= 0.0)
      return false;

   if(EURUSD_GridStepPoints <= 0 || GBPUSD_GridStepPoints <= 0 ||
      NZDUSD_GridStepPoints <= 0 || AUDCAD_GridStepPoints <= 0)
      return false;

   if(EURUSD_StopPercent < 0.0 || GBPUSD_StopPercent < 0.0 ||
      NZDUSD_StopPercent < 0.0 || AUDCAD_StopPercent < 0.0)
      return false;

   return true;
}

int OnInit()
{
   if(!ManualInputsValidV116())
   {
      Print("Invalid v1.16 manual strategy parameters.");
      return INIT_PARAMETERS_INCORRECT;
   }

   int result = OnInit_v114();
   if(result != INIT_SUCCEEDED)
      return result;

   ApplyManualStrategySettingsV116();

   Print("AO Extremum Portfolio4 v1.16 initialized with manual settings. ",
         "EURUSD lot/step/stop=", DoubleToString(EURUSD_StartLot, 2), "/",
         EURUSD_GridStepPoints, "/", DoubleToString(EURUSD_StopPercent, 2),
         ", GBPUSD=", DoubleToString(GBPUSD_StartLot, 2), "/",
         GBPUSD_GridStepPoints, "/", DoubleToString(GBPUSD_StopPercent, 2),
         ", NZDUSD=", DoubleToString(NZDUSD_StartLot, 2), "/",
         NZDUSD_GridStepPoints, "/", DoubleToString(NZDUSD_StopPercent, 2),
         ", AUDCAD=", DoubleToString(AUDCAD_StartLot, 2), "/",
         AUDCAD_GridStepPoints, "/", DoubleToString(AUDCAD_StopPercent, 2));

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
