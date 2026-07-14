# -*- coding: utf-8 -*-
"""전체 파이프라인 실행: parse_step1 → a1~a6."""
import importlib

STEPS = ["parse_step1", "a1_weekly_returns", "a2_cu_flow", "a3_rebalancing",
         "a4_aum_from_cu", "a5_rebal_summary", "a6_universe_flow"]

for name in STEPS:
    print(f"\n########## {name} ##########")
    importlib.import_module(name).main()
print("\n완료.")
