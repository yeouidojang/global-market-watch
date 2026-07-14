# -*- coding: utf-8 -*-
"""
1회성 완전 파이프라인 — 20260706~20260710 (월~금) ETF 시장 흐름 분석
  수익률/CU/AUM : xlsm Screen 시트 (20260703~20260710, 6일)
  리밸런싱      : 기존 parsed/holdings_ts.csv (20260706~20260710 포함)
  유니버스 흐름  : 기존 a6 재사용
출력: out_260713/ + etf_monitor_20260709.xlsx  (chart sheet 포함)
"""
import os
import numpy as np
import pandas as pd
from openpyxl.utils import get_column_letter

BASE   = os.path.dirname(os.path.abspath(__file__))
PARSED = os.path.join(BASE, "parsed")
OUT    = os.path.join(BASE, "out_260713")
XLSM   = os.path.join(BASE, "raw", "etf_search_etf_260713.xlsm")

MON_FRI   = ["20260706", "20260707", "20260708", "20260709", "20260710"]
BASE_DATE = "20260709"

os.makedirs(OUT, exist_ok=True)


# ════════════════════════════════════════════════════
# 유틸
# ════════════════════════════════════════════════════
def save_csv(df, name):
    p = os.path.join(OUT, name)
    df.to_csv(p, index=False, encoding="utf-8-sig")
    print(f"  -> out_260713/{name}  ({len(df):,} rows)")


def wavg(d, val, w):
    d = d.dropna(subset=[val, w])
    tw = d[w].sum()
    return (d[val] * d[w]).sum() / tw if tw > 0 else np.nan


# ════════════════════════════════════════════════════
# 1. 데이터 로드
# ════════════════════════════════════════════════════
def load_data():
    from parse_xlsm_screen import parse_screen
    print("  [xlsm] Screen 시트 파싱...")
    meta = parse_screen(XLSM, base_date=BASE_DATE)
    print(f"         {meta['etf_code'].nunique()} ETF × {meta['date'].nunique()} dates "
          f"({sorted(meta['date'].unique())[0]}~{sorted(meta['date'].unique())[-1]})")

    uni = pd.read_csv(os.path.join(PARSED, "etf_universe.csv"),
                      dtype={"base_date": str, "etf_code": str,
                             "listing_date": str, "delisting_date": str})
    holdings = pd.read_csv(os.path.join(PARSED, "holdings_ts.csv"),
                           dtype={"base_date": str, "run_date": str,
                                  "etf_code": str, "constituent_code": str})
    return meta, uni, holdings


# ════════════════════════════════════════════════════
# 2. 수익률 (a1 로직)
# ════════════════════════════════════════════════════
def compute_returns(meta, uni):
    uni_slim = uni[uni["base_date"] == BASE_DATE][["etf_code", "theme"]]
    m = meta.merge(uni_slim, on="etf_code", how="left")
    m = m.sort_values(["etf_code", "date"])
    # mktcap_prev: shift 전 계산 (20260703 Fri가 Mon의 전일로 작동)
    m["mktcap_prev"] = m.groupby("etf_code")["mktcap"].shift()
    m["w"] = m["mktcap_prev"].fillna(m["mktcap"])
    m = m[m["date"].isin(MON_FRI)]

    rows = []
    def emit(date, dim, group, d):
        d = d.dropna(subset=["ret"])
        if d.empty: return
        rows.append(dict(date=date, dim=dim, group=group,
                         n_etf=d["etf_code"].nunique(),
                         ret_simple=round(d["ret"].mean(), 4),
                         ret_capwtd=round(wavg(d, "ret", "w"), 4)))

    for date, gd in m.groupby("date"):
        emit(date, "all",   "ALL", gd)
        for t, dt  in gd.groupby(gd["etf_type"].replace("", "(빈값)")):
            emit(date, "type",  t,  dt)
        for th, dth in gd.groupby(gd["theme"].fillna("기타")):
            emit(date, "theme", th, dth)

    out = pd.DataFrame(rows).sort_values(["date", "dim", "group"])
    save_csv(out, "weekly_returns.csv")
    return out


# ════════════════════════════════════════════════════
# 3. CU 흐름 (a2 로직)
# ════════════════════════════════════════════════════
def compute_cu_flow(meta):
    m = meta.sort_values(["etf_code", "date"])
    g = m.groupby("etf_code")
    m["prev_date"]    = g["date"].shift()
    m["prev_num_cu"]  = g["num_cu"].shift()
    m["delta_num_cu"] = m["num_cu"] - m["prev_num_cu"]
    m["flow_value"]   = m["delta_num_cu"] * m["cu_value"]

    def act(x):
        if pd.isna(x): return ""
        return "설정" if x > 0 else ("환매" if x < 0 else "변동없음")
    m["action"] = m["delta_num_cu"].apply(act)

    out = m[m["date"].isin(MON_FRI)][
        ["date", "prev_date", "etf_code", "etf_name", "etf_type",
         "num_cu", "prev_num_cu", "delta_num_cu", "cu_value", "flow_value", "action"]
    ].sort_values(["date", "etf_code"])
    save_csv(out, "cu_flow.csv")
    return out


# ════════════════════════════════════════════════════
# 4. AUM (a4 로직)
# ════════════════════════════════════════════════════
def compute_aum(meta):
    m = meta.sort_values(["etf_code", "date"])
    g = m.groupby("etf_code")
    m["prev_num_cu"]  = g["num_cu"].shift()
    m["delta_num_cu"] = m["num_cu"] - m["prev_num_cu"]
    m["flow_value"]   = m["delta_num_cu"] * m["cu_value"]

    # 일별: prev_num_cu 포함 → delta 검증 가능
    daily = m[m["date"].isin(MON_FRI)][
        ["date", "etf_code", "etf_name", "etf_type",
         "prev_num_cu", "num_cu", "delta_num_cu", "cu_value", "flow_value"]
    ].sort_values(["date", "flow_value"], ascending=[True, False]).copy()
    save_csv(daily, "aum_from_cu_daily.csv")

    # 주별: start_num_cu(20260703 전주금) / end_num_cu(20260710 금) 추가
    # week_delta_num_cu = sum(daily delta) = end_num_cu - start_num_cu 로 검증
    start_nc = (meta[meta["date"] == "20260703"][["etf_code", "num_cu"]]
                .rename(columns={"num_cu": "start_num_cu"}))
    end_nc   = (meta[meta["date"] == MON_FRI[-1]][["etf_code", "num_cu"]]
                .rename(columns={"num_cu": "end_num_cu"}))

    wk = (daily.groupby(["etf_code", "etf_name", "etf_type"])
               .agg(week_delta_num_cu=("delta_num_cu", "sum"),
                    week_flow_value=("flow_value", "sum"),
                    avg_cu_value=("cu_value", "mean"))
               .reset_index())
    wk = (wk.merge(start_nc, on="etf_code", how="left")
             .merge(end_nc,   on="etf_code", how="left"))
    wk = wk.sort_values("week_flow_value", ascending=False)
    wk["rank"] = range(1, len(wk) + 1)
    # 컬럼 순서: start → end → delta 순으로 배치
    cols = ["etf_code", "etf_name", "etf_type",
            "start_num_cu", "end_num_cu", "week_delta_num_cu",
            "week_flow_value", "avg_cu_value", "rank"]
    wk = wk[[c for c in cols if c in wk.columns]]
    save_csv(wk, "aum_from_cu_weekly.csv")
    return daily, wk


