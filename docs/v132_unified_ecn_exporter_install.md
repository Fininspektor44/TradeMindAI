# v1.32 install checklist

1. Run `scripts/install_v132_unified_ecn_exporter.ps1 -Login 77053345` from the repository root.
2. Require MetaEditor compile output containing `0 errors`.
3. In MT5, refresh Navigator and attach `TradeMindAI_ECN_UnifiedExporter_v1_32` to one chart on account `77053345`.
4. For the embedded deal collector, keep `Deal_InpForwardOnly=true` and set `Deal_InpStartFrom` to the clean ECN experiment start time used for this run.
5. Leave the three legacy exporter EAs attached during verification.
6. Run `scripts/verify_v132_unified_outputs.ps1 -Login 77053345`.
7. Only after all output families are fresh, remove the three legacy exporter EAs from their charts.

The unified exporter is read-only and preserves the existing CSV output contracts so downstream TradeMind readers do not need a data-path migration.
