#property strict
#property version   "1.320"
#property description "TradeMind AI v1.32 unified read-only ECN exporter"
#property description "One EA: volume microstructure + MT5 risk snapshots + deal history."
#property description "Read-only. No orders. No position modification."

// -----------------------------------------------------------------------------
// Volume module: preserve the proven v1.9.4 implementation and output schema.
// Prefix every public identifier so the three legacy collectors can coexist in
// one MQL5 program without changing their data semantics.
// -----------------------------------------------------------------------------
#define InpCanonicalSymbols Volume_InpCanonicalSymbols
#define InpTimeframe Volume_InpTimeframe
#define InpTimerSeconds Volume_InpTimerSeconds
#define InpBackfillBars Volume_InpBackfillBars
#define InpCatchupBars Volume_InpCatchupBars
#define InpOutputFolder Volume_InpOutputFolder
#define InpManifestFile Volume_InpManifestFile
#define InpDebugLog Volume_InpDebugLog
#define g_canonical Volume_g_canonical
#define g_actual Volume_g_actual
#define g_last_exported Volume_g_last_exported
#define g_bar_seconds Volume_g_bar_seconds
#define Trim Volume_Trim
#define Upper Volume_Upper
#define NormalizeSymbol Volume_NormalizeSymbol
#define IsCryptoCanonical Volume_IsCryptoCanonical
#define RowSchema Volume_RowSchema
#define RejectUsdToUsdt Volume_RejectUsdToUsdt
#define MatchScore Volume_MatchScore
#define ResolveBrokerSymbol Volume_ResolveBrokerSymbol
#define ParseSymbols Volume_ParseSymbols
#define TimeframeText Volume_TimeframeText
#define SafeSymbolName Volume_SafeSymbolName
#define OutputFile Volume_OutputFile
#define WriteHeader Volume_WriteHeader
#define OpenOutput Volume_OpenOutput
#define WriteManifest Volume_WriteManifest
#define HistoricalVolumeMetrics Volume_HistoricalVolumeMetrics
#define AppendBar Volume_AppendBar
#define BackfillSymbol Volume_BackfillSymbol
#define ExportNewBars Volume_ExportNewBars
#define OnInit Volume_OnInit
#define OnDeinit Volume_OnDeinit
#define OnTimer Volume_OnTimer
#define OnTick Volume_OnTick
#include "source\TradeMindAI_ECN_UniversalVolumeExporter_v1_9_4.mq5"
#undef OnTick
#undef OnTimer
#undef OnDeinit
#undef OnInit
#undef ExportNewBars
#undef BackfillSymbol
#undef AppendBar
#undef HistoricalVolumeMetrics
#undef WriteManifest
#undef OpenOutput
#undef WriteHeader
#undef OutputFile
#undef SafeSymbolName
#undef TimeframeText
#undef ParseSymbols
#undef ResolveBrokerSymbol
#undef MatchScore
#undef RejectUsdToUsdt
#undef RowSchema
#undef IsCryptoCanonical
#undef NormalizeSymbol
#undef Upper
#undef Trim
#undef g_bar_seconds
#undef g_last_exported
#undef g_actual
#undef g_canonical
#undef InpDebugLog
#undef InpManifestFile
#undef InpOutputFolder
#undef InpCatchupBars
#undef InpBackfillBars
#undef InpTimerSeconds
#undef InpTimeframe
#undef InpCanonicalSymbols

// -----------------------------------------------------------------------------
// Deal/history module: preserve v1.152 forward-only exporter semantics.
// -----------------------------------------------------------------------------
#define InpForwardOnly Deal_InpForwardOnly
#define InpStartFrom Deal_InpStartFrom
#define InpHistoryDays Deal_InpHistoryDays
#define InpRefreshSeconds Deal_InpRefreshSeconds
#define InpOutputFolder Deal_InpOutputFolder
#define InpMagicFilter Deal_InpMagicFilter
#define InpIncludeManual Deal_InpIncludeManual
#define InpSymbols Deal_InpSymbols
#define InpMagicLabels Deal_InpMagicLabels
#define TrimCopy Deal_TrimCopy
#define ListContainsLong Deal_ListContainsLong
#define ListContainsString Deal_ListContainsString
#define MagicAllowed Deal_MagicAllowed
#define FindTrackedPosition Deal_FindTrackedPosition
#define RegisterTrackedPosition Deal_RegisterTrackedPosition
#define RobotLabel Deal_RobotLabel
#define EnumTail Deal_EnumTail
#define LoginText Deal_LoginText
#define DealsFilename Deal_DealsFilename
#define PositionsFilename Deal_PositionsFilename
#define AccountFilename Deal_AccountFilename
#define ForwardStartKey Deal_ForwardStartKey
#define ResolveStartTime Deal_ResolveStartTime
#define ExportDeals Deal_ExportDeals
#define AppendAccountSnapshot Deal_AppendAccountSnapshot
#define AppendPositionSnapshots Deal_AppendPositionSnapshots
#define Collect Deal_Collect
#define OnInit Deal_OnInit
#define OnTimer Deal_OnTimer
#define OnDeinit Deal_OnDeinit
#include "source\TradeMind_Grid_Deal_Exporter.mq5"
#undef OnDeinit
#undef OnTimer
#undef OnInit
#undef Collect
#undef AppendPositionSnapshots
#undef AppendAccountSnapshot
#undef ExportDeals
#undef ResolveStartTime
#undef ForwardStartKey
#undef AccountFilename
#undef PositionsFilename
#undef DealsFilename
#undef LoginText
#undef EnumTail
#undef RobotLabel
#undef RegisterTrackedPosition
#undef FindTrackedPosition
#undef MagicAllowed
#undef ListContainsString
#undef ListContainsLong
#undef TrimCopy
#undef InpMagicLabels
#undef InpSymbols
#undef InpIncludeManual
#undef InpMagicFilter
#undef InpOutputFolder
#undef InpRefreshSeconds
#undef InpHistoryDays
#undef InpStartFrom
#undef InpForwardOnly

