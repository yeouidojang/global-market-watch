"""
KOSPI / KOSDAQ 시장 폭 분석
────────────────────────────────────────────
Universe:
  · KOSPI200  — DB 내 KOSPI 종목을 LSEG 시총 기준 상위 200개로 선정
  · KOSDAQ150 — DB 내 KOSDAQ 종목을 LSEG 시총 기준 상위 150개로 선정

GICS 섹터: LSEG TR.GICSSector / TR.GICSSubIndustry

지표 (US Breadth와 동일):
  · 고가대비(%)       : 250거래일 최고가 대비 현재가
  · 저가대비(%)       : 250거래일 최저가 대비 현재가
  · MA20/60/120/250 이격도(%) : (현재가 − MA) / MA × 100

시트:
  KOSPI200       — 종목별 지표 (섹터·고가대비 오름차순)
  KOSPI200_섹터  — GICS 섹터별 집계
  KOSDAQ150      — 종목별 지표
  KOSDAQ150_섹터 — GICS 섹터별 집계

출력: /home/quant/kr-market-analysis/output/kr_breadth_YYYYMMDD.xlsx
"""

import ssl
import httpx
import urllib3

# LSEG SDK가 httpx를 사용해 Refinitiv API에 연결할 때 프록시/SSL 설정 패치
# collect_macro.py 와 동일한 패치 — 모듈 reload 시 이중 적용 방지를 위해 sentinel 사용
if not getattr(httpx.Client.__init__, "_gmw_patched", False):
    _orig_httpx_client = httpx.Client.__init__
    _orig_httpx_async  = httpx.AsyncClient.__init__

    def _fix_proxy(kw):
        kw.setdefault("verify", False)
        if "proxies" in kw:
            proxies = kw.pop("proxies")
            if isinstance(proxies, dict):
                url = next((v for v in proxies.values() if v), None)
                if url:
                    kw.setdefault("proxy", httpx.Proxy(url))
            elif proxies is not None:
                kw.setdefault("proxy", proxies)
        if "proxy" in kw and isinstance(kw["proxy"], dict):
            url = next((v for v in kw["proxy"].values() if v), None)
            kw["proxy"] = httpx.Proxy(url) if url else None

    def _patched_client(self, *a, **kw):
        _fix_proxy(kw)
        _orig_httpx_client(self, *a, **kw)

    def _patched_async(self, *a, **kw):
        _fix_proxy(kw)
        _orig_httpx_async(self, *a, **kw)

    _patched_client._gmw_patched = True
    _patched_async._gmw_patched  = True
    httpx.Client.__init__      = _patched_client
    httpx.AsyncClient.__init__ = _patched_async

urllib3.disable_warnings()

import sys
import os
import sqlite3
from pathlib import Path
from datetime import datetime

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent   # global-market-watch/
from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")
OUT_DIR  = (BASE_DIR / (os.getenv("KR_ANALYSIS_DIR") or "../kr-market-analysis")).resolve() / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE_DIR))

DB_PATH = BASE_DIR / "db" / "market_watch.db"
TODAY   = datetime.today().strftime("%Y-%m-%d")
N_DAYS  = 250
LSEG_BATCH      = 50    # LSEG get_data 배치 크기
LSEG_CAL_DAYS   = 365   # OHLCV 조회 캘린더 일수 (≈ 250 거래일)
KOSPI200_N      = 200
KOSDAQ150_N     = 150


# ── LSEG retry wrapper ────────────────────────────────────────────────────────

def _lseg_get(ld, universe, fields, parameters=None, label="", retries=3):
    import time
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return ld.get_data(universe=universe, fields=fields, parameters=parameters)
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                wait = 2 ** (attempt - 1)
                if label:
                    print(f"  [{label} attempt={attempt}] {exc} → retry in {wait}s")
                time.sleep(wait)
    raise last_exc


# ── 1. DB에서 시장별 티커 목록 ───────────────────────────────────────────────

