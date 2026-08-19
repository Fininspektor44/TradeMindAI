#property strict
#property version   "1.3"
#property description "TradeMind SER8 Demo Order Executor v1.3"
#property description "EXECUTOR ONLY. Reads at most one pending SER8 order"
#property description "request per timer tick, independently verifies the"
#property description "account/login and request identity, sends exactly"
#property description "one order via CTrade, and writes exactly one result"
#property description "row. No grid, no averaging, no martingale, no"
#property description "position sizing, and no signal generation of any"
#property description "kind happen in this file -- SER8 remains the sole"
#property description "trading authority; this EA only carries out an"
#property description "already-decided, already-authorized request."
#property description "v1.1 additionally performs the SAME read-only"
#property description "account/positions/Market-Watch risk snapshot export"
#property description "TradeMind_MT5_Risk_Snapshot_Exporter.mq5 already"
#property description "performs, on its own independently configurable"
#property description "timer cadence, so ONE EA on ONE chart is sufficient"
#property description "for the SER8 demo path -- a second EA is no longer"
#property description "required. The snapshot export is unconditionally"
#property description "read-only: it never opens, modifies, or closes a"
#property description "position, and it shares no state with order"
#property description "execution -- a failed snapshot write never"
#property description "authorizes an order, and a rejected/failed order"
#property description "never skips a future snapshot."
#property description "v1.2 fixes the positions-snapshot writer: it now"
#property description "writes to a temp file and atomically renames it"
#property description "over the real file, so a concurrent reader can"
#property description "never observe a truncated or zero-filled positions"
#property description "CSV. No other writer, no order-execution behavior,"
#property description "and no sizing/grid/martingale logic changed."
#property description "v1.3 adds two more read-only exports on the SAME"
#property description "risk-refresh timer: mt5_risk_orders_utc_<login>.csv"
#property description "(every order this account's magic owns, active or"
#property description "historical, with its ENUM_ORDER_STATE) and"
#property description "mt5_risk_deals_utc_<login>.csv (matching deal"
#property description "history, DEAL_ORDER-linked back to its order). This"
#property description "is the authoritative evidence SER8's own automatic"
#property description "MT5 reconciliation layer needs to know whether a"
#property description "pending LIMIT/STOP order has since filled, been"
#property description "cancelled, expired, or rejected -- still strictly"
#property description "read-only, still no OrderSend/CTrade call anywhere"
#property description "in either export function."

#include <Trade\Trade.mqh>

//--- Order-send executor inputs (unchanged from v1.0).
input int    InpPollSeconds       = 5;     // How often pending SER8 order requests are checked.
input string InpOutputFolder      = "TradeMindAI";
input long   InpApprovedDemoLogin = 0;     // 0 = no extra pin beyond the request's own demo_account_id match; set explicitly to also hard-pin one terminal login.
input int    InpMagicNumber       = 990244; // Must match trademind.ser8_mt5_demo_order_send.DEMO_EXECUTOR_MAGIC_NUMBER.
input int    InpDeviationPoints   = 20;

//--- Read-only risk snapshot inputs (same names/semantics/defaults as the
//--- standalone TradeMind_MT5_Risk_Snapshot_Exporter.mq5).
input int    InpRiskRefreshSeconds = 30;    // Independent of InpPollSeconds; must be >= 10.
input string InpSymbols            = "";    // Optional Market Watch filter. Blank = every selected symbol.

//--- Read-only order/deal history inputs (v1.3). Bounds how far back
//--- ExportOrderHistorySnapshot/ExportDealHistorySnapshot scan on every
//--- refresh -- a small, fast, bounded read every cycle, never an
//--- unbounded full-account history scan.
input int    InpHistoryLookbackDays = 30;   // Independent of InpRiskRefreshSeconds; must be >= 1.

CTrade trade;

//--- Runs both cadences off exactly ONE EventSetTimer -- fires every
//--- InpBaseTickSeconds (the finer of the two configured intervals), and
//--- each of the two independent jobs below only actually does work once
//--- its own elapsed time has passed. This is the only timer this EA ever
//--- registers.
datetime g_last_order_poll_at    = 0;
datetime g_last_risk_snapshot_at = 0;