# ════════════════════════════════════════════════════
# 5. 리밸런싱 (a3/a5 재사용)
# ════════════════════════════════════════════════════
def compute_rebalancing():
    import a3_rebalancing, a5_rebal_summary
    a3_rebalancing.main(out_dir=OUT, run_dates=MON_FRI)
    a5_rebal_summary.main(out_dir=OUT)

    reb = pd.read_csv(os.path.join(OUT, "rebalancing.csv"),
                      dtype={"base_date": str, "run_date": str, "etf_code": str,
                             "constituent_code": str})
    rebal_s = pd.read_csv(os.path.join(OUT, "rebal_summary.csv"),
                          dtype={"base_date": str, "run_date": str})

    _KINDS = {"stock", "futures", "option"}
    r = reb[reb["kind"].isin(_KINDS)].copy()
    for col in ("prev_qty", "qty", "delta_qty", "prev_amount", "amount", "delta_amount"):
        r[col] = pd.to_numeric(r[col], errors="coerce").fillna(0)

    # ── ETF별 prev/current 집계 → rebal_etf_daily.csv 보강 ──
    etf_agg = (r.groupby(["run_date", "etf_code"])
                .agg(total_prev_qty     =("prev_qty",    "sum"),
                     total_qty          =("qty",         "sum"),
                     total_delta_qty    =("delta_qty",   "sum"),
                     total_prev_amount  =("prev_amount", "sum"),
                     total_amount       =("amount",      "sum"),
                     total_delta_amount =("delta_amount","sum"))
                .reset_index())
    etf_agg["total_prev_amount_억"]  = (etf_agg["total_prev_amount"]  / 1e8).round(2)
    etf_agg["total_amount_억"]       = (etf_agg["total_amount"]       / 1e8).round(2)
    etf_agg["total_delta_amount_억"] = (etf_agg["total_delta_amount"] / 1e8).round(2)

    etf_d_path = os.path.join(OUT, "rebal_etf_daily.csv")
    etf_d = pd.read_csv(etf_d_path,
                        dtype={"base_date": str, "run_date": str, "etf_code": str})
    etf_d = etf_d.merge(etf_agg, on=["run_date", "etf_code"], how="left")
    etf_d.to_csv(etf_d_path, index=False, encoding="utf-8-sig")
    print(f"  -> rebal_etf_daily.csv 보강 (ETF prev/current 추가)")

    # ── 종목별 prev/current 집계 → rebal_stock_daily.csv ───
    stock_agg = (r.groupby(["run_date", "constituent_code", "constituent_name", "kind"])
                  .agg(n_etf              =("etf_code",    "nunique"),
                       total_prev_qty     =("prev_qty",    "sum"),
                       total_qty          =("qty",         "sum"),
                       total_delta_qty    =("delta_qty",   "sum"),
                       total_prev_amount  =("prev_amount", "sum"),
                       total_amount       =("amount",      "sum"),
                       total_delta_amount =("delta_amount","sum"))
                  .reset_index()
                  .sort_values(["run_date", "total_delta_qty"], ascending=[True, False]))
    stock_agg["total_prev_amount_억"]  = (stock_agg["total_prev_amount"]  / 1e8).round(2)
    stock_agg["total_amount_억"]       = (stock_agg["total_amount"]       / 1e8).round(2)
    stock_agg["total_delta_amount_억"] = (stock_agg["total_delta_amount"] / 1e8).round(2)
    save_csv(stock_agg, "rebal_stock_daily.csv")

    return reb, rebal_s


