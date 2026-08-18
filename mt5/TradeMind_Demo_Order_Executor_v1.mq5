#property strict
#property version   "1.0"
#property description "TradeMind SER8 Demo Order Executor v1.0"
#property description "EXECUTOR ONLY. Reads at most one pending SER8 order"
#property description "request per timer tick, independently verifies the"
#property description "account/login and request identity, sends exactly"
#property description "one order via CTrade, and writes exactly one result"
#property description "row. No grid, no averaging, no martingale, no"
#property description "position sizing, and no signal generation of any"
#property description "kind happen in this file -- SER8 remains the sole"
#property description "trading authority; this EA only carries out an"
#property description "already-decided, already-authorized request."

#include <Trade\Trade.mqh>

input int    InpPollSeconds       = 5;     // How often OnTimer checks for a pending request.
input string InpOutputFolder      = "TradeMindAI";
input long   InpApprovedDemoLogin = 0;     // 0 = no extra pin beyond the request's own demo_account_id match; set explicitly to also hard-pin one terminal login.
input int    InpMagicNumber       = 990244; // Must match trademind.ser8_mt5_demo_order_send.DEMO_EXECUTOR_MAGIC_NUMBER.
input int    InpDeviationPoints   = 20;

CTrade trade;

//--- Retcode used by the Python side to recognize a clean fill.
#define SER8_RETCODE_MALFORMED_REQUEST -1

string LoginText()
{
   return IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
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

int OnInit()
{
   if(InpPollSeconds<1)
   {
      Print("TradeMind demo executor: InpPollSeconds must be at least 1");
      return INIT_PARAMETERS_INCORRECT;
   }
   EventSetTimer(InpPollSeconds);
   Print("TradeMind SER8 Demo Order Executor v1.0 started. Executor only. One request in, one order, one result out. No strategy logic of any kind runs here.");
   return INIT_SUCCEEDED;
}

void OnTimer()
{
   ProcessPendingRequest();
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}