// -----------------------------------------------------------------------------
// Risk module: preserve v1.181 account/position/symbol snapshot schemas.
// -----------------------------------------------------------------------------
#define InpRefreshSeconds Risk_InpRefreshSeconds
#define InpOutputFolder Risk_InpOutputFolder
#define InpSymbols Risk_InpSymbols
#define TrimCopy Risk_TrimCopy
#define ListContainsString Risk_ListContainsString
#define EnumTail Risk_EnumTail
#define LoginText Risk_LoginText
#define UtcNowMsc Risk_UtcNowMsc
#define AccountFilename Risk_AccountFilename
#define PositionsFilename Risk_PositionsFilename
#define SymbolsFilename Risk_SymbolsFilename
#define AppendAccountSnapshot Risk_AppendAccountSnapshot
#define ExportPositionSnapshot Risk_ExportPositionSnapshot
#define ExportSymbolSnapshot Risk_ExportSymbolSnapshot
#define Collect Risk_Collect
#define OnInit Risk_OnInit
#define OnTimer Risk_OnTimer
#define OnDeinit Risk_OnDeinit
#include "source\TradeMind_MT5_Risk_Snapshot_Exporter.mq5"
#undef OnDeinit
#undef OnTimer
#undef OnInit
#undef Collect
#undef ExportSymbolSnapshot
#undef ExportPositionSnapshot
#undef AppendAccountSnapshot
#undef SymbolsFilename
#undef PositionsFilename
#undef AccountFilename
#undef UtcNowMsc
#undef LoginText
#undef EnumTail
#undef ListContainsString
#undef TrimCopy
#undef InpSymbols
#undef InpOutputFolder
#undef InpRefreshSeconds

long g_unified_last_volume=0;
long g_unified_last_deals=0;
long g_unified_last_risk=0;

int UnifiedBaseTimerSeconds()
{
   int seconds=Volume_InpTimerSeconds;
   if(Deal_InpRefreshSeconds<seconds)
      seconds=Deal_InpRefreshSeconds;
   if(Risk_InpRefreshSeconds<seconds)
      seconds=Risk_InpRefreshSeconds;
   if(seconds<1)
      seconds=1;
   return seconds;
}

void UnifiedMarkScheduleNow()
{
   long now=(long)TimeLocal();
   g_unified_last_volume=now;
   g_unified_last_deals=now;
   g_unified_last_risk=now;
}

int OnInit()
{
   // Each legacy module performs its normal first collection/backfill here.
   // Their EventSetTimer calls are deliberately replaced by one final timer
   // after all three initializers succeed.
   int volume_result=Volume_OnInit();
   if(volume_result!=INIT_SUCCEEDED)
      return volume_result;

   int deal_result=Deal_OnInit();
   if(deal_result!=INIT_SUCCEEDED)
   {
      Volume_OnDeinit(0);
      return deal_result;
   }

   int risk_result=Risk_OnInit();
   if(risk_result!=INIT_SUCCEEDED)
   {
      Deal_OnDeinit(0);
      Volume_OnDeinit(0);
      return risk_result;
   }

   EventKillTimer();
   int base_seconds=UnifiedBaseTimerSeconds();
   if(!EventSetTimer(base_seconds))
   {
      Print("TradeMind v1.32 unified exporter: EventSetTimer failed error=",GetLastError());
      Risk_OnDeinit(0);
      Deal_OnDeinit(0);
      Volume_OnDeinit(0);
      return INIT_FAILED;
   }

   UnifiedMarkScheduleNow();
   Print("TradeMind AI v1.32 Unified ECN Exporter started. account=",
         AccountInfoInteger(ACCOUNT_LOGIN)," timer=",base_seconds,
         "s. READ-ONLY. No orders. No position modification.");
   return INIT_SUCCEEDED;
}

void OnTimer()
{
   long now=(long)TimeLocal();

   if(now-g_unified_last_volume>=Volume_InpTimerSeconds)
   {
      Volume_OnTimer();
      g_unified_last_volume=now;
   }

   if(now-g_unified_last_risk>=Risk_InpRefreshSeconds)
   {
      Risk_OnTimer();
      g_unified_last_risk=now;
   }

   if(now-g_unified_last_deals>=Deal_InpRefreshSeconds)
   {
      Deal_OnTimer();
      g_unified_last_deals=now;
   }
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   Risk_OnDeinit(reason);
   Deal_OnDeinit(reason);
   Volume_OnDeinit(reason);
   Print("TradeMind AI v1.32 Unified ECN Exporter stopped. reason=",reason);
}

void OnTick()
{
   // Intentionally empty. All collection is timer-driven and read-only.
}