# ════════════════════════════════════════════════════
# 5b. 리밸런싱 KRW 수급 (Goal 3 & 4)
#   delta_amount(per 1CU) × num_cu = 종목별 실제 시장 수급 규모
# ════════════════════════════════════════════════════
def compute_rebal_flow(reb, meta, holdings):
    """
    rebalancing.csv의 delta_amount(1CU 기준) × num_cu(총 CU) → 종목별 KRW 수급
    - 주식: delta_amount(원본) × num_cu
    - 개별주식선물: delta_qty × 계약승수 × 기초자산주가 × num_cu  (amount 원본 NaN이므로 재구성)
    """
    import re

    def _parse_underlying(fut_name):
        m = re.match(r'\d{4}-\d{2}\s+(.+?)개별선물', str(fut_name))
        if m: return m.group(1).strip()
        m = re.match(r'(.+?)\s+F\s+\d{6}', str(fut_name))
        if m: return m.group(1).strip()
        m = re.match(r'(.+?)선물\d{4}', str(fut_name))
        if m: return m.group(1).strip()
        return None

    def _parse_multiplier(name):
        m = re.search(r'\(\s*(\d+)\s*\)', str(name))
        return int(m.group(1)) if m else 10

    # 주식 현물 주가 맵: (constituent_name, run_date) → median price
    h_stk = holdings[(holdings["kind"] == "stock") & (holdings["run_date"].isin(MON_FRI))].copy()
    h_stk["_qty"] = pd.to_numeric(h_stk["qty"], errors="coerce")
    h_stk["_amt"] = pd.to_numeric(h_stk["amount"], errors="coerce")
    h_stk["_price"] = h_stk["_amt"] / h_stk["_qty"]
    price_map = (h_stk[h_stk["_price"] > 0]
                 .groupby(["constituent_name", "run_date"])["_price"]
                 .median().to_dict())

    # num_cu 조회맵: (etf_code, run_date) → num_cu
    nc_map = (meta[meta["date"].isin(MON_FRI)]
              .set_index(["etf_code", "date"])["num_cu"]
              .to_dict())

    r = reb[reb["kind"].isin(["stock", "futures", "option"])].copy()
    r["num_cu"] = r.apply(lambda x: nc_map.get((x.etf_code, x.run_date)), axis=1)

    # 개별주식선물/옵션: delta_amount NaN → delta_qty × 승수 × 주가로 재구성
    r_stk = r[r["kind"] == "stock"].copy()
    r_fut = r[r["kind"].isin(["futures", "option"])].copy()

    if not r_fut.empty:
        r_fut["_underlying"] = r_fut["constituent_name"].apply(_parse_underlying)
        r_fut["_multiplier"] = r_fut["constituent_name"].apply(_parse_multiplier)
        r_fut["_delta_qty"]  = pd.to_numeric(r_fut["delta_qty"], errors="coerce")
        r_fut["_qty"]        = pd.to_numeric(r_fut["qty"], errors="coerce")
        r_fut["_price"]      = r_fut.apply(
            lambda row: price_map.get((row["_underlying"], row["run_date"])), axis=1)
        nan_mask = r_fut["delta_amount"].isna()
        r_fut.loc[nan_mask, "delta_amount"] = (
            r_fut.loc[nan_mask, "_delta_qty"] *
            r_fut.loc[nan_mask, "_multiplier"] *
            r_fut.loc[nan_mask, "_price"])
        r_fut.loc[nan_mask, "amount"] = (
            r_fut.loc[nan_mask, "_qty"] *
            r_fut.loc[nan_mask, "_multiplier"] *
            r_fut.loc[nan_mask, "_price"])
        n_filled = r_fut.loc[nan_mask, "delta_amount"].notna().sum()
        print(f"  [선물 notional 보정] {n_filled}/{nan_mask.sum()}행 qty×승수×주가 적용")
        r_fut = r_fut.drop(columns=["_underlying","_multiplier","_delta_qty","_qty","_price"])
        r = pd.concat([r_stk, r_fut], ignore_index=True)

    # futures/option은 constituent_code가 없으므로 constituent_name을 groupby 키로 사용
    r["constituent_code"] = r["constituent_code"].fillna(r["constituent_name"])

    r["flow_krw"] = r["delta_amount"] * r["num_cu"]
    r = r.dropna(subset=["flow_krw"])

    # ── 일별 종목별 집계 ─────────────────────────────
    # prev_amount_1cu / amount_1cu 포함 → delta_amount_1cu = amount - prev 검증
    daily = (r.groupby(["run_date", "constituent_code", "constituent_name", "kind"])
              .agg(flow_krw=("flow_krw", "sum"),
                   n_etf=("etf_code", "nunique"),
                   prev_amount_1cu=("prev_amount", "sum"),
                   amount_1cu=("amount", "sum"),
                   delta_amount_1cu=("delta_amount", "sum"))
              .reset_index()
              .sort_values(["run_date", "flow_krw"], ascending=[True, False]))
    daily["flow_krw_억"] = (daily["flow_krw"] / 1e8).round(2)
    save_csv(daily, "rebal_flow_daily.csv")

    # ── 주간 종목별 집계 ─────────────────────────────
    # week_delta_amount_1cu = 주간 누적 delta_amount 합산
    weekly = (r.groupby(["constituent_code", "constituent_name", "kind"])
               .agg(week_flow_krw=("flow_krw", "sum"),
                    n_etf=("etf_code", "nunique"),
                    n_days=("run_date", "nunique"),
                    week_delta_amount_1cu=("delta_amount", "sum"))
               .reset_index()
               .sort_values("week_flow_krw", ascending=False))
    weekly["week_flow_krw_억"] = (weekly["week_flow_krw"] / 1e8).round(2)
    save_csv(weekly, "rebal_flow_weekly.csv")

    # ── 일별 ETF별 리밸 총금액 ───────────────────────
    etf_daily = (r.groupby(["run_date", "etf_code", "etf_name"])
                  .agg(flow_krw=("flow_krw", "sum"),
                       n_events=("constituent_code", "count"))
                  .reset_index()
                  .sort_values(["run_date", "flow_krw"], ascending=[True, False]))
    etf_daily["flow_krw_억"] = (etf_daily["flow_krw"] / 1e8).round(2)
    save_csv(etf_daily, "rebal_flow_etf_daily.csv")

    print(f"  [리밸 수급] 일별 종목수 {daily['constituent_code'].nunique()}, "
          f"주간 상위 유입: {weekly.iloc[0]['constituent_name']} "
          f"{weekly.iloc[0]['week_flow_krw_억']:.1f}억")
    return daily, weekly, etf_daily


# ════════════════════════════════════════════════════
# 6. 유니버스 흐름
# ════════════════════════════════════════════════════
def compute_universe(meta, uni):
    import a6_universe_flow
    # universe_events (편입/편출 목록): a6 그대로 사용
    a6_universe_flow.main(out_dir=OUT)

    # universe_flow: xlsm meta 기반으로 직접 계산
    # (a6는 parsed 목요일 스냅샷 2개를 비교 → 동일 날짜라 flow=0)
    _compute_universe_flow(meta, uni)


def _compute_universe_flow(meta, uni):
    """xlsm meta 20260703 → 20260710 기준 유형별 AUM 흐름 (event-adjusted)."""
    START, END = "20260703", "20260710"
    uni_curr = (uni[uni["base_date"] == BASE_DATE][["etf_code", "etf_type"]]
                .rename(columns={"etf_type": "_uni_type"}))

    m = meta.merge(uni_curr, on="etf_code", how="left")
    # uni etf_type 우선, 없으면 meta etf_type
    m["etype"] = m["_uni_type"].where(m["_uni_type"].notna(), m["etf_type"])
    m["etype"] = m["etype"].fillna("").replace("", "(빈값)")

    s = m[m["date"] == START].set_index("etf_code")[["num_cu", "cu_value", "mktcap", "etype"]]
    e = m[m["date"] == END  ].set_index("etf_code")[["num_cu", "cu_value", "mktcap", "etype"]]

    cont = set(s.index) & set(e.index)
    ins   = set(e.index) - set(s.index)
    outs  = set(s.index) - set(e.index)

    cont_rows = []
    for c in sorted(cont):
        a_nc, b_nc = s.loc[c, "num_cu"], e.loc[c, "num_cu"]
        b_cv, b_mc = e.loc[c, "cu_value"], e.loc[c, "mktcap"]
        flow = (b_cv * (b_nc - a_nc)
                if pd.notna(b_cv) and pd.notna(b_nc) and pd.notna(a_nc) else 0.0)
        cont_rows.append({"etf_code": c, "etype": e.loc[c, "etype"],
                          "flow": flow, "mktcap_e": b_mc})

    cd = (pd.DataFrame(cont_rows) if cont_rows
          else pd.DataFrame(columns=["etf_code", "etype", "flow", "mktcap_e"]))
    aum_total = (e.groupby("etype")["mktcap"].sum() * 1e6).rename("aum_total")
    cont_agg  = cd.groupby("etype").agg(n_cont=("etf_code", "nunique"),
                                         flow_value=("flow", "sum"))

    all_types = sorted(set(aum_total.index) | set(cont_agg.index)
                       | {e.loc[c, "etype"] for c in ins}
                       | {s.loc[c, "etype"] for c in outs})

    rows = []
    for t in all_types:
        ct = cont_agg.loc[t] if t in cont_agg.index else None
        in_etfs  = [c for c in ins  if e.loc[c, "etype"] == t]
        out_etfs = [c for c in outs if s.loc[c, "etype"] == t]
        rows.append({
            "base_prev": START, "base": END, "etf_type": t,
            "n_cont":    int(ct["n_cont"])       if ct is not None else 0,
            "flow_value": float(ct["flow_value"]) if ct is not None else 0.0,
            "aum_total": float(aum_total.get(t, 0.0)),
            "n_in":    len(in_etfs),
            "in_aum":  float(sum(e.loc[c, "mktcap"] for c in in_etfs) * 1e6),
            "n_out":   len(out_etfs),
            "out_aum": float(sum(s.loc[c, "mktcap"] for c in out_etfs) * 1e6),
        })

    flow_df = pd.DataFrame(rows).sort_values("etf_type")
    flow_df["flow_value_억"] = (flow_df["flow_value"] / 1e8).round(1)
    flow_df["aum_total_억"]  = (flow_df["aum_total"]  / 1e8).round(0)
    save_csv(flow_df, "universe_flow.csv")

    print(f"  [유니버스 AUM] {START}→{END} 유형별 순유입(억원)")
    for r in flow_df.itertuples():
        print(f"    {r.etf_type}: {r.flow_value_억:+.1f}억 | AUM {r.aum_total_억:.0f}억 | "
              f"연속 {r.n_cont}개 / 신규 {r.n_in}개 / 이탈 {r.n_out}개")


