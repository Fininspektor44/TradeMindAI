#property strict
#property version   "1.180"
#property description "Read-only account, position and symbol snapshots for TradeMind Risk Manager"

input int      InpRefreshSeconds = 30;
input string   InpOutputFolder   = "TradeMindAI";
input string   InpSymbols        = ""; // Optional Market Watch filter. Blank = every selected symbol.

string TrimCopy(string value)
{
   StringTrimLeft(value);
   StringTrimRight(value);
   return value;
}

bool ListContainsString(string list,string value)
{
   string clean=TrimCopy(list);
   if(clean=="")
      return true;

   string parts[];
   ushort separator=StringGetCharacter(",",0);
   int count=StringSplit(clean,separator,parts);
   for(int index=0;index<count;index++)
   {
      string item=TrimCopy(parts[index]);
      if(item!="" && item==value)
         return true;
   }
   return false;
}

string EnumTail(string value,string prefix)
{
   StringReplace(value,prefix,"");
   return value;
}

string LoginText()
{
   return IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
}

string AccountFilename()
{
   return InpOutputFolder+"\\mt5_risk_account_"+LoginText()+".csv";
}

string PositionsFilename()
{
   return InpOutputFolder+"\\mt5_risk_positions_"+LoginText()+".csv";
}

string SymbolsFilename()
{
   return InpOutputFolder+"\\mt5_risk_symbols_"+LoginText()+".csv";
}

bool AppendAccountSnapshot()
{
   string filename=AccountFilename();
   int handle=FileOpen(
      filename,
      FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE,
      ','
   );
   if(handle==INVALID_HANDLE)
   {
      PrintFormat("TradeMind risk exporter: account file open failed for %s, error=%d",filename,GetLastError());
      ResetLastError();
      return false;
   }

   bool empty=(FileSize(handle)==0);
   FileSeek(handle,0,SEEK_END);
   if(empty)
   {
      FileWrite(
         handle,
         "time_msc","account_login","server","currency","balance","equity","margin","free_margin",
         "margin_level","leverage","open_positions","trade_allowed","terminal_connected"
      );
   }

   FileWrite(
      handle,
      (long)TimeCurrent()*1000,
      AccountInfoInteger(ACCOUNT_LOGIN),
      AccountInfoString(ACCOUNT_SERVER),
      AccountInfoString(ACCOUNT_CURRENCY),
      DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE),2),
      DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY),2),
      DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN),2),
      DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE),2),
      DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL),2),
      AccountInfoInteger(ACCOUNT_LEVERAGE),
      PositionsTotal(),
      (AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) ? 1 : 0),
      (TerminalInfoInteger(TERMINAL_CONNECTED) ? 1 : 0)
   );
   FileFlush(handle);
   FileClose(handle);
   return true;
}

bool ExportPositionSnapshot()
{
   string filename=PositionsFilename();
   int handle=FileOpen(
      filename,
      FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE,
      ','
   );
   if(handle==INVALID_HANDLE)
   {
      PrintFormat("TradeMind risk exporter: position file open failed for %s, error=%d",filename,GetLastError());
      ResetLastError();
      return false;
   }

   FileWrite(
      handle,
      "time_msc","account_login","server","currency","position_ticket","position_id","position_time_msc",
      "symbol","magic","side","volume","open_price","current_price","sl","tp","profit","swap","comment"
   );

   long captured_msc=(long)TimeCurrent()*1000;
   int written=0;
   int total=PositionsTotal();
   for(int index=0;index<total;index++)
   {
      ulong ticket=PositionGetTicket(index);
      if(ticket==0 || !PositionSelectByTicket(ticket))
         continue;

      string symbol=PositionGetString(POSITION_SYMBOL);
      if(symbol=="")
         continue;

      int digits=(int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);
      ENUM_POSITION_TYPE type=(ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      string side=EnumTail(EnumToString(type),"POSITION_TYPE_");
      FileWrite(
         handle,
         captured_msc,
         AccountInfoInteger(ACCOUNT_LOGIN),
         AccountInfoString(ACCOUNT_SERVER),
         AccountInfoString(ACCOUNT_CURRENCY),
         ticket,
         (ulong)PositionGetInteger(POSITION_IDENTIFIER),
         (long)PositionGetInteger(POSITION_TIME_MSC),
         symbol,
         PositionGetInteger(POSITION_MAGIC),
         side,
         DoubleToString(PositionGetDouble(POSITION_VOLUME),8),
         DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN),digits),
         DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT),digits),
         DoubleToString(PositionGetDouble(POSITION_SL),digits),
         DoubleToString(PositionGetDouble(POSITION_TP),digits),
         DoubleToString(PositionGetDouble(POSITION_PROFIT),2),
         DoubleToString(PositionGetDouble(POSITION_SWAP),2),
         PositionGetString(POSITION_COMMENT)
      );
      written++;
   }

   FileFlush(handle);
   FileClose(handle);
   PrintFormat("TradeMind risk exporter: account=%s open positions=%d",LoginText(),written);
   return true;
}

