//+------------------------------------------------------------------+
//|                         AOExtremum_Portfolio4_v1_15.mq5          |
//| Risk-control wrapper for Portfolio4 v1.14                        |
//+------------------------------------------------------------------+
#property strict
#property version   "1.150"
#property description "AO Extremum portfolio v1.15: one USD basket and reduced NZDUSD risk"

// v1.15 additions
input bool   LimitUSDGroupToOneBasket = true;
input double NZDUSDLotFactor          = 0.70;
input double NZDUSDStopPercent        = 3.00;

// Rename the original event handlers so this file can provide
// the v1.15 event loop while reusing the validated v1.14 engine.
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

bool IsUSDGroupStrategy(const int index)
{
   // EURUSD, GBPUSD and NZDUSD all express a large common USD factor.
   return index >= 0 && index <= 2;
}

bool OtherUSDGroupBasketOpen(const int index)
{
   if(!LimitUSDGroupToOneBasket || !IsUSDGroupStrategy(index))
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

void DrawPanelV115()
{
   if(!ShowInfoPanel)
   {
      Comment("");
      return;
   }

   string panel = "AO Extremum Portfolio4 v1.15\n";

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
   panel += "NZDUSD lot: " +
            DoubleToString(StartLot * NZDUSDLotFactor * LotScale, 2) + "\n";
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
      else if(OtherUSDGroupBasketOpen(i))
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

void ProcessAllV115()
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
      if(!has_own_basket && OtherUSDGroupBasketOpen(i))
      {
         if(DebugLog)
            Print("USD group block: ", rt[i].symbol,
                  " cannot open while another USD basket is active.");
         continue;
      }

      ProcessNewBar(i);
   }

   DrawPanelV115();
}

int OnInit()
{
   if(NZDUSDLotFactor <= 0.0 || NZDUSDLotFactor > 1.0 ||
      NZDUSDStopPercent <= 0.0)
   {
      Print("Invalid v1.15 NZDUSD risk parameters.");
      return INIT_PARAMETERS_INCORRECT;
   }

   int result = OnInit_v114();
   if(result != INIT_SUCCEEDED)
      return result;

   // Strategy index 2 is NZDUSD.
   cfg[2].start_lot               = StartLot * NZDUSDLotFactor;
   cfg[2].stop_percent            = NZDUSDStopPercent;
   cfg[2].cooldown_after_sl_hours = 48;

   Print("AO Extremum Portfolio4 v1.15 initialized. ",
         "NZDUSD lot factor=", DoubleToString(NZDUSDLotFactor, 2),
         ", NZDUSD stop=", DoubleToString(NZDUSDStopPercent, 2), "%",
         ", USD group limit=", LimitUSDGroupToOneBasket ? "ON" : "OFF");

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
   ProcessAllV115();
}

void OnTimer()
{
   ProcessAllV115();
}
//+------------------------------------------------------------------+