def _get_db_tickers(market: str) -> list[str]:
    """session='asia', category='stock' 기준 KOSPI 또는 KOSDAQ 전체 종목 코드."""
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT DISTINCT name FROM market_daily "
        "WHERE session='asia' AND category='stock' AND market=?",
        (market,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


# ── 2. LSEG: 시총 + GICS 섹터 배치 조회 ─────────────────────────────────────

def _fetch_mktcap_gics(rics: list[str], ld) -> pd.DataFrame:
    """RIC 리스트 → DataFrame(ric, name, mktcap, sector, sub_industry)."""
    parts = []
    for i in range(0, len(rics), LSEG_BATCH):
        batch = rics[i: i + LSEG_BATCH]
        try:
            df = _lseg_get(
                ld, batch,
                ["TR.RIC", "TR.CompanyName", "TR.CompanyMarketCap",
                 "TR.GICSSector", "TR.GICSSubIndustry"],
                label=f"mktcap+GICS batch {i // LSEG_BATCH + 1}",
            )
            if df is not None and not df.empty:
                parts.append(df)
        except Exception as e:
            print(f"  [LSEG mktcap+GICS FAIL batch {i // LSEG_BATCH + 1}] {e}")

    if not parts:
        return pd.DataFrame()

    out = pd.concat(parts, ignore_index=True)
    out = out.rename(columns={
        "Instrument":             "ric",
        "RIC":                    "_ric2",
        "Company Name":           "name",
        "Company Market Cap":     "mktcap",
        "GICS Sector Name":       "sector",
        "GICS Sub-Industry Name": "sub_industry",
    })
    out["mktcap"] = pd.to_numeric(out["mktcap"], errors="coerce")
    return out[["ric", "name", "mktcap", "sector", "sub_industry"]]


# ── 3. 시총 상위 N 선정 ──────────────────────────────────────────────────────

def _select_top_n(meta_df: pd.DataFrame, n: int, market: str) -> pd.DataFrame:
    """mktcap 상위 n개 선정 + ticker(6자리) 컬럼 추가."""
    suffix  = ".KS" if market == "KOSPI" else ".KQ"
    valid   = meta_df.dropna(subset=["mktcap"]).copy()
    top     = valid.nlargest(n, "mktcap").reset_index(drop=True)
    top["market"] = market
    top["ticker"] = top["ric"].str.replace(suffix, "", regex=False)
    print(f"  [{market} 상위{n}] 선정 완료 "
          f"(시총 유효: {len(valid)}개 / 전체 {len(meta_df)}개)")
    return top


# ── 4. 섹터·종목명 맵 ────────────────────────────────────────────────────────

def _build_maps(top_df: pd.DataFrame) -> tuple[dict, dict]:
    """ric → (sector, sub_industry), ric → name."""
    sector_map, name_map = {}, {}
    for _, row in top_df.iterrows():
        ric = str(row["ric"])
        sector_map[ric] = (
            str(row["sector"])      if pd.notna(row.get("sector"))      else "",
            str(row["sub_industry"]) if pd.notna(row.get("sub_industry")) else "",
        )
        nm = row.get("name")
        if pd.notna(nm) and nm:
            name_map[ric] = str(nm)
    return sector_map, name_map


# ── 5. DB OHLCV 로드 ─────────────────────────────────────────────────────────

def _load_db_ohlcv(tickers: list[str]) -> pd.DataFrame:
    """session='asia', category='stock' 에서 OHLCV 로드 (ticker = KRX 6자리)."""
    if not tickers:
        return pd.DataFrame()
    placeholders = ",".join("?" * len(tickers))
    conn = sqlite3.connect(str(DB_PATH))
    df   = pd.read_sql_query(
        f"SELECT name AS ticker, date, close, high, low "
        f"FROM market_daily "
        f"WHERE session='asia' AND category='stock' AND name IN ({placeholders}) "
        f"ORDER BY name, date",
        conn, params=tickers,
    )
    conn.close()
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


# ── 6. LSEG OHLCV 히스토리 조회 ─────────────────────────────────────────────

def _fetch_lseg_ohlcv(rics: list[str], ld) -> pd.DataFrame:
    """LSEG에서 LSEG_CAL_DAYS 기간 OHLCV → long-format DataFrame(ric, date, open/high/low/close/volume)."""
    parts = []
    params = {"SDate": f"-{LSEG_CAL_DAYS}D", "EDate": "0D", "Frq": "D"}
    for i in range(0, len(rics), LSEG_BATCH):
        batch = rics[i: i + LSEG_BATCH]
        try:
            df = _lseg_get(
                ld, batch,
                ["TR.PriceOpen", "TR.PriceHigh", "TR.PriceLow",
                 "TR.PriceClose", "TR.Volume", "TR.PriceClose.date"],
                parameters=params,
                label=f"OHLCV batch {i // LSEG_BATCH + 1}",
            )
            if df is not None and not df.empty:
                parts.append(df)
        except Exception as e:
            print(f"  [LSEG OHLCV FAIL batch {i // LSEG_BATCH + 1}] {e}")

    if not parts:
        return pd.DataFrame()

    raw = pd.concat(parts, ignore_index=True)
    raw = raw.rename(columns={
        "Instrument":  "ric",
        "Price Open":  "open",
        "Price High":  "high",
        "Price Low":   "low",
        "Price Close": "close",
        "Volume":      "volume",
        "Date":        "date",
    })
    for col in ("open", "high", "low", "close", "volume"):
        if col in raw.columns:
            raw[col] = pd.to_numeric(raw[col], errors="coerce")
    raw = raw.dropna(subset=["close", "date"])
    raw = raw[raw["close"] > 0].copy()
    raw["date"] = pd.to_datetime(raw["date"])
    return raw


# ── 7. LSEG OHLCV → DB 저장 (upsert) ────────────────────────────────────────

def _save_lseg_ohlcv_to_db(lseg_df: pd.DataFrame, market: str):
    """LSEG long-format OHLCV → market_daily DB 저장."""
    if lseg_df.empty:
        return
    from db.db_manager import DBManager

    suffix  = ".KS" if market == "KOSPI" else ".KQ"
    records = []
    for _, row in lseg_df.iterrows():
        ric    = str(row["ric"])
        ticker = ric.replace(suffix, "")
        close  = row.get("close")
        if pd.isna(close) or float(close) <= 0:
            continue
        records.append({
            "date":     str(row["date"])[:10],
            "session":  "asia",
            "category": "stock",
            "name":     ticker,
            "market":   market,
            "close":    float(close),
            "open":     float(row["open"])   if pd.notna(row.get("open"))   else None,
            "high":     float(row["high"])   if pd.notna(row.get("high"))   else None,
            "low":      float(row["low"])    if pd.notna(row.get("low"))    else None,
            "volume":   float(row["volume"]) if pd.notna(row.get("volume")) else None,
        })
    if records:
        n = DBManager().upsert_market_daily(records)
        dates = {r["date"] for r in records}
        print(f"  [DB upsert] {n}건 ({len({r['name'] for r in records})}개 종목 × "
              f"{len(dates)}일, {min(dates)} ~ {max(dates)})")


# ── 8. 지표 계산 ─────────────────────────────────────────────────────────────

def calc_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """250거래일 고/저가 대비 현재가 + MA 이격도 계산. US Breadth와 동일."""
    if df.empty:
        return pd.DataFrame()

    rows = []
    for ticker, grp in df.groupby("ticker"):
        grp    = grp.sort_values("date")
        closes = grp["close"].values
        highs  = grp["high"].values
        lows   = grp["low"].values

        if len(closes) < 5:
            continue

        cur    = closes[-1]
        cur_dt = grp["date"].iloc[-1].strftime("%Y-%m-%d")
        h250   = highs.max()
        l250   = lows.min()

        def ma_dev(n):
            if len(closes) < n:
                return None
            ma = closes[-n:].mean()
            return round((cur - ma) / ma * 100, 2) if ma else None

        def ret_n(n):
            if len(closes) <= n:
                return None
            prev = closes[-n - 1]
            return round((cur - prev) / prev * 100, 2) if prev else None

        year_start = pd.Timestamp(year=grp["date"].iloc[-1].year, month=1, day=1)
        ytd_rows   = grp.loc[grp["date"] >= year_start, "close"]
        ytd_base   = ytd_rows.iloc[0] if not ytd_rows.empty else None
        ytd_ret    = round((cur - ytd_base) / ytd_base * 100, 2) if ytd_base else None

        rows.append({
            "ticker":          ticker,
            "기준일":           cur_dt,
            "현재가":           round(cur, 0),
            "250일_고가":       round(h250, 0),
            "250일_저가":       round(l250, 0),
            "고가대비(%)":      round((cur - h250) / h250 * 100, 2),
            "저가대비(%)":      round((cur - l250) / l250 * 100, 2),
            "1일_수익률(%)":    ret_n(1),
            "1주_수익률(%)":    ret_n(5),
            "1개월_수익률(%)":  ret_n(21),
            "YTD_수익률(%)":    ytd_ret,
            "MA20_이격도(%)":   ma_dev(20),
            "MA60_이격도(%)":   ma_dev(60),
            "MA120_이격도(%)":  ma_dev(120),
            "MA250_이격도(%)":  ma_dev(250),
            "데이터_거래일수":   len(closes),
        })

    return pd.DataFrame(rows)


# ── 9. 메타데이터 부착 ───────────────────────────────────────────────────────

def attach_meta(
    result:     pd.DataFrame,
    name_map:   dict[str, str],
    sector_map: dict[str, tuple[str, str]],
    suffix:     str,    # ".KS" or ".KQ"
) -> pd.DataFrame:
    result = result.copy()

    def _ric(t):
        return t + suffix

    result.insert(1, "종목명",   result["ticker"].map(lambda t: name_map.get(_ric(t), "")))
    result.insert(2, "섹터",     result["ticker"].map(lambda t: sector_map.get(_ric(t), ("", ""))[0]))
    result.insert(3, "세부업종", result["ticker"].map(lambda t: sector_map.get(_ric(t), ("", ""))[1]))
    result.insert(4, "시장",     suffix.lstrip("."))
    return result


# ── 10. 섹터 집계 ────────────────────────────────────────────────────────────

def build_sector_summary(df: pd.DataFrame) -> pd.DataFrame:
    metric_cols = ["고가대비(%)", "저가대비(%)", "1주_수익률(%)", "1개월_수익률(%)", "YTD_수익률(%)",
                   "MA20_이격도(%)", "MA60_이격도(%)", "MA120_이격도(%)", "MA250_이격도(%)"]
    rows = []
    for sector, grp in df.groupby("섹터"):
        row = {"섹터": sector, "종목수": len(grp)}
        for col in metric_cols:
            vals = grp[col].dropna()
            row[f"{col}_평균"]   = round(vals.mean(), 2)   if not vals.empty else None
            row[f"{col}_중앙값"] = round(vals.median(), 2) if not vals.empty else None
        h = grp["고가대비(%)"].dropna()
        row["신고가권(-5%이내)_수"] = int((h >= -5).sum())
        row["하락권(-20%이하)_수"]  = int((h <= -20).sum())
        row["신고가권_비율(%)"]      = round((h >= -5).mean() * 100, 1)
        row["하락권_비율(%)"]        = round((h <= -20).mean() * 100, 1)
        rows.append(row)

    summary = pd.DataFrame(rows).sort_values("섹터")

    total = {"섹터": "【전체】", "종목수": len(df)}
    for col in metric_cols:
        vals = df[col].dropna()
        total[f"{col}_평균"]   = round(vals.mean(), 2)   if not vals.empty else None
        total[f"{col}_중앙값"] = round(vals.median(), 2) if not vals.empty else None
    h_all = df["고가대비(%)"].dropna()
    total["신고가권(-5%이내)_수"] = int((h_all >= -5).sum())
    total["하락권(-20%이하)_수"]  = int((h_all <= -20).sum())
    total["신고가권_비율(%)"]      = round((h_all >= -5).mean() * 100, 1)
    total["하락권_비율(%)"]        = round((h_all <= -20).mean() * 100, 1)

    return pd.concat([summary, pd.DataFrame([total])], ignore_index=True)


# ── 11. Excel 저장 ──────────────────────────────────────────────────────────

def _auto_col_width(ws, df: pd.DataFrame):
    for col_idx, col in enumerate(df.columns, 1):
        try:
            max_len = max(df[col].astype(str).map(len).max(), len(str(col))) + 2
        except Exception:
            max_len = len(str(col)) + 2
        ws.column_dimensions[ws.cell(1, col_idx).column_letter].width = min(max_len, 35)


def save_excel(sheets: dict[str, pd.DataFrame], fname: str) -> Path:
    path = OUT_DIR / fname
    with pd.ExcelWriter(str(path), engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            _auto_col_width(writer.sheets[sheet_name], df)
    print(f"[저장] {path}")
    return path


# ── 12. 단일 시장 파이프라인 ─────────────────────────────────────────────────

def _analyze_market(market: str, n_top: int, ld) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    KOSPI 또는 KOSDAQ에 대해:
      1) DB 전체 종목 → LSEG 시총 기준 상위 n_top 선정
      2) GICS 섹터 부착
      3) 250일 OHLCV 확보 (DB 우선 → 부족 시 LSEG 보완 후 DB 저장)
      4) 지표 계산 → 섹터 집계
    """
    suffix     = ".KS" if market == "KOSPI" else ".KQ"
    label      = "KOSPI200" if market == "KOSPI" else "KOSDAQ150"

    # 1. DB 전체 티커
    db_tickers = _get_db_tickers(market)
    db_rics    = [f"{t}{suffix}" for t in db_tickers]
    print(f"\n  [{label}] DB 종목 수: {len(db_tickers)}")

    # 2. LSEG 시총 + GICS → 상위 N 선정
    print(f"  [{label}] LSEG 시총+GICS 조회 ({len(db_rics)}개 배치 처리 중)...")
    meta_df = _fetch_mktcap_gics(db_rics, ld)
    if meta_df.empty:
        print(f"  [{label}] LSEG 데이터 없음 — 중단")
        return pd.DataFrame(), pd.DataFrame()

    top_df      = _select_top_n(meta_df, n_top, market)
    top_rics    = top_df["ric"].tolist()
    top_tickers = top_df["ticker"].tolist()
    sector_map, name_map = _build_maps(top_df)

    # 3. DB OHLCV 로드 (선정된 상위 종목만)
    db_ohlcv = _load_db_ohlcv(top_tickers)
    if not db_ohlcv.empty:
        db_days_per_ticker = db_ohlcv.groupby("ticker")["date"].count()
    else:
        db_days_per_ticker = pd.Series(dtype=int)

    # 절반 미만 데이터인 종목은 LSEG에서 보완
    min_db_days = N_DAYS // 2   # 125일
    need_lseg   = [
        ric for ric, ticker in zip(top_rics, top_tickers)
        if db_days_per_ticker.get(ticker, 0) < min_db_days
    ]

    # 4. LSEG OHLCV 보완
    if need_lseg:
        print(f"  [{label}] LSEG OHLCV 보완: {len(need_lseg)}개 종목 ({LSEG_CAL_DAYS}일)...")
        lseg_df = _fetch_lseg_ohlcv(need_lseg, ld)
        if not lseg_df.empty:
            lseg_df["ticker"] = lseg_df["ric"].str.replace(suffix, "", regex=False)
            _save_lseg_ohlcv_to_db(lseg_df, market)

            # DB 데이터와 합산 (LSEG 보완 종목 + DB 전용 종목)
            lseg_tickers = set(lseg_df["ticker"])
            db_only      = db_ohlcv[~db_ohlcv["ticker"].isin(lseg_tickers)]
            ohlcv_df     = pd.concat(
                [lseg_df[["ticker", "date", "close", "high", "low"]], db_only],
                ignore_index=True,
            )
        else:
            ohlcv_df = db_ohlcv
    else:
        print(f"  [{label}] DB 데이터 충분 — LSEG 조회 생략")
        ohlcv_df = db_ohlcv

    if ohlcv_df.empty:
        print(f"  [{label}] OHLCV 없음 — 중단")
        return pd.DataFrame(), pd.DataFrame()

    # 5. N_DAYS 윈도우 적용
    ohlcv_df["date"] = pd.to_datetime(ohlcv_df["date"])
    all_dates = sorted(ohlcv_df["date"].unique())
    if len(all_dates) >= N_DAYS:
        ohlcv_df = ohlcv_df[ohlcv_df["date"] >= all_dates[-N_DAYS]]

    # 6. 지표 계산
    result = calc_metrics(ohlcv_df)
    result = attach_meta(result, name_map, sector_map, suffix)
    result = result.sort_values(["섹터", "고가대비(%)"])
    print(f"  [{label}] 지표 계산: {len(result)}개 종목")

    # 7. 섹터 집계
    sector_summary = build_sector_summary(result)
    n_sectors = len(sector_summary) - 1  # 전체 합계 행 제외
    print(f"  [{label}] GICS 섹터: {n_sectors}개")

    return result, sector_summary


# ── MAIN ────────────────────────────────────────────────────────────────────

def run():
    import os
    import lseg.data as ld_mod

    print(f"[kr_breadth] 기준일: {TODAY}  분석 윈도우: {N_DAYS} 거래일")
    print(f"[kr_breadth] KOSPI200 (시총 상위 {KOSPI200_N}) + KOSDAQ150 (시총 상위 {KOSDAQ150_N})\n")

    # LSEG_CONFIG_PATH가 세션 이름(예: "platform.ldp")으로 설정된 경우 사용, 없으면 기본 세션
    cfg_name = os.getenv("LSEG_CONFIG_PATH")
    if cfg_name and "." in cfg_name and not cfg_name.endswith(".json"):
        ld_mod.open_session(config_name=cfg_name)
    else:
        ld_mod.open_session()
    try:
        print("▶ KOSPI200 분석")
        kp_result, kp_sector = _analyze_market("KOSPI", KOSPI200_N, ld_mod)

        print("\n▶ KOSDAQ150 분석")
        kq_result, kq_sector = _analyze_market("KOSDAQ", KOSDAQ150_N, ld_mod)
    finally:
        ld_mod.close_session()

    # Excel 저장
    fname  = f"kr_breadth_{TODAY.replace('-', '')}.xlsx"
    sheets = {}
    if not kp_result.empty:
        sheets["KOSPI200"]       = kp_result
        sheets["KOSPI200_섹터"]  = kp_sector
    if not kq_result.empty:
        sheets["KOSDAQ150"]      = kq_result
        sheets["KOSDAQ150_섹터"] = kq_sector

    if not sheets:
        raise RuntimeError("KOSPI200 / KOSDAQ150 데이터 모두 없음")

    fpath = save_excel(sheets, fname)

    # 콘솔 요약
    summary_cols = ["섹터", "종목수", "고가대비(%)_평균", "MA20_이격도(%)_평균",
                    "신고가권(-5%이내)_수", "하락권(-20%이하)_수"]
    if not kp_sector.empty:
        print("\n" + "=" * 70)
        print("■ KOSPI200 GICS 섹터별 요약")
        print(kp_sector[summary_cols].to_string(index=False))
    if not kq_sector.empty:
        print("\n■ KOSDAQ150 GICS 섹터별 요약")
        print(kq_sector[summary_cols].to_string(index=False))

    return kp_result, kq_result, kp_sector, kq_sector


if __name__ == "__main__":
    run()
