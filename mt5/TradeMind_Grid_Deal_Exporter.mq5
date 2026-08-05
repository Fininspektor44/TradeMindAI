#property strict
#property version   "1.152"
#property description "Read-only forward monitor for TradeMind grid basket analytics"

input bool     InpForwardOnly       = true;
input datetime InpStartFrom         = 0;   // Set explicitly for an existing forward run; 0 = remember first attach time.
input int      InpHistoryDays       = 365; // Used only when InpForwardOnly=false.
input int      InpRefreshSeconds    = 60;
input string   InpOutputFolder      = "TradeMindAI";
input string   InpMagicFilter       = ""; // Comma-separated; blank = every non-zero magic.
input bool     InpIncludeManual     = false; // Manual entries. Manual exits of tracked robot positions are always retained.
input string   InpSymbols           = ""; // Comma-separated; blank = every symbol.
input string   InpMagicLabels       = ""; // Example: 445501=AOExtremum;992211=GridSafe

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

int FindTrackedPosition(const ulong &position_ids[],ulong position_id)
{
   int count=ArraySize(position_ids);
   for(int index=0;index<count;index++)
   {
      if(position_ids[index]==position_id)
         return index;
   }
   return -1;
}

void RegisterTrackedPosition(ulong &position_ids[],long &owner_magics[],ulong position_id,long owner_magic)
{
   if(position_id==0 || FindTrackedPosition(position_ids,position_id)>=0)
      return;

   int size=ArraySize(position_ids);
   ArrayResize(position_ids,size+1);
   ArrayResize(owner_magics,size+1);
   position_ids[size]=position_id;
   owner_magics[size]=owner_magic;
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

string LoginText()
{
   return IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
}

string DealsFilename()
{
   return InpOutputFolder+"\\grid_deals_"+LoginText()+".csv";
}

string PositionsFilename()
{
   return InpOutputFolder+"\\grid_positions_"+LoginText()+".csv";
}

string AccountFilename()
{
   return InpOutputFolder+"\\grid_account_"+LoginText()+".csv";
}

string ForwardStartKey()
{
   return "TradeMind.GridStart."+LoginText();
}

datetime ResolveStartTime()
{
   datetime now=TimeCurrent();
   if(!InpForwardOnly)
      return now-(datetime)(InpHistoryDays*86400);
   if(InpStartFrom>0)
      return InpStartFrom;

   string key=ForwardStartKey();
   if(GlobalVariableCheck(key))
      return (datetime)GlobalVariableGet(key);

   GlobalVariableSet(key,(double)now);
   return now;
}

bool ExportDeals(datetime start,datetime finish)
{
   if(!HistorySelect(start,finish))
   {
      PrintFormat("TradeMind grid monitor: HistorySelect failed, error=%d",GetLastError());
      ResetLastError();
      return false;
   }

   string filename=DealsFilename();
   int handle=FileOpen(
      filename,
      FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE,
      ','
   );
   if(handle==INVALID_HANDLE)
   {
      PrintFormat("TradeMind grid monitor: FileOpen failed for %s, error=%d",filename,GetLastError());
      ResetLastError();
      return false;
   }

   FileWrite(
      handle,
      "account_login","server","currency","monitor_start","ticket","order","position_id","time_msc",
      "symbol","magic","deal_magic","robot","deal_type","entry","volume","price","profit","commission","swap","fee",
      "comment","reason"
   );

   long login=AccountInfoInteger(ACCOUNT_LOGIN);
   string server=AccountInfoString(ACCOUNT_SERVER);
   string currency=AccountInfoString(ACCOUNT_CURRENCY);
   int total=HistoryDealsTotal();
   int exported=0;
   int related_exits=0;
   ulong tracked_positions[];
   long tracked_magics[];

   // First pass: remember positions opened by the selected robot/manual scope.
   // This lets the second pass retain a later manual or cross-magic close.
   for(int index=0;index<total;index++)
   {
      ulong ticket=HistoryDealGetTicket(index);
      if(ticket==0)
         continue;

      ENUM_DEAL_TYPE type=(ENUM_DEAL_TYPE)HistoryDealGetInteger(ticket,DEAL_TYPE);
      if(type!=DEAL_TYPE_BUY && type!=DEAL_TYPE_SELL)
         continue;

      ENUM_DEAL_ENTRY entry=(ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket,DEAL_ENTRY);
      if(entry!=DEAL_ENTRY_IN && entry!=DEAL_ENTRY_INOUT)
         continue;

      string symbol=HistoryDealGetString(ticket,DEAL_SYMBOL);
      long magic=HistoryDealGetInteger(ticket,DEAL_MAGIC);
      if(symbol=="" || !MagicAllowed(magic) || !ListContainsString(InpSymbols,symbol))
         continue;

      RegisterTrackedPosition(
         tracked_positions,
         tracked_magics,
         (ulong)HistoryDealGetInteger(ticket,DEAL_POSITION_ID),
         magic
      );
   }

   // Second pass: export selected entries and every exit that belongs to a
   // tracked position, even when that exit was manual or sent by another EA.
   for(int index=0;index<total;index++)
   {
      ulong ticket=HistoryDealGetTicket(index);
      if(ticket==0)
         continue;

      ENUM_DEAL_TYPE type=(ENUM_DEAL_TYPE)HistoryDealGetInteger(ticket,DEAL_TYPE);
      if(type!=DEAL_TYPE_BUY && type!=DEAL_TYPE_SELL)
         continue;

      ENUM_DEAL_ENTRY entry=(ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket,DEAL_ENTRY);
      if(entry!=DEAL_ENTRY_IN && entry!=DEAL_ENTRY_OUT && entry!=DEAL_ENTRY_INOUT && entry!=DEAL_ENTRY_OUT_BY)
         continue;

      string symbol=HistoryDealGetString(ticket,DEAL_SYMBOL);
      if(symbol=="" || !ListContainsString(InpSymbols,symbol))
         continue;

      long deal_magic=HistoryDealGetInteger(ticket,DEAL_MAGIC);
      ulong position_id=(ulong)HistoryDealGetInteger(ticket,DEAL_POSITION_ID);
      int owner_index=FindTrackedPosition(tracked_positions,position_id);
      bool is_exit=(entry==DEAL_ENTRY_OUT || entry==DEAL_ENTRY_OUT_BY || entry==DEAL_ENTRY_INOUT);
      bool selected_magic=MagicAllowed(deal_magic);
      bool related_exit=(is_exit && owner_index>=0);
      if(!selected_magic && !related_exit)
         continue;

      long strategy_magic=deal_magic;
      if(owner_index>=0)
         strategy_magic=tracked_magics[owner_index];
      if(related_exit && deal_magic!=strategy_magic)
         related_exits++;

      int digits=(int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);
      string type_name=EnumTail(EnumToString(type),"DEAL_TYPE_");
      string entry_name=EnumTail(EnumToString(entry),"DEAL_ENTRY_");
      ENUM_DEAL_REASON reason=(ENUM_DEAL_REASON)HistoryDealGetInteger(ticket,DEAL_REASON);
      string reason_name=EnumTail(EnumToString(reason),"DEAL_REASON_");

      FileWrite(
         handle,
         login,server,currency,(long)start,ticket,
         (ulong)HistoryDealGetInteger(ticket,DEAL_ORDER),
         position_id,
         (long)HistoryDealGetInteger(ticket,DEAL_TIME_MSC),
         symbol,strategy_magic,deal_magic,RobotLabel(strategy_magic),type_name,entry_name,
         DoubleToString(HistoryDealGetDouble(ticket,DEAL_VOLUME),2),
         DoubleToString(HistoryDealGetDouble(ticket,DEAL_PRICE),digits),
         DoubleToString(HistoryDealGetDouble(ticket,DEAL_PROFIT),2),
         DoubleToString(HistoryDealGetDouble(ticket,DEAL_COMMISSION),2),
         DoubleToString(HistoryDealGetDouble(ticket,DEAL_SWAP),2),
         DoubleToString(HistoryDealGetDouble(ticket,DEAL_FEE),2),
         HistoryDealGetString(ticket,DEAL_COMMENT),reason_name
      );
      exported++;
   }

   FileFlush(handle);
   FileClose(handle);
   PrintFormat(
      "TradeMind grid monitor: account=%s start=%s exported=%d deals related_exits=%d -> Common\\Files\\%s",
      LoginText(),TimeToString(start,TIME_DATE|TIME_MINUTES),exported,related_exits,filename
   );
   return true;
}

bool AppendAccountSnapshot(datetime start)
{
   string filename=AccountFilename();
   int handle=FileOpen(
      filename,
      FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE,
      ','
   );
   if(handle==INVALID_HANDLE)
   {
      PrintFormat("TradeMind grid monitor: account snapshot open failed for %s, error=%d",filename,GetLastError());
      ResetLastError();
      return false;
   }

   bool empty=(FileSize(handle)==0);
   FileSeek(handle,0,SEEK_END);
   if(empty)
      FileWrite(handle,"time_msc","account_login","server","currency","monitor_start","balance","equity","margin","free_margin","margin_level","open_positions");

   FileWrite(
      handle,
      (long)TimeCurrent()*1000,
      AccountInfoInteger(ACCOUNT_LOGIN),
      AccountInfoString(ACCOUNT_SERVER),
      AccountInfoString(ACCOUNT_CURRENCY),
      (long)start,
      DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE),2),
      DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY),2),
      DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN),2),
      DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE),2),
      DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL),2),
      PositionsTotal()
   );
   FileFlush(handle);
   FileClose(handle);
   return true;
}