# ════════════════════════════════════════════════════
# 7. 차트용 피벗 테이블
# ════════════════════════════════════════════════════
def build_chart_tables(returns, cu_flow, aum_wk, reb, rebal_s, meta, uni):
    frames = {}

    # ── C1: 테마 × 날짜 수익률 (단순평균, 전체 유형) ──────
    theme_ret = (returns[returns["dim"] == "theme"]
                 .pivot(index="group", columns="date", values="ret_simple")
                 .rename_axis("theme")
                 .rename_axis(None, axis=1))
    theme_ret.columns = [str(c) for c in theme_ret.columns]
    theme_ret["주간평균"] = theme_ret[MON_FRI].mean(axis=1)
    theme_ret = theme_ret.sort_values("주간평균", ascending=False).reset_index()
    frames["C_수익률_테마"] = theme_ret

    # ── C1b: 테마 × 날짜 수익률 (일반 ETF만) ─────────────
    uni_slim_c = uni[uni["base_date"] == BASE_DATE][["etf_code", "theme"]]
    m_일반 = (meta[meta["date"].isin(MON_FRI)]
               .merge(uni_slim_c, on="etf_code", how="left"))
    m_일반 = m_일반[(m_일반["etf_type"] == "일반")].dropna(subset=["ret"])
    if not m_일반.empty:
        tr_일반 = (m_일반.groupby(["theme", "date"])["ret"]
                   .mean().round(4).reset_index()
                   .pivot(index="theme", columns="date", values="ret")
                   .rename_axis(None, axis=1))
        tr_일반.columns = [str(c) for c in tr_일반.columns]
        tr_일반["주간평균"] = tr_일반[[c for c in MON_FRI if c in tr_일반.columns]].mean(axis=1)
        tr_일반 = (tr_일반.sort_values("주간평균", ascending=False)
                   .reset_index().rename(columns={"theme": "테마"}))
        frames["C_수익률_테마_일반"] = tr_일반

    # ── C2: 유형 × 날짜 수익률 (시총가중) ─────────────
    type_ret = (returns[returns["dim"] == "type"]
                .pivot(index="group", columns="date", values="ret_capwtd")
                .rename_axis("etf_type")
                .rename_axis(None, axis=1))
    type_ret.columns = [str(c) for c in type_ret.columns]
    type_ret["주간평균"] = type_ret[MON_FRI].mean(axis=1)
    type_ret = type_ret.sort_values("주간평균", ascending=False).reset_index()
    frames["C_수익률_유형"] = type_ret

    # ── C3: 유형 × 날짜 CU 순유입 (억원) ──────────────
    cu_uni = cu_flow.merge(
        uni[uni["base_date"] == BASE_DATE][["etf_code", "etf_type"]],
        on="etf_code", how="left", suffixes=("", "_u")
    )
    cu_uni["etf_type"] = cu_uni["etf_type"].fillna(cu_uni.get("etf_type_u", ""))
    cu_type = (cu_uni.groupby(["etf_type", "date"])["flow_value"]
               .sum().div(1e8)
               .reset_index()
               .pivot(index="etf_type", columns="date", values="flow_value")
               .rename_axis(None, axis=1))
    cu_type.columns = [str(c) for c in cu_type.columns]
    cu_type["주간합계"] = cu_type[[c for c in MON_FRI if c in cu_type.columns]].sum(axis=1)
    cu_type = cu_type.sort_values("주간합계", ascending=False).reset_index()
    frames["C_CU순유입_유형(억)"] = cu_type

    # ── C4: CU 순유입 상위 ETF (주간 합산) — start/end 포함
    top_aum = aum_wk.head(30)[["etf_code", "etf_name", "etf_type",
                                "start_num_cu", "end_num_cu", "week_delta_num_cu",
                                "week_flow_value", "avg_cu_value", "rank"]].copy()
    top_aum["week_flow_value_억"] = (top_aum["week_flow_value"] / 1e8).round(1)
    frames["C_AUM유입상위ETF"] = top_aum

    # ── C5: 일별 리밸런싱 현황 ─────────────────────────
    frames["C_리밸일별요약"] = rebal_s[["run_date", "n_rebalanced_etf",
                                        "total_events", "representative_etfs"]]

    # ── C6: 신규상장 ETF ───────────────────────────────
    bases = sorted(uni["base_date"].unique())
    curr  = bases[-1]
    ever_before = set(uni[uni["base_date"] < curr]["etf_code"])
    new = uni[(uni["base_date"] == curr) & (~uni["etf_code"].isin(ever_before))].copy()
    if not new.empty:
        snap = (meta[meta["date"] == meta["date"].max()]
                .set_index("etf_code")[["mktcap", "cu_value", "close"]])
        new = new.merge(snap, on="etf_code", how="left")
    want = ["etf_code", "etf_name", "listing_date", "etf_type", "theme",
            "base_index", "mktcap", "cu_value", "close"]
    frames["신규상장ETF"] = new[[c for c in want if c in new.columns]].sort_values(
        "listing_date", na_position="last").reset_index(drop=True)

    for name, df in frames.items():
        save_csv(df, f"{name}.csv")

    return frames


