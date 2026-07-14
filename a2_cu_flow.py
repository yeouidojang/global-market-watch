# -*- coding: utf-8 -*-
"""
goal 2 — 일별 ETF Creation Unit(num_cu) 설정/환매 동향
  delta_num_cu = num_cu - 전일 num_cu   (>0 설정, <0 환매)
  flow_value   = delta_num_cu × cu_value(=NAV×CU구성좌수)  ← 자금유입/유출 금액
출력: out/cu_flow.csv
전일은 해당 ETF가 관측된 직전 run/영업일(주 경계 포함).
"""
import pandas as pd
from _common import load_meta, save


def main(out_dir=None, dates=None):
    m = load_meta().sort_values(["etf_code", "date"])
    g = m.groupby("etf_code")
    m["prev_date"] = g["date"].shift()
    m["prev_num_cu"] = g["num_cu"].shift()
    m["delta_num_cu"] = m["num_cu"] - m["prev_num_cu"]
    m["flow_value"] = m["delta_num_cu"] * m["cu_value"]

    def act(x):
        if pd.isna(x):
            return ""
        return "설정" if x > 0 else ("환매" if x < 0 else "변동없음")
    m["action"] = m["delta_num_cu"].apply(act)

    out = m[["base_date", "date", "prev_date", "etf_code", "etf_name", "etf_type",
             "num_cu", "prev_num_cu", "delta_num_cu", "cu_value", "flow_value", "action"]]
    out = out.sort_values(["date", "etf_code"])
    if dates is not None:
        out = out[out["date"].isin(dates)]
    save(out, "cu_flow.csv", out_dir)


if __name__ == "__main__":
    main()