//--- Retcode used by the Python side to recognize a clean fill.
#define SER8_RETCODE_MALFORMED_REQUEST -1

string LoginText()
{
   return IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
}

long UtcNowMsc()
{
   return (long)TimeGMT()*1000;
}

string RequestFilename()
{
   return InpOutputFolder+"\\ser8_demo_order_request_"+LoginText()+".csv";
}

string RequestConsumedFilename()
{
   return InpOutputFolder+"\\ser8_demo_order_request_"+LoginText()+".consumed";
}

string ResultFilename()
{
   return InpOutputFolder+"\\ser8_demo_order_result_"+LoginText()+".csv";
}

void WriteResult(
   string claim_id,
   string demo_account_id,
   string symbol,
   int retcode,
   string retcode_description,
   string order_ticket,
   string deal_ticket,
   string position_ticket,
   string filled_volume,
   string filled_price)
{
   int handle=FileOpen(
      ResultFilename(),
      FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ,
      ',');
   if(handle==INVALID_HANDLE)
   {
      Print("TradeMind demo executor: could not open result file for writing, error=",GetLastError());
      return;
   }
   FileWrite(
      handle,
      "claim_id","demo_account_id","symbol","retcode","retcode_description",
      "order_ticket","deal_ticket","position_ticket","filled_volume","filled_price");
   FileWrite(
      handle,
      claim_id,demo_account_id,symbol,IntegerToString(retcode),retcode_description,
      order_ticket,deal_ticket,position_ticket,filled_volume,filled_price);
   FileClose(handle);
}

void WriteMalformedResult(string claim_id,string demo_account_id,string symbol)
{
   WriteResult(claim_id,demo_account_id,symbol,SER8_RETCODE_MALFORMED_REQUEST,
      "REJECTED_BY_EXECUTOR",  "", "", "", "", "");
}

//--- Reads and immediately consumes (renames) any pending request file.
//--- Returns false if no request is pending or the file could not be read.
bool ReadAndConsumeRequest(
   string &claim_id,
   string &authorization_id,
   string &demo_account_id,
   string &symbol,
   string &action,
   string &order_type,
   double &volume,
   double &price,
   double &sl,
   double &tp,
   int    &magic,
   string &comment,
   string &request_hash)
{
   string filename=RequestFilename();
   if(!FileIsExist(filename,FILE_COMMON))
      return false;

   int handle=FileOpen(filename,FILE_READ|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ,',');
   if(handle==INVALID_HANDLE)
   {
      Print("TradeMind demo executor: could not open request file, error=",GetLastError());
      return false;
   }

   // Skip the 13-column header row.
   for(int i=0;i<13 && !FileIsEnding(handle);i++)
      FileReadString(handle);

   bool ok=true;
   if(FileIsEnding(handle))
      ok=false;
   else
   {
      claim_id         = FileReadString(handle);
      authorization_id = FileReadString(handle);
      demo_account_id  = FileReadString(handle);
      symbol           = FileReadString(handle);
      action           = FileReadString(handle);
      order_type       = FileReadString(handle);
      volume           = StringToDouble(FileReadString(handle));
      price            = StringToDouble(FileReadString(handle));
      sl               = StringToDouble(FileReadString(handle));
      tp               = StringToDouble(FileReadString(handle));
      magic            = (int)StringToInteger(FileReadString(handle));
      comment          = FileReadString(handle);
      request_hash     = FileReadString(handle);
   }
   FileClose(handle);

   // Consume the request file immediately, unconditionally, so it can
   // never be re-read on a later timer tick regardless of what happens
   // next (this is the file-level half of the one-shot guard; the
   // authoritative one-shot guard itself lives on the SER8/Python side).
   FileMove(filename,FILE_COMMON,RequestConsumedFilename(),FILE_COMMON|FILE_REWRITE);

   if(!ok || claim_id=="" || demo_account_id=="" || symbol=="")
      return false;

   return true;
}

