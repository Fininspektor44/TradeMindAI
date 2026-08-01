#property strict
#property version   "1.70"
#property description "TradeMind AI v1.7 read-only crypto volume intelligence exporter"
#property description "Auto-discovers broker crypto symbols and never sends trading orders."

input string          InpCanonicalSymbols = "BTCUSD,ETHUSD,SOLUSD,XRPUSD,LTCUSD,BCHUSD,ADAUSD,DOGEUSD";
input ENUM_TIMEFRAMES InpTimeframe        = PERIOD_M5;
input int             InpTimerSeconds     = 10;
input int             InpBackfillBars     = 288;
input int             InpCatchupBars      = 576;
input string          InpOutputFolder     = "TradeMindAI_Volume_v1_4";
input string          InpManifestFile     = "crypto_manifest.csv";
input bool            InpDebugLog         = true;

string   g_canonical[];
string   g_actual[];
datetime g_last_exported[];
int      g_bar_seconds=300;

string Trim(string value)
{
   StringTrimLeft(value);
   StringTrimRight(value);
   return value;
}

string Upper(string value)
{
   StringToUpper(value);
   return value;
}

string NormalizeSymbol(string value)
{
   value=Upper(Trim(value));
   StringReplace(value,".","");
   StringReplace(value,"_","");
   StringReplace(value,"-","");
   StringReplace(value,"/","");
   StringReplace(value,"\\","");
   StringReplace(value,":","");
   StringReplace(value," ","");
   StringReplace(value,"#","");
   return value;
}

bool RejectUsdToUsdt(const string candidate,const string canonical)
{
   string c=NormalizeSymbol(candidate);
   string a=NormalizeSymbol(canonical);
   if(StringLen(a)<3 || StringSubstr(a,StringLen(a)-3)!="USD")
      return false;
   if(StringFind(c,a)==0 && StringLen(c)>StringLen(a))
   {
      string tail=StringSubstr(c,StringLen(a),1);
      return tail=="T";
   }
   return false;
}

int MatchScore(const string candidate,const string canonical)
{
   string c=NormalizeSymbol(candidate);
   string a=NormalizeSymbol(canonical);
   if(c==a)
      return 10000;
   if(RejectUsdToUsdt(candidate,canonical))
      return -1;
   int extra=StringLen(c)-StringLen(a);
   if(extra<1 || extra>8)
      return -1;
   if(StringFind(c,a)==0)
      return 8000-extra;
   int position=StringFind(c,a);
   if(position==extra)
      return 7000-extra;
   return -1;
}

string ResolveBrokerSymbol(const string canonical)
{
   int total=SymbolsTotal(false);
   string best="";
   int best_score=-1;
   for(int i=0;i<total;i++)
   {
      string candidate=SymbolName(i,false);
      int score=MatchScore(candidate,canonical);
      if(score>best_score)
      {
         best=candidate;
         best_score=score;
      }
   }
   return best;
}

int ParseSymbols(const string list,string &symbols[])
{
   string parts[];
   int count=StringSplit(list,',',parts);
   ArrayResize(symbols,0);
   for(int i=0;i<count;i++)
   {
      string symbol=Upper(Trim(parts[i]));
      if(symbol=="")
         continue;
      bool duplicate=false;
      for(int j=0;j<ArraySize(symbols);j++)
         if(symbols[j]==symbol)
            duplicate=true;
      if(duplicate)
         continue;
      int size=ArraySize(symbols);
      ArrayResize(symbols,size+1);
      symbols[size]=symbol;
   }
   return ArraySize(symbols);
}

string TimeframeText(ENUM_TIMEFRAMES timeframe)
{
   string text=EnumToString(timeframe);
   if(StringFind(text,"PERIOD_")==0)
      return StringSubstr(text,7);
   return text;
}

string SafeSymbolName(string symbol)
{
   StringReplace(symbol,".","_");
   StringReplace(symbol,"/","_");
   StringReplace(symbol,"\\","_");
   StringReplace(symbol,":","_");
   return symbol;
}

string OutputFile(const string canonical)
{
   return InpOutputFolder+"\\volume_"+SafeSymbolName(canonical)+"_"+TimeframeText(InpTimeframe)+".csv";
}

