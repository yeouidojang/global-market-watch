# -*- coding: utf-8 -*-
"""
out/ 결과 CSV → 단일 xlsx (시트별 1~6번 대응 + 신규상장 ETF)
출력: {out_dir}/etf_monitor_{max_base_date}.xlsx
"""
import os
import pandas as pd
from openpyxl.utils import get_column_letter
from _common import OUT, PARSED

# (시트명, 파일명, dtype 오버라이드)
SHEETS = [
    ("1_수익률",         "weekly_returns.csv",      {"base_date": str, "date": str}),
    ("2_CU흐름",         "cu_flow.csv",             {"base_date": str, "date": str,
                                                     "prev_date": str, "etf_code": str}),
    ("3_리밸런싱",       "rebalancing.csv",          {"base_date": str, "run_date": str,
                                                     "prev_run": str, "etf_code": str,
                                                     "constituent_code": str}),
    ("4_AUM일별",        "aum_from_cu_daily.csv",   {"base_date": str, "date": str,
                                                     "etf_code": str}),
    ("4_AUM주별",        "aum_from_cu_weekly.csv",  {"base_date": str, "etf_code": str}),
    ("5_리밸요약",       "rebal_summary.csv",        {"base_date": str, "run_date": str}),
    ("5_리밸ETF별",      "rebal_etf_daily.csv",      {"base_date": str, "run_date": str,
                                                     "etf_code": str}),
    ("6_유니버스흐름",   "universe_flow.csv",        {"base_prev": str, "base": str}),
    ("6_유니버스이벤트", "universe_events.csv",       {"base_prev": str, "base": str,
                                                     "etf_code": str}),
]

_W_MIN, _W_MAX = 8, 42


def _apply_sheet_style(ws):
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col in ws.columns:
        w = max((_len(c.value) for c in col), default=0)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(w + 2, _W_MIN), _W_MAX)


def _len(v):
    if v is None:
        return 0
    s = str(v)
    return sum(2 if ord(c) > 0x3000 else 1 for c in s)


def _build_new_listings():
    """신규상장 ETF: 이전 base_date에 없다가 최신 base_date에 처음 편입된 ETF."""
    uni_path = os.path.join(PARSED, "etf_universe.csv")
    meta_path = os.path.join(PARSED, "etf_meta_ts.csv")
    if not os.path.exists(uni_path):
        return pd.DataFrame()

    uni = pd.read_csv(uni_path, dtype={"base_date": str, "etf_code": str,
                                        "listing_date": str, "delisting_date": str})
    bases = sorted(uni["base_date"].unique())
    if len(bases) < 1:
        return pd.DataFrame()

    curr_base = bases[-1]
    # 현재 base 이전에 한 번도 나타난 적 없는 ETF = 신규상장
    ever_before = set(uni[uni["base_date"] < curr_base]["etf_code"])
    curr = uni[uni["base_date"] == curr_base].copy()
    new = curr[~curr["etf_code"].isin(ever_before)].copy()

    if new.empty:
        return new

    # 최신 mktcap / cu_value 붙이기
    if os.path.exists(meta_path):
        meta = pd.read_csv(meta_path, dtype={"base_date": str, "etf_code": str, "date": str})
        snap = (meta[meta["base_date"] == curr_base]
                .sort_values("date")
                .groupby("etf_code")[["mktcap", "cu_value", "close"]]
                .last()
                .reset_index())
        new = new.merge(snap, on="etf_code", how="left")

    want = ["etf_code", "etf_name", "listing_date", "etf_type", "theme",
            "base_index", "mktcap", "cu_value", "close"]
    cols = [c for c in want if c in new.columns]
    return new[cols].sort_values("listing_date", na_position="last").reset_index(drop=True)


def main(out_dir=None):
    src = out_dir or OUT
    frames = {}
    for sheet_name, fname, dtype in SHEETS:
        path = os.path.join(src, fname)
        if not os.path.exists(path):
            print(f"  skip (없음): {fname}")
            continue
        df = pd.read_csv(path, dtype=dtype)
        frames[sheet_name] = df
        print(f"  read  {fname:<30s} {len(df):>7,} rows  →  [{sheet_name}]")

    # 신규상장 ETF 시트 (parsed/ 기반, 항상 추가)
    new_listings = _build_new_listings()
    if not new_listings.empty:
        frames["신규상장ETF"] = new_listings
        print(f"  build 신규상장ETF{'':<22s} {len(new_listings):>7,} rows  →  [신규상장ETF]")
    else:
        print("  신규상장ETF: 해당 없음")

    # 가장 최신 base_date로 파일명 결정
    max_base = max(
        (df["base_date"].max() for df in frames.values() if "base_date" in df.columns),
        default="unknown",
    )
    out_path = os.path.join(src, f"etf_monitor_{max_base}.xlsx")

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        for sheet_name, df in frames.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            _apply_sheet_style(writer.sheets[sheet_name])

    print(f"\n  -> {os.path.relpath(out_path)}")


if __name__ == "__main__":
    main()
