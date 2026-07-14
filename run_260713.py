# -*- coding: utf-8 -*-
"""
1회성 실행: 기준일 20260709(목), 월~금(20260706~20260710) 데이터
출력: out_260713/etf_monitor_20260709.xlsx

실행 방법:
  meritzquant venv python.exe run_260713.py
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_260713 = os.path.join(BASE, "out_260713")

# 월~금 run_date 목록 (없는 날짜는 자동 무시됨)
MON_FRI = ["20260706", "20260707", "20260708", "20260709", "20260710"]

# ── Step 0: parse_step1 재실행 (상장일/상장폐지일 필드 추가) ──────────
print("\n########## parse_step1 ##########")
import parse_step1
parse_step1.main()

# ── Step 1~6: 월~금 필터, out_260713/ 출력 ──────────────────────────
import a1_weekly_returns, a2_cu_flow, a3_rebalancing
import a4_aum_from_cu, a5_rebal_summary, a6_universe_flow
import export_xlsx

print("\n########## a1_weekly_returns ##########")
a1_weekly_returns.main(out_dir=OUT_260713, dates=MON_FRI)

print("\n########## a2_cu_flow ##########")
a2_cu_flow.main(out_dir=OUT_260713, dates=MON_FRI)

print("\n########## a3_rebalancing ##########")
a3_rebalancing.main(out_dir=OUT_260713, run_dates=MON_FRI)

print("\n########## a4_aum_from_cu ##########")
a4_aum_from_cu.main(out_dir=OUT_260713, dates=MON_FRI)

print("\n########## a5_rebal_summary ##########")
a5_rebal_summary.main(out_dir=OUT_260713)

print("\n########## a6_universe_flow ##########")
a6_universe_flow.main(out_dir=OUT_260713)

# ── xlsx 내보내기 (신규상장ETF 시트 포함) ────────────────────────────
print("\n########## export_xlsx ##########")
export_xlsx.main(out_dir=OUT_260713)

print("\n완료 → out_260713/")