void WriteHeader(const int handle)
{
   FileWrite(handle,
      "schema_version","time","symbol","timeframe","bar_seconds","point",
      "open","high","low","close","bar_tick_volume","tick_count",
      "tick_rate_per_sec","bid_up","bid_down","ask_up","ask_down",
      "mid_up","mid_down","buy_ticks","sell_ticks","trade_volume",
      "trade_volume_real","spread_mean_points","spread_min_points",
      "spread_max_points","spread_last_points","spread_expansion_points",
      "realized_abs_move_points","direction_imbalance","delta_proxy",
      "rvol_20","volume_percentile_100","range_per_tick_points",
      "body_per_tick_points","tick_copy_status","tick_copy_error");
}

int OpenOutput(const string canonical)
{
   string path=OutputFile(canonical);
   ResetLastError();
   int handle=FileOpen(path,FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_SHARE_READ|FILE_COMMON,',');
   if(handle==INVALID_HANDLE)
   {
      Print("TradeMind v1.7: cannot open ",path," error=",GetLastError());
      return INVALID_HANDLE;
   }
   if(FileSize(handle)==0)
      WriteHeader(handle);
   FileSeek(handle,0,SEEK_END);
   return handle;
}

void WriteManifest(const string requested[])
{
   string path=InpOutputFolder+"\\"+InpManifestFile;
   int handle=FileOpen(path,FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_SHARE_READ|FILE_COMMON,',');
   if(handle==INVALID_HANDLE)
   {
      Print("TradeMind v1.7: cannot write manifest ",path," error=",GetLastError());
      return;
   }
   FileWrite(handle,"schema_version","canonical_symbol","broker_symbol","status","timeframe");
   for(int i=0;i<ArraySize(requested);i++)
   {
      string actual=ResolveBrokerSymbol(requested[i]);
      FileWrite(handle,"1.7",requested[i],actual,(actual=="" ? "MISSING" : "RESOLVED"),TimeframeText(InpTimeframe));
   }
   FileFlush(handle);
   FileClose(handle);
}

void HistoricalVolumeMetrics(const string actual,const datetime bar_time,const long current_volume,double &rvol_20,double &percentile_100)
{
   rvol_20=0.0;
   percentile_100=0.0;
   datetime from_time=bar_time-(datetime)(g_bar_seconds*140);
   datetime to_time=bar_time-1;
   MqlRates history[];
   ArraySetAsSeries(history,false);
   int copied=CopyRates(actual,InpTimeframe,from_time,to_time,history);
   if(copied<=0)
      return;
   int start_100=(copied>100 ? copied-100 : 0);
   int sample_100=0;
   int below_or_equal=0;
   for(int i=start_100;i<copied;i++)
   {
      sample_100++;
      if(history[i].tick_volume<=current_volume)
         below_or_equal++;
   }
   if(sample_100>0)
      percentile_100=100.0*(double)below_or_equal/(double)sample_100;
   int start_20=(copied>20 ? copied-20 : 0);
   double sum_20=0.0;
   int sample_20=0;
   for(int i=start_20;i<copied;i++)
   {
      sum_20+=(double)history[i].tick_volume;
      sample_20++;
   }
   if(sample_20>0 && sum_20>0.0)
      rvol_20=(double)current_volume/(sum_20/(double)sample_20);
}