# ════════════════════════════════════════════════════
# 7b. 추가 차트/표 데이터
# ════════════════════════════════════════════════════
def build_extra_chart_tables(cu_flow, aum_wk, rebal_flow_daily, uni, meta):
    frames = {}
    uni_curr  = uni[uni["base_date"] == BASE_DATE][["etf_code", "theme", "etf_type"]]
    uni_theme = uni_curr[["etf_code", "theme"]]

    # ── EC1: 주간 ETF 수익률 상위/하위 (금→금 close 기준) ────
    s = (meta[meta["date"] == "20260703"]
         .set_index("etf_code")[["etf_name", "etf_type", "close", "mktcap"]])
    e = (meta[meta["date"] == "20260710"]
         .set_index("etf_code")[["close", "mktcap"]])
    ret_df = (s.merge(e, left_index=True, right_index=True, suffixes=("_s", "_e"))
               .dropna(subset=["close_s", "close_e"]))
    ret_df["weekly_ret_%"] = ((ret_df["close_e"] / ret_df["close_s"] - 1) * 100).round(2)
    ret_df = (ret_df.merge(uni_theme.set_index("etf_code"), left_index=True,
                           right_index=True, how="left")
              .reset_index().rename(columns={"index": "etf_code", "etf_code": "etf_code"}))
    want = ["etf_code", "etf_name", "etf_type", "theme", "weekly_ret_%", "close_s", "close_e", "mktcap_e"]
    ret_df = ret_df[[c for c in want if c in ret_df.columns]].rename(
        columns={"mktcap_e": "mktcap_백만"})
    frames["C_수익률상위ETF"] = ret_df.nlargest(20, "weekly_ret_%").reset_index(drop=True)
    frames["C_수익률하위ETF"] = ret_df.nsmallest(20, "weekly_ret_%").reset_index(drop=True)

    # ── EC1b: 일반 ETF only 수익률 상위/하위 ─────────────
    ret_df_일반 = ret_df[ret_df["etf_type"] == "일반"].copy()
    frames["C_수익률상위ETF_일반"] = ret_df_일반.nlargest(20, "weekly_ret_%").reset_index(drop=True)
    frames["C_수익률하위ETF_일반"] = ret_df_일반.nsmallest(20, "weekly_ret_%").reset_index(drop=True)

    # ── EC2: 테마별 × 날짜 CU 자금유입 pivot (억원) ───────
    cu_t = cu_flow.merge(uni_theme, on="etf_code", how="left")
    cu_t["theme"] = cu_t["theme"].fillna("기타")
    ct_piv = (cu_t.groupby(["theme", "date"])["flow_value"]
               .sum().div(1e8).reset_index()
               .pivot(index="theme", columns="date", values="flow_value")
               .rename_axis(None, axis=1))
    ct_piv.columns = [str(c) for c in ct_piv.columns]
    ct_piv["주간합계"] = ct_piv[[c for c in MON_FRI if c in ct_piv.columns]].sum(axis=1)
    ct_piv = (ct_piv.sort_values("주간합계", ascending=False)
              .reset_index().rename(columns={"theme": "테마"}))
    frames["C_CU테마별(억)"] = ct_piv

    # ── EC3: 일별 설정/환매 ETF수 + 순유입(억원) ────────────
    def _agg_act(g):
        return pd.Series({
            "n_설정":      (g["action"] == "설정").sum(),
            "n_환매":      (g["action"] == "환매").sum(),
            "n_변동없음":  (g["action"] == "변동없음").sum(),
            "설정금액_억": round(g.loc[g["action"] == "설정",  "flow_value"].sum() / 1e8, 1),
            "환매금액_억": round(g.loc[g["action"] == "환매",  "flow_value"].sum() / 1e8, 1),
            "순유입_억":   round(g["flow_value"].sum() / 1e8, 1),
        })
    daily_act = cu_flow.groupby("date").apply(_agg_act).reset_index().sort_values("date")
    frames["C_설정환매일별"] = daily_act

    # ── EC4: 테마별 주간 AUM 유입 (억원, sorted) ─────────────
    aum_theme = (cu_flow.merge(uni_theme, on="etf_code", how="left")
                 .assign(theme=lambda d: d["theme"].fillna("기타"))
                 .groupby("theme")["flow_value"].sum().div(1e8).round(1)
                 .reset_index(name="주간_AUM유입_억")
                 .sort_values("주간_AUM유입_억", ascending=False)
                 .rename(columns={"theme": "테마"}))
    frames["C_AUM테마주간"] = aum_theme

    # ── EC5: AUM 유출 하위 20 ETF — start/end 포함
    aum_out = (aum_wk[aum_wk["week_flow_value"] < 0]
               .nsmallest(20, "week_flow_value").copy())
    aum_out["week_flow_value_억"] = (aum_out["week_flow_value"] / 1e8).round(1)
    aum_out["rank_out"] = range(1, len(aum_out) + 1)
    frames["C_AUM유출하위ETF"] = aum_out[["etf_code", "etf_name", "etf_type",
                                          "start_num_cu", "end_num_cu", "week_delta_num_cu",
                                          "week_flow_value_억", "rank_out"]].reset_index(drop=True)

    # ── EC6: kind별(주식/선물/옵션) × 날짜 리밸수급(억) pivot ─
    if not rebal_flow_daily.empty:
        kp = (rebal_flow_daily.groupby(["kind", "run_date"])["flow_krw_억"]
               .sum().reset_index()
               .pivot(index="kind", columns="run_date", values="flow_krw_억")
               .rename_axis(None, axis=1).fillna(0))
        kp.columns = [str(c) for c in kp.columns]
        kp["주간합계"] = kp[[c for c in MON_FRI if c in kp.columns]].sum(axis=1)
        frames["C_리밸수급kind"] = (kp.sort_values("주간합계", ascending=False)
                                    .reset_index().rename(columns={"kind": "종류"}))

    # ── EC8: 리밸 수급 일별 유입/유출/순유입 두 버전 (Chart용) ──
    if not rebal_flow_daily.empty:
        def _day_agg(df):
            pos = df[df["flow_krw_억"] > 0].groupby("run_date")["flow_krw_억"].sum().rename("유입_억")
            neg = df[df["flow_krw_억"] < 0].groupby("run_date")["flow_krw_억"].sum().rename("유출_억")
            net = df.groupby("run_date")["flow_krw_억"].sum().rename("순유입_억")
            out = (pd.concat([pos, neg, net], axis=1).fillna(0).round(2)
                   .reset_index().rename(columns={"run_date": "날짜"}))
            return out[out["날짜"].isin(MON_FRI)].sort_values("날짜").reset_index(drop=True)

        # 버전1: 선물/옵션 포함 전체
        frames["C_리밸수급일별_전체"] = _day_agg(rebal_flow_daily)
        # 버전2: 주식 현물만
        frames["C_리밸수급일별_주식"] = _day_agg(
            rebal_flow_daily[rebal_flow_daily["kind"] == "stock"])

    # ── EC7: 유니버스 AUM 요약 (유형별 깔끔 테이블) ──────────
    try:
        uf = pd.read_csv(os.path.join(OUT, "universe_flow.csv"))
        uf_c = uf[["etf_type", "n_cont", "aum_total_억", "flow_value_억",
                   "n_in", "in_aum", "n_out", "out_aum"]].copy()
        uf_c["편입AUM(억)"] = (uf_c["in_aum"]  / 1e8).round(1)
        uf_c["편출AUM(억)"] = (uf_c["out_aum"] / 1e8).round(1)
        uf_c = uf_c.drop(columns=["in_aum", "out_aum"])
        uf_c.columns = ["유형", "연속ETF수", "현재AUM(억)", "주간순유입(억)",
                        "편입ETF수", "편출ETF수", "편입AUM(억)", "편출AUM(억)"]
        frames["C_유니버스AUM요약"] = uf_c
    except Exception:
        pass

    for name, df in frames.items():
        save_csv(df, f"{name}.csv")

    print(f"  [추가차트] {len(frames)}개 테이블 생성 완료")
    return frames