//--- Volume is READ ONLY from the already-authorized request -- this
//--- function (and this entire file) never computes, sizes, or overrides a
//--- lot size. SER8/Python's RiskDecision.SizedOrder remains the sole
//--- source of the trade volume; this executor only carries it out.
void ProcessPendingRequest()
{
   string claim_id,authorization_id,demo_account_id,symbol,action,order_type,comment,request_hash;
   double volume,price,sl,tp;
   int magic;

   if(!ReadAndConsumeRequest(
         claim_id,authorization_id,demo_account_id,symbol,action,order_type,
         volume,price,sl,tp,magic,comment,request_hash))
      return;

   // Independent identity verification -- SER8 already checked this on
   // its own side, but this executor never trusts that at face value.
   if(demo_account_id!=LoginText())
   {
      WriteMalformedResult(claim_id,demo_account_id,symbol);
      Print("TradeMind demo executor: request account ",demo_account_id,
            " does not match this terminal's login ",LoginText(),"; rejected.");
      return;
   }
   if(InpApprovedDemoLogin!=0 && AccountInfoInteger(ACCOUNT_LOGIN)!=InpApprovedDemoLogin)
   {
      WriteMalformedResult(claim_id,demo_account_id,symbol);
      Print("TradeMind demo executor: this terminal's login is not the explicitly approved demo login; rejected.");
      return;
   }
   if(magic!=InpMagicNumber)
   {
      WriteMalformedResult(claim_id,demo_account_id,symbol);
      Print("TradeMind demo executor: request magic ",magic," does not match InpMagicNumber; rejected.");
      return;
   }
   if(!SymbolSelect(symbol,true))
   {
      WriteMalformedResult(claim_id,demo_account_id,symbol);
      Print("TradeMind demo executor: symbol ",symbol," is unavailable in Market Watch; rejected.");
      return;
   }
   if(volume<=0 || sl<=0 || tp<=0)
   {
      WriteMalformedResult(claim_id,demo_account_id,symbol);
      Print("TradeMind demo executor: request volume/sl/tp are not all positive; rejected.");
      return;
   }
   if(action!="BUY" && action!="SELL")
   {
      WriteMalformedResult(claim_id,demo_account_id,symbol);
      Print("TradeMind demo executor: unsupported action ",action,"; rejected.");
      return;
   }

   trade.SetExpertMagicNumber(InpMagicNumber);
   trade.SetDeviationInPoints(InpDeviationPoints);

   bool sent=false;
   if(order_type=="MARKET")
   {
      if(action=="BUY")
         sent=trade.Buy(volume,symbol,0.0,sl,tp,comment);
      else
         sent=trade.Sell(volume,symbol,0.0,sl,tp,comment);
   }
   else if(order_type=="LIMIT")
   {
      if(action=="BUY")
         sent=trade.BuyLimit(volume,price,symbol,sl,tp,ORDER_TIME_GTC,0,comment);
      else
         sent=trade.SellLimit(volume,price,symbol,sl,tp,ORDER_TIME_GTC,0,comment);
   }
   else if(order_type=="STOP")
   {
      if(action=="BUY")
         sent=trade.BuyStop(volume,price,symbol,sl,tp,ORDER_TIME_GTC,0,comment);
      else
         sent=trade.SellStop(volume,price,symbol,sl,tp,ORDER_TIME_GTC,0,comment);
   }
   else
   {
      WriteMalformedResult(claim_id,demo_account_id,symbol);
      Print("TradeMind demo executor: unsupported order_type ",order_type,"; rejected.");
      return;
   }

   uint retcode=trade.ResultRetcode();
   string retcode_description=trade.ResultRetcodeDescription();
   ulong order_ticket=trade.ResultOrder();
   ulong deal_ticket=trade.ResultDeal();
   double filled_volume=trade.ResultVolume();
   double filled_price=trade.ResultPrice();

   WriteResult(
      claim_id,demo_account_id,symbol,(int)retcode,retcode_description,
      IntegerToString(order_ticket),IntegerToString(deal_ticket),"",
      DoubleToString(filled_volume,8),DoubleToString(filled_price,8));

   Print("TradeMind demo executor: processed claim ",claim_id,
         " sent=",sent," retcode=",retcode," (",retcode_description,")");
}