bool AppendBar(const string actual,const string canonical,const MqlRates &bar)
{
   double point=SymbolInfoDouble(actual,SYMBOL_POINT);
   if(point<=0.0)
      point=0.00001;
   int digits=(int)SymbolInfoInteger(actual,SYMBOL_DIGITS);
   ulong from_msc=(ulong)bar.time*1000;
   ulong to_msc=((ulong)bar.time+(ulong)g_bar_seconds)*1000-1;
   MqlTick ticks[];
   ArraySetAsSeries(ticks,false);
   ResetLastError();
   int copied=CopyTicksRange(actual,ticks,COPY_TICKS_ALL,from_msc,to_msc);
   int copy_error=GetLastError();
   string copy_status="OK";
   if(copied<0) copy_status="ERROR";
   else if(copied==0) copy_status="NO_TICKS";
   else if(copy_error!=0) copy_status="PARTIAL";

   int tick_count=(copied>0 ? copied : 0);
   int bid_up=0,bid_down=0,ask_up=0,ask_down=0,mid_up=0,mid_down=0,buy_ticks=0,sell_ticks=0;
   ulong trade_volume=0;
   double trade_volume_real=0.0;
   double previous_bid=0.0,previous_ask=0.0,previous_mid=0.0;
   double spread_sum=0.0,spread_min=0.0,spread_max=0.0,spread_first=0.0,spread_last=0.0;
   int spread_samples=0;
   double realized_abs_move_points=0.0;

   for(int i=0;i<tick_count;i++)
   {
      double bid=ticks[i].bid;
      double ask=ticks[i].ask;
      if(previous_bid>0.0 && bid>0.0)
      {
         if(bid>previous_bid) bid_up++;
         else if(bid<previous_bid) bid_down++;
      }
      if(previous_ask>0.0 && ask>0.0)
      {
         if(ask>previous_ask) ask_up++;
         else if(ask<previous_ask) ask_down++;
      }
      if(bid>0.0) previous_bid=bid;
      if(ask>0.0) previous_ask=ask;
      if(bid>0.0 && ask>=bid)
      {
         double spread=(ask-bid)/point;
         if(spread_samples==0)
         {
            spread_first=spread;
            spread_min=spread;
            spread_max=spread;
         }
         spread_sum+=spread;
         spread_samples++;
         spread_last=spread;
         if(spread<spread_min) spread_min=spread;
         if(spread>spread_max) spread_max=spread;
         double mid=(bid+ask)*0.5;
         if(previous_mid>0.0)
         {
            double change=mid-previous_mid;
            realized_abs_move_points+=MathAbs(change)/point;
            if(change>0.0) mid_up++;
            else if(change<0.0) mid_down++;
         }
         previous_mid=mid;
      }
      bool is_buy=((ticks[i].flags&TICK_FLAG_BUY)==TICK_FLAG_BUY);
      bool is_sell=((ticks[i].flags&TICK_FLAG_SELL)==TICK_FLAG_SELL);
      if(is_buy) buy_ticks++;
      if(is_sell) sell_ticks++;
      bool volume_changed=((ticks[i].flags&TICK_FLAG_VOLUME)==TICK_FLAG_VOLUME);
      if(volume_changed || is_buy || is_sell)
      {
         trade_volume+=ticks[i].volume;
         trade_volume_real+=ticks[i].volume_real;
      }
   }

   double spread_mean=(spread_samples>0 ? spread_sum/(double)spread_samples : 0.0);
   double spread_expansion=(spread_samples>0 ? spread_last-spread_first : 0.0);
   int directional_ticks=mid_up+mid_down;
   double direction_imbalance=(directional_ticks>0 ? (double)(mid_up-mid_down)/(double)directional_ticks : 0.0);
   int delta_proxy=mid_up-mid_down;
   double tick_rate=(g_bar_seconds>0 ? (double)tick_count/(double)g_bar_seconds : 0.0);
   double range_points=(bar.high-bar.low)/point;
   double body_points=MathAbs(bar.close-bar.open)/point;
   double range_per_tick=(tick_count>0 ? range_points/(double)tick_count : 0.0);
   double body_per_tick=(tick_count>0 ? body_points/(double)tick_count : 0.0);
   double rvol_20=0.0,volume_percentile_100=0.0;
   HistoricalVolumeMetrics(actual,bar.time,bar.tick_volume,rvol_20,volume_percentile_100);

   int handle=OpenOutput(canonical);
   if(handle==INVALID_HANDLE)
      return false;
   FileWrite(handle,
      "1.7",(long)bar.time,canonical,TimeframeText(InpTimeframe),g_bar_seconds,
      DoubleToString(point,digits),DoubleToString(bar.open,digits),DoubleToString(bar.high,digits),
      DoubleToString(bar.low,digits),DoubleToString(bar.close,digits),(long)bar.tick_volume,tick_count,
      DoubleToString(tick_rate,8),bid_up,bid_down,ask_up,ask_down,mid_up,mid_down,buy_ticks,sell_ticks,
      (long)trade_volume,DoubleToString(trade_volume_real,8),DoubleToString(spread_mean,6),
      DoubleToString(spread_min,6),DoubleToString(spread_max,6),DoubleToString(spread_last,6),
      DoubleToString(spread_expansion,6),DoubleToString(realized_abs_move_points,6),
      DoubleToString(direction_imbalance,8),delta_proxy,DoubleToString(rvol_20,8),
      DoubleToString(volume_percentile_100,6),DoubleToString(range_per_tick,8),
      DoubleToString(body_per_tick,8),copy_status,copy_error);
   FileFlush(handle);
   FileClose(handle);
   if(InpDebugLog)
      Print("TradeMind v1.7 ",canonical," <- ",actual," bar=",TimeToString(bar.time,TIME_DATE|TIME_MINUTES),
            " ticks=",tick_count," rvol=",DoubleToString(rvol_20,2)," status=",copy_status);
   return copy_status!="ERROR";
}