# ════════════════════════════════════════════════════
# 투자전략 분석
# ════════════════════════════════════════════════════
FRIDAY = "20260710"   # 옵션만기(20260709 목요일) 다음 영업일


def compute_strategy(cu_flow, rebal_wk, rebal_daily, aum_wk, uni, meta, reb):
    frames = {}
    uni_curr = uni[uni["base_date"] == BASE_DATE][["etf_code", "theme", "etf_type"]]
    uni_theme = uni_curr[["etf_code", "theme"]]   # etf_type은 cu_flow에 이미 있음

    # (etf_code, run_date) → num_cu map (배경 시트 flow_krw 재계산용)
    nc_map = (meta[meta["date"].isin(MON_FRI)]
              .set_index(["etf_code", "date"])["num_cu"].to_dict())

    # ── 전략1: 리밸런싱 수급 유입 종목 ────────────────
    #  delta_amount × num_cu 합산 기준 상위 종목
    inflow = rebal_wk[rebal_wk["week_flow_krw"] > 0].head(50).copy()
    outflow = rebal_wk[rebal_wk["week_flow_krw"] < 0].tail(30).copy()
    frames["전략1_리밸수급유입종목"] = inflow[["constituent_code", "constituent_name", "kind",
                                               "week_flow_krw_억", "n_etf", "n_days"]]
    frames["전략1_리밸수급유출종목"] = outflow[["constituent_code", "constituent_name", "kind",
                                               "week_flow_krw_억", "n_etf", "n_days"]]

    # 주식 현물만 (futures 롤오버 노이즈 제거)
    _stk_cols = ["constituent_code", "constituent_name", "kind", "week_flow_krw_억", "n_etf", "n_days"]
    inflow_stk  = rebal_wk[(rebal_wk["week_flow_krw"] > 0) & (rebal_wk["kind"] == "stock")].head(50).copy()
    outflow_stk = rebal_wk[(rebal_wk["week_flow_krw"] < 0) & (rebal_wk["kind"] == "stock")].tail(30).copy()
    frames["전략1_리밸수급유입종목_주식"] = inflow_stk[_stk_cols]
    frames["전략1_리밸수급유출종목_주식"] = outflow_stk[_stk_cols]

    # ── 전략1 배경: 유입 종목 → ETF별 일별 리밸 상세 ─────────────────
    _inflow_codes = set(inflow["constituent_code"].dropna())
    _KINDS_STK = {"stock", "futures", "option"}
    bg1 = reb[(reb["constituent_code"].isin(_inflow_codes)) &
              (reb["kind"].isin(_KINDS_STK))].copy()
    for _c in ("prev_qty", "qty", "delta_qty", "prev_amount", "amount", "delta_amount"):
        bg1[_c] = pd.to_numeric(bg1[_c], errors="coerce").fillna(0)
    bg1["num_cu"]          = bg1.apply(
        lambda x: nc_map.get((x["etf_code"], x["run_date"])), axis=1)
    bg1["flow_krw_억"]     = (bg1["delta_amount"] * bg1["num_cu"] / 1e8).round(2)
    bg1["prev_amount_억"]  = (bg1["prev_amount"] / 1e8).round(2)
    bg1["amount_억"]       = (bg1["amount"] / 1e8).round(2)
    bg1["delta_amount_억"] = (bg1["delta_amount"] / 1e8).round(2)
    _want_bg1 = ["constituent_code", "constituent_name", "run_date",
                 "etf_code", "etf_name", "kind", "event",
                 "prev_qty", "qty", "delta_qty",
                 "prev_amount_억", "amount_억", "delta_amount_억",
                 "num_cu", "flow_krw_억"]
    frames["전략1_배경_유입종목리밸상세"] = (
        bg1[[c for c in _want_bg1 if c in bg1.columns]]
        .sort_values(["constituent_code", "run_date", "flow_krw_억"],
                     ascending=[True, True, False])
        .reset_index(drop=True))

    # 일별 리밸 수급 유입 상위 (chart용 피벗)
    _top_codes = set(inflow.head(20)["constituent_code"])
    pivot_d = (rebal_daily[rebal_daily["constituent_code"].isin(_top_codes)]
               .pivot_table(index="constituent_name", columns="run_date",
                            values="flow_krw_억", aggfunc="sum", fill_value=0)
               .rename_axis(None, axis=1)
               .reset_index())
    frames["전략1_C_리밸수급일별"] = pivot_d

    # ── 전략2: 금요일(옵션만기 다음날) CU 유입 ETF ──────
    fri = cu_flow[cu_flow["date"] == FRIDAY].copy()
    fri = fri.merge(uni_theme, on="etf_code", how="left")
    fri["flow_value_억"] = (fri["flow_value"] / 1e8).round(2)

    fri_in  = fri[fri["flow_value"] > 0].sort_values("flow_value", ascending=False)
    fri_out = fri[fri["flow_value"] < 0].sort_values("flow_value")

    frames["전략2_금요일CU유입ETF"] = fri_in[["etf_code", "etf_name", "etf_type", "theme",
                                               "delta_num_cu", "flow_value", "flow_value_억"]]
    frames["전략2_금요일CU유출ETF"] = fri_out[["etf_code", "etf_name", "etf_type", "theme",
                                               "delta_num_cu", "flow_value", "flow_value_억"]]

    # 금요일 테마별 순유입
    fri_theme = (fri.groupby("theme")["flow_value"]
                 .sum().div(1e8).round(2)
                 .reset_index(name="금요일_flow_억")
                 .sort_values("금요일_flow_억", ascending=False))
    frames["전략2_C_금요일테마유입"] = fri_theme

    # 금요일 유형별 순유입
    fri_type = (fri.groupby("etf_type")["flow_value"]
                .sum().div(1e8).round(2)
                .reset_index(name="금요일_flow_억")
                .sort_values("금요일_flow_억", ascending=False))
    frames["전략2_C_금요일유형유입"] = fri_type

    # ── 전략2 배경: CU 유입 ETF → 주간 일별 CU 흐름 ─────────────────
    bg2 = cu_flow[cu_flow["etf_code"].isin(set(fri_in["etf_code"]))].copy()
    bg2 = bg2.merge(uni_theme, on="etf_code", how="left")
    bg2["flow_value_억"] = (bg2["flow_value"] / 1e8).round(2)
    _want_bg2 = ["etf_code", "etf_name", "etf_type", "theme",
                 "date", "prev_num_cu", "num_cu", "delta_num_cu",
                 "cu_value", "flow_value_억", "action"]
    frames["전략2_배경_CU유입ETF주간흐름"] = (
        bg2[[c for c in _want_bg2 if c in bg2.columns]]
        .sort_values(["etf_code", "date"])
        .reset_index(drop=True))

    # ── 전략3: 리밸 수급 + 금요일 CU 유입 교집합 ─────────
    # 금요일 CU 유입 테마 × 해당 테마 ETF 구성종목 중 리밸 수급 유입 종목
    # (ETF → theme 매핑 후 리밸 수급 집계)
    # ETF별 리밸 총유입(금요일 기준)
    fri_rebal_etf = (rebal_daily[rebal_daily["run_date"] == FRIDAY]
                     .groupby(["constituent_code", "constituent_name", "kind"])["flow_krw"]
                     .sum().div(1e8).round(2)
                     .reset_index(name="fri_rebal_억")
                     .sort_values("fri_rebal_억", ascending=False))
    frames["전략3_금요일리밸수급유입종목"] = fri_rebal_etf[fri_rebal_etf["fri_rebal_억"] > 0].head(40)

    # 금요일 CU 유입 + 금요일 리밸 수급 유입이 겹치는 ETF
    fri_cu_in_codes = set(fri_in["etf_code"])
    fri_reb_etf_daily = (rebal_daily[rebal_daily["run_date"] == FRIDAY])
    if not fri_reb_etf_daily.empty:
        # etf_code 컬럼은 rebal_flow_etf_daily에 있음 → 로드
        edf_path = os.path.join(OUT, "rebal_flow_etf_daily.csv")
        if os.path.exists(edf_path):
            edf = pd.read_csv(edf_path, dtype={"etf_code": str, "run_date": str})
            fri_reb_etf = edf[(edf["run_date"] == FRIDAY) & (edf["flow_krw"] > 0)]
            overlap = fri_reb_etf[fri_reb_etf["etf_code"].isin(fri_cu_in_codes)].copy()
            overlap = overlap.merge(uni_curr[["etf_code", "theme", "etf_type"]], on="etf_code", how="left")
            overlap = overlap.sort_values("flow_krw", ascending=False)
            overlap["flow_krw_억"] = (overlap["flow_krw"] / 1e8).round(2)
            frames["전략3_금요일CU+리밸동시유입ETF"] = overlap[
                ["etf_code", "etf_name", "etf_type", "theme", "flow_krw_억", "n_events"]]

            # ── 전략3 배경: 동시유입 ETF → 금요일 리밸 종목 상세 ──────
            _ov_codes = set(overlap["etf_code"])
            bg3 = reb[(reb["etf_code"].isin(_ov_codes)) &
                      (reb["run_date"] == FRIDAY)].copy()
            for _c in ("prev_qty", "qty", "delta_qty", "prev_amount", "amount", "delta_amount"):
                bg3[_c] = pd.to_numeric(bg3[_c], errors="coerce").fillna(0)
            bg3["num_cu"]          = bg3.apply(
                lambda x: nc_map.get((x["etf_code"], x["run_date"])), axis=1)
            bg3["flow_krw_억"]     = (bg3["delta_amount"] * bg3["num_cu"] / 1e8).round(2)
            bg3["prev_amount_억"]  = (bg3["prev_amount"] / 1e8).round(2)
            bg3["amount_억"]       = (bg3["amount"] / 1e8).round(2)
            bg3["delta_amount_억"] = (bg3["delta_amount"] / 1e8).round(2)
            _fri_cu = (cu_flow[cu_flow["date"] == FRIDAY]
                       [["etf_code", "delta_num_cu", "flow_value"]]
                       .assign(fri_cu_flow_억=lambda d: (d["flow_value"] / 1e8).round(2))
                       .rename(columns={"delta_num_cu": "fri_delta_num_cu"})
                       [["etf_code", "fri_delta_num_cu", "fri_cu_flow_억"]])
            bg3 = bg3.merge(_fri_cu, on="etf_code", how="left")
            _want_bg3 = ["etf_code", "etf_name", "constituent_code", "constituent_name",
                         "kind", "event", "prev_qty", "qty", "delta_qty",
                         "prev_amount_억", "amount_억", "delta_amount_억",
                         "num_cu", "flow_krw_억", "fri_delta_num_cu", "fri_cu_flow_억"]
            frames["전략3_배경_동시유입ETF리밸상세"] = (
                bg3[[c for c in _want_bg3 if c in bg3.columns]]
                .sort_values(["etf_code", "flow_krw_억"], ascending=[True, False])
                .reset_index(drop=True))

    for name, df in frames.items():
        save_csv(df, f"{name}.csv")

    print(f"  [전략1] 리밸 수급 유입 종목 수: {len(inflow)}")
    print(f"  [전략2] 금요일 CU 유입 ETF: {len(fri_in)}")
    print(f"  [전략3] 금요일 CU+리밸 동시유입 ETF: "
          f"{len(frames.get('전략3_금요일CU+리밸동시유입ETF', pd.DataFrame()))}")
    return frames