//===========================================================================
// Read-only risk snapshot export -- merged verbatim (same schema, same
// filenames, same columns) from TradeMind_MT5_Risk_Snapshot_Exporter.mq5.
// Nothing below this point ever calls CTrade, OrderSend, or any other
// trading function; it only reads account/position/symbol state and writes
// CSV rows for the Python SER8 Risk Manager to read. A failure here (e.g. a
// file-open error) is logged and skipped -- it never touches, blocks, or
// implicitly authorizes any order request, and a failed/rejected order
// request above never prevents this snapshot from running on its own
// schedule.
//===========================================================================

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

string RiskAccountFilename()
{
   return InpOutputFolder+"\\mt5_risk_account_utc_"+LoginText()+".csv";
}

string RiskPositionsFilename()
{
   return InpOutputFolder+"\\mt5_risk_positions_utc_"+LoginText()+".csv";
}

//--- Temp filename for the atomic write-then-rename pattern
//--- ExportPositionSnapshot uses below -- see that function's own comment
//--- for why. Reuses exactly the same FileMove(...,FILE_REWRITE) mechanism
//--- ReadAndConsumeRequest already uses (above) to rename a request file
//--- after reading it; this is not a new/parallel snapshot path.
string RiskPositionsTempFilename()
{
   return InpOutputFolder+"\\mt5_risk_positions_utc_"+LoginText()+".csv.tmp";
}

string RiskSymbolsFilename()
{
   return InpOutputFolder+"\\mt5_risk_symbols_utc_"+LoginText()+".csv";
}

string RiskOrdersFilename()
{
   return InpOutputFolder+"\\mt5_risk_orders_utc_"+LoginText()+".csv";
}

string RiskOrdersTempFilename()
{
   return InpOutputFolder+"\\mt5_risk_orders_utc_"+LoginText()+".csv.tmp";
}

string RiskDealsFilename()
{
   return InpOutputFolder+"\\mt5_risk_deals_utc_"+LoginText()+".csv";
}

string RiskDealsTempFilename()
{
   return InpOutputFolder+"\\mt5_risk_deals_utc_"+LoginText()+".csv.tmp";
}

