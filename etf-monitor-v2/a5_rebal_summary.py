# -*- coding: utf-8 -*-
"""
goal 5 — 일별 리밸런싱된 ETF 수 + 대표 ETF 목록
  리밸런싱 = 주식/선물/옵션 바스켓에 편입·편출·변경 발생한 ETF.
  대표 ETF = 해당일 이벤트 건수 상위.
입력: out/rebalancing.csv (a3 산출)
출력: out/rebal_summary.csv       (run_date × #리밸런싱 ETF, 대표목록)
     out/rebal_etf_daily.csv     (run_date, etf) 이벤트 건수
"""
import os
import pandas as pd
from _common import OUT, save

REBAL_KINDS = {"stock", "futures", "option"}


def main(out_dir=None):
    _out = out_dir or OUT
    reb = pd.read_csv(os.path.join(_out, "rebalancing.csv"),
                      dtype={"base_date": str, "run_date": str, "etf_code": str})
    reb = reb[reb["kind"].isin(REBAL_KINDS)]

    # ETF×일 이벤트 건수
    per = (reb.groupby(["base_date", "run_date", "etf_code", "etf_name"])
              .agg(n_events=("event", "size"),
                   n_in=("event", lambda s: (s == "편입").sum()),
                   n_out=("event", lambda s: (s == "편출").sum()),
                   n_chg=("event", lambda s: (s == "변경").sum()))
              .reset_index()
              .sort_values(["run_date", "n_events"], ascending=[True, False]))
    save(per, "rebal_etf_daily.csv", out_dir)

    # 일별 요약 + 대표 ETF(상위 5)
    rows = []
    for (base, rd), g in per.groupby(["base_date", "run_date"]):
        top = g.head(5)
        reps = ", ".join(f"{r.etf_name}({r.n_events})" for r in top.itertuples())
        rows.append(dict(base_date=base, run_date=rd,
                         n_rebalanced_etf=g["etf_code"].nunique(),
                         total_events=int(g["n_events"].sum()),
                         representative_etfs=reps))
    summ = pd.DataFrame(rows).sort_values("run_date")
    save(summ, "rebal_summary.csv", out_dir)

    print("  [일별 리밸런싱 ETF 수]")
    for r in summ.itertuples():
        print(f"    {r.run_date}: {r.n_rebalanced_etf:3d} ETF  "
              f"({r.total_events:,} events)")


if __name__ == "__main__":
    main()
