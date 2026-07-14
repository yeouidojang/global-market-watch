# -*- coding: utf-8 -*-
"""공통 로더/저장 유틸 — Stage 2 분석 스크립트 공용."""
import os
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
PARSED = os.path.join(BASE, "parsed")
CONFIG = os.path.join(BASE, "config")
OUT = os.path.join(BASE, "out")
os.makedirs(OUT, exist_ok=True)

_STR = {"base_date": str, "date": str, "run_date": str,
        "etf_code": str, "constituent_code": str,
        "listing_date": str, "delisting_date": str}


def load_meta():
    return pd.read_csv(os.path.join(PARSED, "etf_meta_ts.csv"), dtype=_STR)


def load_holdings():
    return pd.read_csv(os.path.join(PARSED, "holdings_ts.csv"), dtype=_STR)


def load_universe():
    return pd.read_csv(os.path.join(PARSED, "etf_universe.csv"), dtype=_STR)


def save(df, name, out_dir=None):
    d = out_dir or OUT
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    df.to_csv(p, index=False, encoding="utf-8-sig")
    print(f"  -> {os.path.basename(d)}/{name}  ({len(df):,} rows)")
    return p