void BackfillSymbol(const int index)
{
   MqlRates rates[];
   ArraySetAsSeries(rates,false);
   int copied=CopyRates(g_actual[index],InpTimeframe,1,InpBackfillBars,rates);
   if(copied<=0)
   {
      Print("TradeMind v1.7: no backfill bars for ",g_canonical[index]," actual=",g_actual[index]," error=",GetLastError());
      return;
   }
   datetime latest=0;
   for(int i=0;i<copied;i++)
   {
      AppendBar(g_actual[index],g_canonical[index],rates[i]);
      if(rates[i].time>latest) latest=rates[i].time;
   }
   g_last_exported[index]=latest;
}

void ExportNewBars(const int index)
{
   datetime latest_closed=iTime(g_actual[index],InpTimeframe,1);
   if(latest_closed<=0 || latest_closed<=g_last_exported[index])
      return;
   MqlRates rates[];
   ArraySetAsSeries(rates,false);
   int copied=CopyRates(g_actual[index],InpTimeframe,1,InpCatchupBars,rates);
   if(copied<=0)
      return;
   for(int i=0;i<copied;i++)
   {
      if(rates[i].time<=g_last_exported[index])
         continue;
      if(!AppendBar(g_actual[index],g_canonical[index],rates[i]))
         break;
      g_last_exported[index]=rates[i].time;
   }
}

int OnInit()
{
   if(InpTimerSeconds<1 || InpBackfillBars<1 || InpCatchupBars<1)
      return INIT_PARAMETERS_INCORRECT;
   g_bar_seconds=PeriodSeconds(InpTimeframe);
   if(g_bar_seconds<=0)
      return INIT_PARAMETERS_INCORRECT;
   string requested[];
   int requested_count=ParseSymbols(InpCanonicalSymbols,requested);
   if(requested_count<=0)
      return INIT_PARAMETERS_INCORRECT;
   if(!FolderCreate(InpOutputFolder,FILE_COMMON) && GetLastError()!=0)
      ResetLastError();

   ArrayResize(g_canonical,0);
   ArrayResize(g_actual,0);
   for(int i=0;i<requested_count;i++)
   {
      string actual=ResolveBrokerSymbol(requested[i]);
      if(actual=="")
      {
         Print("TradeMind v1.7: crypto symbol not found: ",requested[i]);
         continue;
      }
      if(!SymbolSelect(actual,true))
      {
         Print("TradeMind v1.7: cannot select broker symbol ",actual," for ",requested[i]);
         continue;
      }
      int size=ArraySize(g_actual);
      ArrayResize(g_actual,size+1);
      ArrayResize(g_canonical,size+1);
      g_actual[size]=actual;
      g_canonical[size]=requested[i];
      Print("TradeMind v1.7: resolved ",requested[i]," -> ",actual);
   }
   WriteManifest(requested);
   int count=ArraySize(g_actual);
   if(count<=0)
   {
      Print("TradeMind v1.7: no crypto symbols resolved in this terminal");
      return INIT_FAILED;
   }
   ArrayResize(g_last_exported,count);
   ArrayInitialize(g_last_exported,0);
   for(int i=0;i<count;i++)
      BackfillSymbol(i);
   EventSetTimer(InpTimerSeconds);
   Print("TradeMind AI v1.7 Crypto Volume started. Read-only. Resolved=",count," requested=",requested_count,
         " folder=",InpOutputFolder);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   Print("TradeMind AI v1.7 Crypto Volume stopped. reason=",reason);
}

void OnTimer()
{
   for(int i=0;i<ArraySize(g_actual);i++)
      ExportNewBars(i);
}

void OnTick()
{
   // Intentionally empty. This exporter never sends orders.
}
