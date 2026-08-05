#property strict
#property version   "1.15"
#property description "Read-only MT5 deal exporter for TradeMind grid basket analytics"

input int    InpHistoryDays      = 365;
input int    InpRefreshSeconds   = 60;
input string InpOutputFolder     = "TradeMindAI";
input string InpOutputFile       = "grid_deals.csv";
input string InpMagicFilter      = ""; // Comma-separated; blank = every non-zero magic.
input bool   InpIncludeManual    = false;
input string InpSymbols          = ""; // Comma-separated; blank = every symbol.
input string InpMagicLabels      = ""; // Example: 445501=AOExtremum;992211=GridSafe

string TrimCopy(string value)
{
   StringTrimLeft(value);
   StringTrimRight(value);
   return value;
}

bool ListContainsLong(string list,long value)
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
      if(item!="" && (long)StringToInteger(item)==value)
         return true;
   }
   return false;
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

bool MagicAllowed(long magic)
{
   if(magic==0 && !InpIncludeManual)
      return false;
   return ListContainsLong(InpMagicFilter,magic);
}

string RobotLabel(long magic)
{
   string mappings=TrimCopy(InpMagicLabels);
   if(mappings=="")
      return "MAGIC_"+IntegerToString(magic);

   string items[];
   ushort separator=StringGetCharacter(";",0);
   int count=StringSplit(mappings,separator,items);
   for(int index=0;index<count;index++)
   {
      string item=TrimCopy(items[index]);
      int equals=StringFind(item,"=");
      if(equals<=0)
         continue;
      string left=TrimCopy(StringSubstr(item,0,equals));
      string right=TrimCopy(StringSubstr(item,equals+1));
      if((long)StringToInteger(left)==magic && right!="")
         return right;
   }
   return "MAGIC_"+IntegerToString(magic);
}

string EnumTail(string value,string prefix)
{
   StringReplace(value,prefix,"");
   return value;
}

bool ExportDeals()
{
   datetime finish=TimeCurrent();
   datetime start=finish-(datetime)(InpHistoryDays*86400);
   if(!HistorySelect(start,finish))
   {
      PrintFormat("TradeMind grid exporter: HistorySelect failed, error=%d",GetLastError());
      ResetLastError();
      return false;
   }

   string filename=InpOutputFolder+"\\"+InpOutputFile;
   int handle=FileOpen(
      filename,
      FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE,
      ','
   );
   if(handle==INVALID_HANDLE)
   {
      PrintFormat("TradeMind grid exporter: FileOpen failed for %s, error=%d",filename,GetLastError());
      ResetLastError();
      return false;
   }

   FileWrite(
      handle,
      "account_login",
      "server",
      "currency",
      "ticket",
      "order",
      "position_id",
      "time_msc",
      "symbol",
      "magic",
      "robot",
      "deal_type",
      "entry",
      "volume",
      "price",
      "profit",
      "commission",
      "swap",
      "fee",
      "comment",
      "reason"
   );

   long login=AccountInfoInteger(ACCOUNT_LOGIN);
   string server=AccountInfoString(ACCOUNT_SERVER);
   string currency=AccountInfoString(ACCOUNT_CURRENCY);
   int total=HistoryDealsTotal();
   int exported=0;

   for(int index=0;index<total;index++)
   {
      ulong ticket=HistoryDealGetTicket(index);
      if(ticket==0)
         continue;

      ENUM_DEAL_TYPE type=(ENUM_DEAL_TYPE)HistoryDealGetInteger(ticket,DEAL_TYPE);
      if(type!=DEAL_TYPE_BUY && type!=DEAL_TYPE_SELL)
         continue;

      ENUM_DEAL_ENTRY entry=(ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket,DEAL_ENTRY);
      if(entry!=DEAL_ENTRY_IN && entry!=DEAL_ENTRY_OUT &&
         entry!=DEAL_ENTRY_INOUT && entry!=DEAL_ENTRY_OUT_BY)
         continue;

      string symbol=HistoryDealGetString(ticket,DEAL_SYMBOL);
      long magic=HistoryDealGetInteger(ticket,DEAL_MAGIC);
      if(symbol=="" || !MagicAllowed(magic) || !ListContainsString(InpSymbols,symbol))
         continue;

      int digits=(int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);
      string type_name=EnumTail(EnumToString(type),"DEAL_TYPE_");
      string entry_name=EnumTail(EnumToString(entry),"DEAL_ENTRY_");
      ENUM_DEAL_REASON reason=(ENUM_DEAL_REASON)HistoryDealGetInteger(ticket,DEAL_REASON);
      string reason_name=EnumTail(EnumToString(reason),"DEAL_REASON_");

      FileWrite(
         handle,
         login,
         server,
         currency,
         ticket,
         (ulong)HistoryDealGetInteger(ticket,DEAL_ORDER),
         (ulong)HistoryDealGetInteger(ticket,DEAL_POSITION_ID),
         (long)HistoryDealGetInteger(ticket,DEAL_TIME_MSC),
         symbol,
         magic,
         RobotLabel(magic),
         type_name,
         entry_name,
         DoubleToString(HistoryDealGetDouble(ticket,DEAL_VOLUME),2),
         DoubleToString(HistoryDealGetDouble(ticket,DEAL_PRICE),digits),
         DoubleToString(HistoryDealGetDouble(ticket,DEAL_PROFIT),2),
         DoubleToString(HistoryDealGetDouble(ticket,DEAL_COMMISSION),2),
         DoubleToString(HistoryDealGetDouble(ticket,DEAL_SWAP),2),
         DoubleToString(HistoryDealGetDouble(ticket,DEAL_FEE),2),
         HistoryDealGetString(ticket,DEAL_COMMENT),
         reason_name
      );
      exported++;
   }

   FileFlush(handle);
   FileClose(handle);
   PrintFormat(
      "TradeMind grid exporter: exported %d deals to Common\\Files\\%s",
      exported,
      filename
   );
   return true;
}

int OnInit()
{
   if(InpHistoryDays<1)
   {
      Print("TradeMind grid exporter: InpHistoryDays must be at least 1");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(InpRefreshSeconds<10)
   {
      Print("TradeMind grid exporter: InpRefreshSeconds must be at least 10");
      return INIT_PARAMETERS_INCORRECT;
   }

   EventSetTimer(InpRefreshSeconds);
   ExportDeals();
   return INIT_SUCCEEDED;
}

void OnTimer()
{
   ExportDeals();
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}