# ════════════════════════════════════════════════════
# 8. xlsx 내보내기
# ════════════════════════════════════════════════════
SHEET_ORDER = [
    # ── 1~6: 기본 분석 ──────────────────────────────────────────
    ("1_수익률",          "weekly_returns.csv",               {"date": str}),
    ("2_CU흐름",          "cu_flow.csv",                      {"date": str, "prev_date": str, "etf_code": str}),
    ("3_리밸런싱",        "rebalancing.csv",                   {"base_date": str, "run_date": str,
                                                                "prev_run": str, "etf_code": str,
                                                                "constituent_code": str}),
    ("3b_리밸수급일별",   "rebal_flow_daily.csv",              {"run_date": str, "constituent_code": str}),
    ("3b_리밸수급주간",   "rebal_flow_weekly.csv",             {"constituent_code": str}),
    ("3b_리밸수급ETF일별","rebal_flow_etf_daily.csv",          {"run_date": str, "etf_code": str}),
    ("4_AUM일별",         "aum_from_cu_daily.csv",            {"date": str, "etf_code": str}),
    ("4_AUM주별",         "aum_from_cu_weekly.csv",           {"etf_code": str}),
    ("5_리밸요약",        "rebal_summary.csv",                 {"base_date": str, "run_date": str}),
    ("5_리밸ETF별",       "rebal_etf_daily.csv",               {"base_date": str, "run_date": str,
                                                                "etf_code": str}),
    ("5b_리밸종목일별",   "rebal_stock_daily.csv",             {"run_date": str,
                                                                "constituent_code": str}),
    ("6_유니버스흐름",    "universe_flow.csv",                 {"base_prev": str, "base": str}),
    ("6_유니버스이벤트",  "universe_events.csv",               {"base_prev": str, "base": str, "etf_code": str}),
    # ── 차트 피벗 (기본) ─────────────────────────────────────
    ("C_수익률_테마",     "C_수익률_테마.csv",                  {}),
    ("C_수익률_테마_일반","C_수익률_테마_일반.csv",              {}),
    ("C_수익률_유형",     "C_수익률_유형.csv",                  {}),
    ("C_CU순유입_유형",   "C_CU순유입_유형(억).csv",            {}),
    ("C_AUM유입상위",     "C_AUM유입상위ETF.csv",               {"etf_code": str}),
    ("C_리밸일별요약",    "C_리밸일별요약.csv",                 {"run_date": str}),
    ("신규상장ETF",       "신규상장ETF.csv",                    {"etf_code": str,
                                                                "listing_date": str}),
    # ── 차트 피벗 (추가) ─────────────────────────────────────
    ("C_수익률상위ETF",   "C_수익률상위ETF.csv",                {"etf_code": str}),
    ("C_수익률하위ETF",   "C_수익률하위ETF.csv",                {"etf_code": str}),
    ("C_수익률상위ETF_일반","C_수익률상위ETF_일반.csv",          {"etf_code": str}),
    ("C_수익률하위ETF_일반","C_수익률하위ETF_일반.csv",          {"etf_code": str}),
    ("C_CU테마별",        "C_CU테마별(억).csv",                 {}),
    ("C_설정환매일별",    "C_설정환매일별.csv",                  {"date": str}),
    ("C_AUM테마주간",     "C_AUM테마주간.csv",                  {}),
    ("C_AUM유출하위ETF",  "C_AUM유출하위ETF.csv",               {"etf_code": str}),
    ("C_리밸수급kind",    "C_리밸수급kind.csv",                  {}),
    ("C_리밸수급일별_전체","C_리밸수급일별_전체.csv",             {"날짜": str}),
    ("C_리밸수급일별_주식","C_리밸수급일별_주식.csv",             {"날짜": str}),
    ("C_유니버스AUM요약", "C_유니버스AUM요약.csv",               {}),
    # ── 투자전략 ──────────────────────────────────────────────
    ("전략1_리밸수급유입","전략1_리밸수급유입종목.csv",          {"constituent_code": str}),
    ("전략1_리밸수급유출","전략1_리밸수급유출종목.csv",          {"constituent_code": str}),
    ("전략1_수급유입_주식","전략1_리밸수급유입종목_주식.csv",    {"constituent_code": str}),
    ("전략1_수급유출_주식","전략1_리밸수급유출종목_주식.csv",    {"constituent_code": str}),
    ("전략1_C_수급일별",  "전략1_C_리밸수급일별.csv",           {}),
    ("전략1_배경_유입리밸","전략1_배경_유입종목리밸상세.csv",   {"constituent_code": str,
                                                                "etf_code": str, "run_date": str}),
    ("전략2_금CU유입ETF", "전략2_금요일CU유입ETF.csv",          {"etf_code": str}),
    ("전략2_금CU유출ETF", "전략2_금요일CU유출ETF.csv",          {"etf_code": str}),
    ("전략2_C_테마유입",  "전략2_C_금요일테마유입.csv",         {}),
    ("전략2_C_유형유입",  "전략2_C_금요일유형유입.csv",         {}),
    ("전략2_배경_CU흐름", "전략2_배경_CU유입ETF주간흐름.csv",  {"etf_code": str, "date": str}),
    ("전략3_금리밸수급",  "전략3_금요일리밸수급유입종목.csv",    {"constituent_code": str}),
    ("전략3_CU+리밸ETF",  "전략3_금요일CU+리밸동시유입ETF.csv", {"etf_code": str}),
    ("전략3_배경_동시리밸","전략3_배경_동시유입ETF리밸상세.csv",{"etf_code": str,
                                                                "constituent_code": str}),
]
_W_MIN, _W_MAX = 8, 42


