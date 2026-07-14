#property strict
#property version   "1.10"
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
   for(int index = 0; index < copied; index++)
   {
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
         rates[index].spread
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
