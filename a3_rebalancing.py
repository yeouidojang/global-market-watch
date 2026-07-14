# -*- coding: utf-8 -*-
"""
goal 3 — 일별 리밸런싱 현황 (1CU 바스켓 구성 변화)
  ETF별로 구성종목의 전일 대비 주식수(계약수) 변화를 산출.
  event: 편입(prev 없음) / 편출(당일 없음) / 변경(수량변동) / 유지(0)
  1CU 바스켓은 설정/환매와 무관 → 수량변동 = 실제 리밸런싱.
출력:
  out/rebalancing.csv         변경분만(편입/편출/변경), 종목단위
  out/rebalancing_by_kind.csv (etf, run_date) × kind 요약(순증감 계약수·건수)
"""
import numpy as np
import pandas as pd
from _common import load_holdings, save

KEEP_EVENTS = {"편입", "편출", "변경"}


def main(out_dir=None, run_dates=None):
    h = load_holdings()
    # (run_date -> base_date) 매핑
    rd2base = dict(h.drop_duplicates("run_date")[["run_date", "base_date"]].values)

    def keyof(code, name):
        # 파생/현금은 constituent_code가 비어 있음 → 종목명으로 식별
        c = "" if pd.isna(code) else str(code).strip()
        return c if c else ("@" + (name or "").strip())

    recs = []
    for etf, g in h.groupby("etf_code"):
        etf_name = g["etf_name"].iloc[0]
        byday = {}
        for d, gd in g.groupby("run_date"):
            byday[d] = {keyof(r.constituent_code, r.constituent_name):
                        (r.qty, r.constituent_code, r.constituent_name, r.kind, r.amount)
                        for r in gd.itertuples()}
        days = sorted(byday)
        for i in range(1, len(days)):
            d, dp = days[i], days[i - 1]
            cur, prev = byday[d], byday[dp]
            for key in set(cur) | set(prev):
                in_cur, in_prev = key in cur, key in prev
                cq, ccode, cname, ckind, camount = cur.get(key, (np.nan, None, None, None, None))
                pq, pcode, pname, pkind, pamount = prev.get(key, (np.nan, None, None, None, None))
                q = 0.0 if pd.isna(cq) else cq
                p = 0.0 if pd.isna(pq) else pq
                delta = q - p
                # 편입/편출은 '구성종목 존재 여부'로 판정(현금 등 qty 없는 항목의 오분류 방지)
                if not in_cur:
                    ev = "편출"
                elif not in_prev:
                    ev = "편입"
                elif delta != 0:
                    ev = "변경"
                else:
                    ev = "유지"
                if ev == "유지":
                    continue
                ca = camount if camount is not None else 0.0
                pa = pamount if pamount is not None else 0.0
                recs.append(dict(
                    base_date=rd2base.get(d), run_date=d, prev_run=dp,
                    etf_code=etf, etf_name=etf_name,
                    constituent_code=ccode if ccode is not None else pcode,
                    constituent_name=cname if cname is not None else pname,
                    kind=ckind if ckind is not None else pkind,
                    prev_qty=pq, qty=cq, delta_qty=delta, event=ev,
                    prev_amount=pamount, amount=camount, delta_amount=ca - pa))

    reb = pd.DataFrame(recs).sort_values(["run_date", "etf_code", "kind", "constituent_code"])
    if run_dates is not None:
        reb = reb[reb["run_date"].isin(run_dates)]
    save(reb, "rebalancing.csv", out_dir)

    # kind 요약: 순증감 계약수 및 이벤트 건수
    if not reb.empty:
        summ = (reb.groupby(["base_date", "run_date", "etf_code", "etf_name", "kind"])
                   .agg(net_delta_qty=("delta_qty", "sum"),
                        n_in=("event", lambda s: (s == "편입").sum()),
                        n_out=("event", lambda s: (s == "편출").sum()),
                        n_chg=("event", lambda s: (s == "변경").sum()))
                   .reset_index())
        summ["n_events"] = summ["n_in"] + summ["n_out"] + summ["n_chg"]
        save(summ.sort_values(["run_date", "etf_code", "kind"]), "rebalancing_by_kind.csv", out_dir)


if __name__ == "__main__":
    main()