bool AppendAccountSnapshot()
{
   string filename=RiskAccountFilename();
   int handle=FileOpen(
      filename,
      FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE,
      ','
   );
   if(handle==INVALID_HANDLE)
   {
      PrintFormat("TradeMind demo executor: risk account file open failed for %s, error=%d",filename,GetLastError());
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
      UtcNowMsc(),
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

//--- ROOT CAUSE (confirmed by direct source comparison with
//--- AppendAccountSnapshot, not assumed): unlike the account file (which
//--- APPENDS one new row to an already-complete, mostly-unchanged file),
//--- this function REWRITES THE ENTIRE positions file from scratch on
//--- every call -- by design, since a positions snapshot is a point-in-
//--- time view, not a growing log. Opening the REAL, well-known filename
//--- directly with FILE_WRITE (no FILE_READ) and rewriting it in place is
//--- NOT atomic: for a real, unbounded window between FileOpen (which
//--- truncates the existing file) and FileClose (once the new content is
//--- fully written and flushed), the SAME filename the Python side
//--- (trademind.mt5_risk_adapter) polls and reads concurrently can be
//--- observed truncated, empty, or -- as directly reproduced on Windows --
//--- zero-filled (162 bytes of 0x00, exactly the length of the header row
//--- alone), because Windows/NTFS can make the file's new, extended length
//--- visible to another process before the corresponding bytes have
//--- actually been written. The Python reader correctly, deliberately
//--- fails closed on that content ("missing fields: ...") -- it is not
//--- being weakened by this fix; the write path is made atomic instead.
//---
//--- FIX: write the complete new snapshot to a TEMP filename first, flush
//--- and close it there, then atomically replace the real filename in one
//--- step via FileMove(...,FILE_REWRITE) -- the SAME rename-based
//--- mechanism ReadAndConsumeRequest already uses above for the one-shot
//--- request file, not a new/parallel snapshot path. A concurrent reader
//--- of the REAL filename can now only ever see the complete PREVIOUS
//--- snapshot or the complete NEW one -- never a partial, empty, or
//--- zero-filled state.
bool ExportPositionSnapshot()
{
   string filename=RiskPositionsFilename();
   string temp_filename=RiskPositionsTempFilename();

   // Clear any stale temp file left over from a prior aborted write, so
   // the temp file always starts clean.
   if(FileIsExist(temp_filename,FILE_COMMON))
      FileDelete(temp_filename,FILE_COMMON);

   int handle=FileOpen(
      temp_filename,
      FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE,
      ','
   );
   if(handle==INVALID_HANDLE)
   {
      PrintFormat("TradeMind demo executor: risk position temp file open failed for %s, error=%d",temp_filename,GetLastError());
      ResetLastError();
      return false;
   }

   // Header row is ALWAYS written, even with zero open positions -- a
   // valid header-only CSV, never a zero-length or NUL-filled file.
   FileWrite(
      handle,
      "time_msc","account_login","server","currency","position_ticket","position_id","position_time_msc",
      "symbol","magic","side","volume","open_price","current_price","sl","tp","profit","swap","comment"
   );

   long captured_msc=UtcNowMsc();
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

   // Atomic replace -- the real filename only ever transitions directly
   // from "previous complete snapshot" to "this complete snapshot".
   if(!FileMove(temp_filename,FILE_COMMON,filename,FILE_COMMON|FILE_REWRITE))
   {
      PrintFormat("TradeMind demo executor: risk position atomic replace failed for %s, error=%d",filename,GetLastError());
      ResetLastError();
      FileDelete(temp_filename,FILE_COMMON);
      return false;
   }

   PrintFormat("TradeMind demo executor: risk snapshot account=%s open positions=%d",LoginText(),written);
   return true;
}

bool ExportSymbolSnapshot()
{
   string filename=RiskSymbolsFilename();
   int handle=FileOpen(
      filename,
      FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE,
      ','
   );
   if(handle==INVALID_HANDLE)
   {
      PrintFormat("TradeMind demo executor: risk symbol file open failed for %s, error=%d",filename,GetLastError());
      ResetLastError();
      return false;
   }

   FileWrite(
      handle,
      "time_msc","account_login","server","currency","symbol","digits","trade_mode","bid","ask",
      "tick_size","tick_value","tick_value_profit","tick_value_loss","volume_min","volume_max","volume_step",
      "contract_size","margin_initial","margin_maintenance","margin_buy_per_volume","margin_sell_per_volume","leverage"
   );

   long captured_msc=UtcNowMsc();
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
   PrintFormat("TradeMind demo executor: risk snapshot account=%s Market Watch symbols=%d",LoginText(),written);
   return true;
}

//--- v1.3: the ONLY authoritative source this repository has for whether
//--- a specific order_ticket is still a genuinely working pending order
//--- or has reached a terminal broker outcome (FILLED/CANCELED/EXPIRED/
//--- REJECTED). Neither the account, positions, nor symbols snapshot can
//--- answer that -- a position row carries its OWN position_ticket, never
//--- the order_ticket of the pending order that created it, and two
//--- same-symbol/same-side pending LIMIT legs (a real, intentional SER8
//--- multi-entry shape) are indistinguishable by symbol/side/volume
//--- alone. Filtered to this EA's own InpMagicNumber only -- never
//--- exports another EA's/account's orders. Strictly read-only: no
//--- OrderSend/CTrade call anywhere in this function. Written atomically
//--- (temp file + FileMove), exactly like ExportPositionSnapshot.
bool ExportOrderHistorySnapshot()
{
   string filename=RiskOrdersFilename();
   string temp_filename=RiskOrdersTempFilename();
   if(FileIsExist(temp_filename,FILE_COMMON))
      FileDelete(temp_filename,FILE_COMMON);

   int handle=FileOpen(
      temp_filename,
      FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE,
      ','
   );
   if(handle==INVALID_HANDLE)
   {
      PrintFormat("TradeMind demo executor: risk order temp file open failed for %s, error=%d",temp_filename,GetLastError());
      ResetLastError();
      return false;
   }

   FileWrite(
      handle,
      "time_msc","account_login","order_ticket","symbol","magic","side","order_type","volume",
      "price","state","time_setup_msc","time_done_msc","position_id"
   );

   long captured_msc=UtcNowMsc();
   int written=0;

   // Currently ACTIVE (still working, not yet terminal) orders --
   // OrdersTotal()/OrderGetTicket() is the real-time list of orders that
   // have not yet reached a terminal state. OrderGetTicket already
   // selects the order for the OrderGet*() calls that follow, exactly
   // like PositionGetTicket already does above for positions.
   int active_total=OrdersTotal();
   for(int index=0;index<active_total;index++)
   {
      ulong ticket=OrderGetTicket(index);
      if(ticket==0)
         continue;
      if(OrderGetInteger(ORDER_MAGIC)!=InpMagicNumber)
         continue;
      string symbol=OrderGetString(ORDER_SYMBOL);
      if(symbol=="")
         continue;
      int digits=(int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);
      ENUM_ORDER_TYPE order_type=(ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      string side=(order_type==ORDER_TYPE_SELL||order_type==ORDER_TYPE_SELL_LIMIT||order_type==ORDER_TYPE_SELL_STOP||order_type==ORDER_TYPE_SELL_STOP_LIMIT) ? "SELL" : "BUY";
      FileWrite(
         handle,
         captured_msc,
         AccountInfoInteger(ACCOUNT_LOGIN),
         ticket,
         symbol,
         OrderGetInteger(ORDER_MAGIC),
         side,
         EnumTail(EnumToString(order_type),"ORDER_TYPE_"),
         DoubleToString(OrderGetDouble(ORDER_VOLUME_INITIAL),8),
         DoubleToString(OrderGetDouble(ORDER_PRICE_OPEN),digits),
         "PLACED",
         (long)OrderGetInteger(ORDER_TIME_SETUP_MSC),
         0,
         0
      );
      written++;
   }

   // Historical (completed/terminal) orders within the bounded lookback
   // window -- HistorySelect populates a combined list that also
   // includes currently-active orders in range; ORDER_STATE tells the
   // real outcome, and PLACED/STARTED rows are skipped here since the
   // OrdersTotal() loop above already reported them, avoiding a
   // duplicate row for the same order_ticket.
   datetime from=TimeGMT()-InpHistoryLookbackDays*86400;
   if(HistorySelect(from,TimeGMT()))
   {
      int history_total=HistoryOrdersTotal();
      for(int index=0;index<history_total;index++)
      {
         ulong ticket=HistoryOrderGetTicket(index);
         if(ticket==0)
            continue;
         if(HistoryOrderGetInteger(ticket,ORDER_MAGIC)!=InpMagicNumber)
            continue;
         ENUM_ORDER_STATE state=(ENUM_ORDER_STATE)HistoryOrderGetInteger(ticket,ORDER_STATE);
         if(state==ORDER_STATE_STARTED || state==ORDER_STATE_PLACED)
            continue;
         string symbol=HistoryOrderGetString(ticket,ORDER_SYMBOL);
         if(symbol=="")
            continue;
         int digits=(int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);
         ENUM_ORDER_TYPE order_type=(ENUM_ORDER_TYPE)HistoryOrderGetInteger(ticket,ORDER_TYPE);
         string side=(order_type==ORDER_TYPE_SELL||order_type==ORDER_TYPE_SELL_LIMIT||order_type==ORDER_TYPE_SELL_STOP||order_type==ORDER_TYPE_SELL_STOP_LIMIT) ? "SELL" : "BUY";
         FileWrite(
            handle,
            captured_msc,
            AccountInfoInteger(ACCOUNT_LOGIN),
            ticket,
            symbol,
            HistoryOrderGetInteger(ticket,ORDER_MAGIC),
            side,
            EnumTail(EnumToString(order_type),"ORDER_TYPE_"),
            DoubleToString(HistoryOrderGetDouble(ticket,ORDER_VOLUME_INITIAL),8),
            DoubleToString(HistoryOrderGetDouble(ticket,ORDER_PRICE_OPEN),digits),
            EnumTail(EnumToString(state),"ORDER_STATE_"),
            (long)HistoryOrderGetInteger(ticket,ORDER_TIME_SETUP_MSC),
            (long)HistoryOrderGetInteger(ticket,ORDER_TIME_DONE_MSC),
            (long)HistoryOrderGetInteger(ticket,ORDER_POSITION_ID)
         );
         written++;
      }
   }

   FileFlush(handle);
   FileClose(handle);

   if(!FileMove(temp_filename,FILE_COMMON,filename,FILE_COMMON|FILE_REWRITE))
   {
      PrintFormat("TradeMind demo executor: risk order atomic replace failed for %s, error=%d",filename,GetLastError());
      ResetLastError();
      FileDelete(temp_filename,FILE_COMMON);
      return false;
   }

   PrintFormat("TradeMind demo executor: risk order snapshot account=%s orders=%d",LoginText(),written);
   return true;
}

//--- v1.3: matching deal history for the same bounded lookback window --
//--- DEAL_ORDER directly gives the ORIGINATING order ticket for an
//--- executed deal (the authoritative link
//--- ExportOrderHistorySnapshot's own FILLED rows alone cannot provide),
//--- plus the real fill price/volume/deal_ticket/position_id. Filtered
//--- to InpMagicNumber only. Strictly read-only. Written atomically,
//--- exactly like ExportOrderHistorySnapshot.
bool ExportDealHistorySnapshot()
{
   string filename=RiskDealsFilename();
   string temp_filename=RiskDealsTempFilename();
   if(FileIsExist(temp_filename,FILE_COMMON))
      FileDelete(temp_filename,FILE_COMMON);

   int handle=FileOpen(
      temp_filename,
      FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE,
      ','
   );
   if(handle==INVALID_HANDLE)
   {
      PrintFormat("TradeMind demo executor: risk deal temp file open failed for %s, error=%d",temp_filename,GetLastError());
      ResetLastError();
      return false;
   }

   FileWrite(
      handle,
      "time_msc","account_login","deal_ticket","order_ticket","position_id","symbol","magic",
      "side","volume","price","entry","time_deal_msc"
   );

   long captured_msc=UtcNowMsc();
   int written=0;
   datetime from=TimeGMT()-InpHistoryLookbackDays*86400;
   if(HistorySelect(from,TimeGMT()))
   {
      int total=HistoryDealsTotal();
      for(int index=0;index<total;index++)
      {
         ulong ticket=HistoryDealGetTicket(index);
         if(ticket==0)
            continue;
         if(HistoryDealGetInteger(ticket,DEAL_MAGIC)!=InpMagicNumber)
            continue;
         string symbol=HistoryDealGetString(ticket,DEAL_SYMBOL);
         if(symbol=="")
            continue;
         int digits=(int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);
         ENUM_DEAL_TYPE deal_type=(ENUM_DEAL_TYPE)HistoryDealGetInteger(ticket,DEAL_TYPE);
         string side=(deal_type==DEAL_TYPE_SELL) ? "SELL" : (deal_type==DEAL_TYPE_BUY) ? "BUY" : "OTHER";
         ENUM_DEAL_ENTRY entry=(ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket,DEAL_ENTRY);
         FileWrite(
            handle,
            captured_msc,
            AccountInfoInteger(ACCOUNT_LOGIN),
            ticket,
            (ulong)HistoryDealGetInteger(ticket,DEAL_ORDER),
            (ulong)HistoryDealGetInteger(ticket,DEAL_POSITION_ID),
            symbol,
            HistoryDealGetInteger(ticket,DEAL_MAGIC),
            side,
            DoubleToString(HistoryDealGetDouble(ticket,DEAL_VOLUME),8),
            DoubleToString(HistoryDealGetDouble(ticket,DEAL_PRICE),digits),
            EnumTail(EnumToString(entry),"DEAL_ENTRY_"),
            (long)HistoryDealGetInteger(ticket,DEAL_TIME_MSC)
         );
         written++;
      }
   }

   FileFlush(handle);
   FileClose(handle);

   if(!FileMove(temp_filename,FILE_COMMON,filename,FILE_COMMON|FILE_REWRITE))
   {
      PrintFormat("TradeMind demo executor: risk deal atomic replace failed for %s, error=%d",filename,GetLastError());
      ResetLastError();
      FileDelete(temp_filename,FILE_COMMON);
      return false;
   }

   PrintFormat("TradeMind demo executor: risk deal snapshot account=%s deals=%d",LoginText(),written);
   return true;
}

//--- Collects all five read-only snapshots. Each export function is
//--- independent and unconditional -- one failing (logged only) never
//--- skips the others, and this function's own success/failure never
//--- feeds back into ProcessPendingRequest() in any way.
void CollectRiskSnapshot()
{
   bool account_ok=AppendAccountSnapshot();
   bool positions_ok=ExportPositionSnapshot();
   bool symbols_ok=ExportSymbolSnapshot();
   bool orders_ok=ExportOrderHistorySnapshot();
   bool deals_ok=ExportDealHistorySnapshot();
   if(!account_ok || !positions_ok || !symbols_ok || !orders_ok || !deals_ok)
      Print("TradeMind demo executor: one or more risk snapshot writes failed");
}

int OnInit()
{
   if(InpPollSeconds<1)
   {
      Print("TradeMind demo executor: InpPollSeconds must be at least 1");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(InpRiskRefreshSeconds<10)
   {
      Print("TradeMind demo executor: InpRiskRefreshSeconds must be at least 10");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(InpHistoryLookbackDays<1)
   {
      Print("TradeMind demo executor: InpHistoryLookbackDays must be at least 1");
      return INIT_PARAMETERS_INCORRECT;
   }
   // Exactly ONE timer for this entire EA. Both independent cadences
   // (order-request polling at InpPollSeconds, risk snapshot refresh at
   // InpRiskRefreshSeconds) are driven off this single timer's ticks via
   // their own separately-tracked elapsed-time checks in OnTimer() -- see
   // g_last_order_poll_at / g_last_risk_snapshot_at below. The timer fires
   // at whichever of the two configured intervals is finer, so neither
   // cadence is ever coarsened by the other.
   int base_seconds=MathMin(InpPollSeconds,InpRiskRefreshSeconds);
   EventSetTimer(base_seconds);
   g_last_order_poll_at=0;
   g_last_risk_snapshot_at=0;
   CollectRiskSnapshot();
   g_last_risk_snapshot_at=TimeGMT();
   Print("TradeMind SER8 Demo Order Executor v1.3 started. One EA, one chart: SER8 one-shot order execution "
         "plus read-only risk/position/order/deal snapshot export. No strategy logic, no position sizing of "
         "any kind runs here.");
   return INIT_SUCCEEDED;
}

void OnTimer()
{
   datetime now=TimeGMT();

   // Order-request polling -- unconditional, independent of snapshot outcome.
   if(now-g_last_order_poll_at>=InpPollSeconds)
   {
      g_last_order_poll_at=now;
      ProcessPendingRequest();
   }

   // Risk snapshot refresh -- unconditional, independent of order outcome.
   if(now-g_last_risk_snapshot_at>=InpRiskRefreshSeconds)
   {
      g_last_risk_snapshot_at=now;
      CollectRiskSnapshot();
   }
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}