bool ExportSymbolSnapshot()
{
   string filename=SymbolsFilename();
   int handle=FileOpen(
      filename,
      FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE,
      ','
   );
   if(handle==INVALID_HANDLE)
   {
      PrintFormat("TradeMind risk exporter: symbol file open failed for %s, error=%d",filename,GetLastError());
      ResetLastError();
      return false;
   }

   FileWrite(
      handle,
      "time_msc","account_login","server","currency","symbol","digits","trade_mode","bid","ask",
      "tick_size","tick_value","tick_value_profit","tick_value_loss","volume_min","volume_max","volume_step",
      "contract_size","margin_initial","margin_maintenance","margin_buy_per_volume","margin_sell_per_volume","leverage"
   );

   long captured_msc=(long)TimeCurrent()*1000;
   int total=SymbolsTotal(true);
   int written=0;
   for(int index=0;index<total;index++)
   {
      string symbol=SymbolName(index,true);
      if(symbol=="" || !ListContainsString(InpSymbols,symbol))
         continue;

      MqlTick tick;
      if(!SymbolInfoTick(symbol,tick))
         continue;

      double buy_margin=0.0;
      double sell_margin=0.0;
      bool buy_margin_ok=OrderCalcMargin(ORDER_TYPE_BUY,symbol,1.0,tick.ask,buy_margin);
      bool sell_margin_ok=OrderCalcMargin(ORDER_TYPE_SELL,symbol,1.0,tick.bid,sell_margin);
      if(!buy_margin_ok)
         buy_margin=0.0;
      if(!sell_margin_ok)
         sell_margin=0.0;

      ENUM_SYMBOL_TRADE_MODE trade_mode=(ENUM_SYMBOL_TRADE_MODE)SymbolInfoInteger(symbol,SYMBOL_TRADE_MODE);
      int digits=(int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);
      FileWrite(
         handle,
         captured_msc,
         AccountInfoInteger(ACCOUNT_LOGIN),
         AccountInfoString(ACCOUNT_SERVER),
         AccountInfoString(ACCOUNT_CURRENCY),
         symbol,
         digits,
         EnumTail(EnumToString(trade_mode),"SYMBOL_TRADE_MODE_"),
         DoubleToString(tick.bid,digits),
         DoubleToString(tick.ask,digits),
         DoubleToString(SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_SIZE),digits),
         DoubleToString(SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_VALUE),8),
         DoubleToString(SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_VALUE_PROFIT),8),
         DoubleToString(SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_VALUE_LOSS),8),
         DoubleToString(SymbolInfoDouble(symbol,SYMBOL_VOLUME_MIN),8),
         DoubleToString(SymbolInfoDouble(symbol,SYMBOL_VOLUME_MAX),8),
         DoubleToString(SymbolInfoDouble(symbol,SYMBOL_VOLUME_STEP),8),
         DoubleToString(SymbolInfoDouble(symbol,SYMBOL_TRADE_CONTRACT_SIZE),8),
         DoubleToString(SymbolInfoDouble(symbol,SYMBOL_MARGIN_INITIAL),8),
         DoubleToString(SymbolInfoDouble(symbol,SYMBOL_MARGIN_MAINTENANCE),8),
         DoubleToString(buy_margin,8),
         DoubleToString(sell_margin,8),
         AccountInfoInteger(ACCOUNT_LEVERAGE)
      );
      written++;
   }

   FileFlush(handle);
   FileClose(handle);
   PrintFormat("TradeMind risk exporter: account=%s Market Watch symbols=%d",LoginText(),written);
   return true;
}

void Collect()
{
   bool account_ok=AppendAccountSnapshot();
   bool positions_ok=ExportPositionSnapshot();
   bool symbols_ok=ExportSymbolSnapshot();
   if(!account_ok || !positions_ok || !symbols_ok)
      Print("TradeMind risk exporter: one or more snapshot writes failed");
}

int OnInit()
{
   if(InpRefreshSeconds<10)
   {
      Print("TradeMind risk exporter: InpRefreshSeconds must be at least 10");
      return INIT_PARAMETERS_INCORRECT;
   }
   EventSetTimer(InpRefreshSeconds);
   Collect();
   Print("TradeMind MT5 Risk Snapshot Exporter v1.180 started. Read-only. No orders.");
   return INIT_SUCCEEDED;
}

void OnTimer()
{
   Collect();
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}
