# -*- coding: utf-8 -*-
"""
goal 4 — CU 증가에 따른 AUM 증가 (설정 자금유입)
  flow_value = delta_num_cu × cu_value(=NAV×CU구성좌수)
출력:
  out/aum_from_cu_daily.csv   (base, date, etf) 일간 유입액
  out/aum_from_cu_weekly.csv  (base, etf) 주간 합계 + 순위(주간 유입액 기준)
"""
import pandas as pd
from _common import load_meta, save


def main(out_dir=None, dates=None):
    m = load_meta().sort_values(["etf_code", "date"])
    g = m.groupby("etf_code")
    m["delta_num_cu"] = m["num_cu"] - g["num_cu"].shift()
    m["flow_value"] = m["delta_num_cu"] * m["cu_value"]

    daily = m[["base_date", "date", "etf_code", "etf_name", "etf_type",
               "num_cu", "delta_num_cu", "cu_value", "flow_value"]].copy()
    if dates is not None:
        daily = daily[daily["date"].isin(dates)]
    daily = daily.sort_values(["base_date", "date", "flow_value"],
                              ascending=[True, True, False])
    save(daily, "aum_from_cu_daily.csv", out_dir)

    # 주간 합계: base_date 내 일간 flow 합 + 주간 num_cu 순증감
    wk = (daily.groupby(["base_date", "etf_code", "etf_name", "etf_type"])
                .agg(week_delta_num_cu=("delta_num_cu", "sum"),
                     week_flow_value=("flow_value", "sum"),
                     avg_cu_value=("cu_value", "mean"))
                .reset_index())
    wk["rank_in_base"] = (wk.groupby("base_date")["week_flow_value"]
                            .rank(ascending=False, method="min").astype("Int64"))
    wk = wk.sort_values(["base_date", "week_flow_value"], ascending=[True, False])
    save(wk, "aum_from_cu_weekly.csv", out_dir)

    # 콘솔 요약: base별 상위 5
    print("  [주간 유입 상위 5 / base]")
    for base, gb in wk.groupby("base_date"):
        top = gb.head(5)
        print(f"    {base}:")
        for r in top.itertuples():
            print(f"      {r.etf_name[:22]:22} {r.week_flow_value/1e8:12,.1f}억 "
                  f"(Δcu {r.week_delta_num_cu:+.0f})")


if __name__ == "__main__":
    main()