bool AppendPositionSnapshots(datetime start)
{
   string filename=PositionsFilename();
   int handle=FileOpen(
      filename,
      FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE,
      ','
   );
   if(handle==INVALID_HANDLE)
   {
      PrintFormat("TradeMind grid monitor: position snapshot open failed for %s, error=%d",filename,GetLastError());
      ResetLastError();
      return false;
   }

   bool empty=(FileSize(handle)==0);
   FileSeek(handle,0,SEEK_END);
   if(empty)
   {
      FileWrite(
         handle,
         "time_msc","account_login","server","monitor_start","position_ticket","position_id","position_time_msc",
         "symbol","magic","robot","side","volume","open_price","current_price","sl","tp","profit","swap","comment"
      );
   }

   int written=0;
   int total=PositionsTotal();
   for(int index=0;index<total;index++)
   {
      ulong ticket=PositionGetTicket(index);
      if(ticket==0 || !PositionSelectByTicket(ticket))
         continue;

      string symbol=PositionGetString(POSITION_SYMBOL);
      long magic=PositionGetInteger(POSITION_MAGIC);
      if(symbol=="" || !MagicAllowed(magic) || !ListContainsString(InpSymbols,symbol))
         continue;

      int digits=(int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);
      ENUM_POSITION_TYPE type=(ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      string side=EnumTail(EnumToString(type),"POSITION_TYPE_");
      FileWrite(
         handle,
         (long)TimeCurrent()*1000,
         AccountInfoInteger(ACCOUNT_LOGIN),
         AccountInfoString(ACCOUNT_SERVER),
         (long)start,
         ticket,
         (ulong)PositionGetInteger(POSITION_IDENTIFIER),
         (long)PositionGetInteger(POSITION_TIME_MSC),
         symbol,magic,RobotLabel(magic),side,
         DoubleToString(PositionGetDouble(POSITION_VOLUME),2),
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
   PrintFormat("TradeMind grid monitor: account=%s position snapshots=%d",LoginText(),written);
   return true;
}

void Collect()
{
   datetime start=ResolveStartTime();
   datetime finish=TimeCurrent();
   ExportDeals(start,finish);
   AppendAccountSnapshot(start);
   AppendPositionSnapshots(start);
}

int OnInit()
{
   if(!InpForwardOnly && InpHistoryDays<1)
   {
      Print("TradeMind grid monitor: InpHistoryDays must be at least 1");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(InpRefreshSeconds<10)
   {
      Print("TradeMind grid monitor: InpRefreshSeconds must be at least 10");
      return INIT_PARAMETERS_INCORRECT;
   }

   EventSetTimer(InpRefreshSeconds);
   Collect();
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