def _cw(v):
    if v is None: return 0
    return sum(2 if ord(c) > 0x3000 else 1 for c in str(v))


def _style(ws):
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col in ws.columns:
        w = max((_cw(c.value) for c in col), default=0)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(w + 2, _W_MIN), _W_MAX)


def export_xlsx():
    out_path = os.path.join(OUT, f"etf_monitor_{BASE_DATE}.xlsx")
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        for sheet, fname, dtype in SHEET_ORDER:
            path = os.path.join(OUT, fname)
            if not os.path.exists(path):
                print(f"  skip: {fname}")
                continue
            df = pd.read_csv(path, dtype=dtype)
            df.to_excel(writer, sheet_name=sheet, index=False)
            _style(writer.sheets[sheet])
    print(f"\n  -> out_260713/etf_monitor_{BASE_DATE}.xlsx")


# ════════════════════════════════════════════════════
# main
# ════════════════════════════════════════════════════
def main():
    print("\n########## 데이터 로드 ##########")
    meta, uni, holdings = load_data()

    print("\n########## 수익률 (a1) ##########")
    returns = compute_returns(meta, uni)

    print("\n########## CU 흐름 (a2) ##########")
    cu_flow = compute_cu_flow(meta)

    print("\n########## AUM (a4) ##########")
    aum_daily, aum_wk = compute_aum(meta)

    print("\n########## 리밸런싱 (a3/a5) ##########")
    reb, rebal_s = compute_rebalancing()

    print("\n########## 리밸 수급 KRW (Goal 3/4) ##########")
    rebal_flow_daily, rebal_flow_wk, rebal_flow_etf_daily = compute_rebal_flow(reb, meta, holdings)

    print("\n########## 유니버스 흐름 (a6) ##########")
    compute_universe(meta, uni)

    print("\n########## 차트 테이블 빌드 ##########")
    build_chart_tables(returns, cu_flow, aum_wk, reb, rebal_s, meta, uni)

    print("\n########## 추가 차트/표 데이터 ##########")
    build_extra_chart_tables(cu_flow, aum_wk, rebal_flow_daily, uni, meta)

    print("\n########## 투자전략 ##########")
    compute_strategy(cu_flow, rebal_flow_wk, rebal_flow_daily, aum_wk, uni, meta, reb)

    print("\n########## xlsx 내보내기 ##########")
    export_xlsx()

    print("\n완료 -> out_260713/")


if __name__ == "__main__":
    main()
