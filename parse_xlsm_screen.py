# -*- coding: utf-8 -*-
"""
xlsm 'Screen' 시트 → etf_meta_ts.csv 포맷 DataFrame.
num_cu  = shares_out / cu_size  (derived, Screen 시트에는 num_cu 컬럼 없음)
cu_value = nav * cu_size
"""
import openpyxl
import pandas as pd

ITEM_MAP = {
    "W200300": "listing_date",
    "R102300": "delisting_date",
    "W202100": "etf_type",
    "W200100": "base_index",
    "S102100": "mktcap",
    "S100100": "close",
    "S100500": "ret",
    "W201100": "nav",
    "W201200": "disparity",
    "S101500": "shares_out",
    "W402600": "cu_size",
}
TS_ITEMS    = {"mktcap", "close", "ret", "nav", "disparity", "shares_out"}
SCALAR_ITEMS = {"etf_type", "base_index", "cu_size", "listing_date", "delisting_date"}
ETF_CODE_LEN = 7


def _str(v):
    """None/int/float/str → clean str (int 변환 시 .0 제거)."""
    if v is None:
        return ""
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v).strip()


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_screen(xlsm_path, base_date="20260709"):
    """
    Parameters
    ----------
    xlsm_path : str  — xlsm 파일 경로
    base_date : str  — 출력 DataFrame 에 부여할 base_date (기준 목요일)

    Returns
    -------
    pd.DataFrame  — etf_meta_ts.csv 와 동일한 컬럼 구성, 6일치
    """
    wb = openpyxl.load_workbook(xlsm_path, read_only=True,
                                keep_vba=False, data_only=True)
    ws = wb["Screen"]
    all_rows = list(ws.iter_rows(values_only=True))
    wb.close()

    # Account 행 탐색 (row[1] == 'Account')
    acc_idx = next(
        (i for i, row in enumerate(all_rows)
         if len(row) > 1 and row[1] == "Account"),
        None
    )
    if acc_idx is None:
        raise ValueError("Screen 시트에서 Account 행을 찾지 못했습니다.")

    acc    = all_rows[acc_idx]
    period = all_rows[acc_idx + 1]

    # 컬럼 매핑 빌드
    col_item, col_date = {}, {}
    for c in range(2, len(acc)):
        a = _str(acc[c]) if c < len(acc) else ""
        p = period[c]   if c < len(period) else None
        if a not in ITEM_MAP:
            continue
        it = ITEM_MAP[a]
        col_item[c] = it
        if it in TS_ITEMS and p is not None:
            d = _str(p)
            if len(d) == 8 and d.isdigit():
                col_date[c] = d

    dates = sorted(set(col_date.values()))

    # 데이터 행 시작 탐색 (ETF 코드가 'A'+6자리)
    data_start = next(
        (i for i in range(acc_idx + 1, len(all_rows))
         if all_rows[i] and _str(all_rows[i][0]).startswith("A")
         and len(_str(all_rows[i][0])) == ETF_CODE_LEN),
        None
    )
    if data_start is None:
        raise ValueError("Screen 시트에서 데이터 시작 행을 찾지 못했습니다.")

    records = []
    for row in all_rows[data_start:]:
        if not row or row[0] is None:
            continue
        code = _str(row[0])
        if not (code.startswith("A") and len(code) == ETF_CODE_LEN):
            continue
        name = _str(row[1]) if len(row) > 1 else ""

        scal = {}
        ts   = {d: {} for d in dates}
        for c, it in col_item.items():
            val = row[c] if c < len(row) else None
            if it in SCALAR_ITEMS:
                scal[it] = (_str(val)
                            if it in ("etf_type", "base_index", "listing_date", "delisting_date")
                            else _num(val))
            else:
                d = col_date.get(c)
                if d is not None:
                    ts[d][it] = _num(val)

        cu = scal.get("cu_size")
        for d in dates:
            shares = ts[d].get("shares_out")
            nav    = ts[d].get("nav")
            ts[d]["num_cu"]   = (shares / cu) if (shares and cu) else None
            ts[d]["cu_value"] = (nav * cu)     if (nav and cu)    else None

        for d in dates:
            rec = {
                "base_date":      base_date,
                "date":           d,
                "etf_code":       code,
                "etf_name":       name,
                "etf_type":       (scal.get("etf_type") or "").strip(),
                "base_index":     scal.get("base_index", ""),
                "cu_size":        cu,
                "listing_date":   scal.get("listing_date", ""),
                "delisting_date": scal.get("delisting_date", ""),
            }
            rec.update({k: ts[d].get(k) for k in
                        ("mktcap", "close", "ret", "nav", "disparity",
                         "shares_out", "num_cu", "cu_value")})
            records.append(rec)

    return pd.DataFrame(records)
