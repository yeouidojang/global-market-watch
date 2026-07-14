# -*- coding: utf-8 -*-
"""
goal 1 — 주간 일별 수익률(금~목)
  · 단순평균 & 시총가중 수익률
  · 전체 / 유형별 / 테마별
출력: out/weekly_returns.csv  (base_date, date, dim, group, n_etf, ret_simple, ret_capwtd)
시총가중은 '전일 시가총액'을 가중치로 사용(당일 등락 반영 전 비중).
"""
import numpy as np
import pandas as pd
from _common import load_meta, load_universe, save


def wavg(d, val, w):
    d = d.dropna(subset=[val, w])
    tw = d[w].sum()
    return (d[val] * d[w]).sum() / tw if tw > 0 else np.nan


def main(out_dir=None, dates=None):
    m = load_meta()
    uni = load_universe()[["base_date", "etf_code", "theme"]]
    m = m.merge(uni, on=["base_date", "etf_code"], how="left")

    # 가중치 = 전일 시가총액(없으면 당일) — shift 전에 필터하면 전일값 누락되므로 shift 후 필터
    m = m.sort_values(["etf_code", "date"])
    m["mktcap_prev"] = m.groupby("etf_code")["mktcap"].shift()
    m["w"] = m["mktcap_prev"].fillna(m["mktcap"])
    if dates is not None:
        m = m[m["date"].isin(dates)]

    rows = []

    def emit(base, date, dim, group, d):
        d = d.dropna(subset=["ret"])
        if d.empty:
            return
        rows.append(dict(
            base_date=base, date=date, dim=dim, group=group,
            n_etf=d["etf_code"].nunique(),
            ret_simple=round(d["ret"].mean(), 4),
            ret_capwtd=round(wavg(d, "ret", "w"), 4),
        ))

    for (base, date), gd in m.groupby(["base_date", "date"]):
        emit(base, date, "all", "ALL", gd)
        for t, dt in gd.groupby(gd["etf_type"].replace("", "(빈값)")):
            emit(base, date, "type", t, dt)
        for th, dth in gd.groupby("theme"):
            emit(base, date, "theme", th, dth)

    out = pd.DataFrame(rows).sort_values(["base_date", "date", "dim", "group"])
    save(out, "weekly_returns.csv", out_dir)


if __name__ == "__main__":
    main()
