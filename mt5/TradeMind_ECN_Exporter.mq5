#property strict
#property version   "1.20"
#property description "Read-only RoboForex ECN candle exporter for TradeMind AI"

input string          InpSymbols         = "XAUUSD,XAGUSD,.USTECHCash,.US500Cash,.US30Cash,WTI,BRENT";
input ENUM_TIMEFRAMES InpTimeframe       = PERIOD_M5;
input int             InpBarsToExport    = 500;
input int             InpRefreshSeconds  = 10;
input string          InpOutputFolder    = "TradeMindAI_ECN";

string TimeframeLabel()
{
   string value = EnumToString(InpTimeframe);
   StringReplace(value, "PERIOD_", "");
   return value;
}

string SafeFilename(string value)
{
   StringReplace(value, "\\", "_");
   StringReplace(value, "/", "_");
   StringReplace(value, ":", "_");
   StringReplace(value, "*", "_");
   StringReplace(value, "?", "_");
   StringReplace(value, "\"", "_");
   StringReplace(value, "<", "_");
   StringReplace(value, ">", "_");
   StringReplace(value, "|", "_");
   StringReplace(value, " ", "_");
   return value;
}

long CurrentSpreadPoints(string symbol)
{
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   MqlTick tick;
   if(point > 0.0 && SymbolInfoTick(symbol, tick) && tick.ask > 0.0 && tick.bid > 0.0)
   {
      long spread = (long)MathRound((tick.ask - tick.bid) / point);
      if(spread > 0)
         return spread;
   }

   long broker_spread = SymbolInfoInteger(symbol, SYMBOL_SPREAD);
   return broker_spread > 0 ? broker_spread : 0;
}

bool ExportSymbol(string symbol)
{
   if(!SymbolSelect(symbol, true))
   {
      PrintFormat("TradeMind ECN exporter: cannot select symbol %s", symbol);
      return false;
   }

   MqlRates rates[];
   ArraySetAsSeries(rates, false);

   // Shift 1 means only completed candles are exported.
   int copied = CopyRates(symbol, InpTimeframe, 1, InpBarsToExport, rates);
   if(copied < InpBarsToExport)
   {
      PrintFormat(
         "TradeMind ECN exporter: %s %s requested=%d copied=%d. Waiting for history.",
         symbol,
         TimeframeLabel(),
         InpBarsToExport,
         copied
      );
      return false;
   }

   string filename = InpOutputFolder + "\\" + SafeFilename(symbol) + "_" +
                      TimeframeLabel() + ".csv";
   int handle = FileOpen(
      filename,
      FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_COMMON | FILE_SHARE_READ | FILE_SHARE_WRITE,
      ','
   );
   if(handle == INVALID_HANDLE)
   {
      PrintFormat(
         "TradeMind ECN exporter: FileOpen failed for %s, error=%d",
         filename,
         GetLastError()
      );
      ResetLastError();
      return false;
   }

   FileWrite(
      handle,
      "time",
      "symbol",
      "timeframe",
      "open",
      "high",
      "low",
      "close",
      "tick_volume",
      "spread"
   );

   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   long live_spread = CurrentSpreadPoints(symbol);
   for(int index = 0; index < copied; index++)
   {
      long spread = rates[index].spread;

      // Some CFD feeds return zero spread in MqlRates. For the newest completed
      // candle, use a live bid/ask snapshot so each newly recorded signal has a
      // realistic spread instead of a false zero. Historical rows remain untouched.
      if(index == copied - 1 && spread <= 0 && live_spread > 0)
         spread = live_spread;

      FileWrite(
         handle,
         (long)rates[index].time,
         symbol,
         TimeframeLabel(),
         DoubleToString(rates[index].open, digits),
         DoubleToString(rates[index].high, digits),
         DoubleToString(rates[index].low, digits),
         DoubleToString(rates[index].close, digits),
         (long)rates[index].tick_volume,
         spread
      );
   }

   FileFlush(handle);
   FileClose(handle);
   return true;
}

void ExportAll()
{
   string symbols[];
   ushort separator = StringGetCharacter(",", 0);
   int count = StringSplit(InpSymbols, separator, symbols);
   if(count <= 0)
   {
      Print("TradeMind ECN exporter: symbol list is empty");
      return;
   }

   int exported = 0;
   for(int index = 0; index < count; index++)
   {
      string symbol = symbols[index];
      StringTrimLeft(symbol);
      StringTrimRight(symbol);
      if(symbol == "")
         continue;
      if(ExportSymbol(symbol))
         exported++;
   }

   PrintFormat(
      "TradeMind ECN exporter: exported %d/%d symbols to Common\\Files\\%s",
      exported,
      count,
      InpOutputFolder
   );
}

int OnInit()
{
   if(InpBarsToExport < 100)
   {
      Print("TradeMind ECN exporter: InpBarsToExport must be at least 100");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(InpRefreshSeconds < 1)
   {
      Print("TradeMind ECN exporter: InpRefreshSeconds must be at least 1");
      return INIT_PARAMETERS_INCORRECT;
   }

   EventSetTimer(InpRefreshSeconds);
   ExportAll();
   return INIT_SUCCEEDED;
}

void OnTimer()
{
   ExportAll();
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}
