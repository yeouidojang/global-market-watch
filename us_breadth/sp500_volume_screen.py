"""
S&P 500 거래대금 급증 + 가격 모멘텀 + EPS 상향 스크리닝
─────────────────────────────────────────────────────────
필터 3단계:
  1) 거래대금  : 최근 5거래일 평균 > 직전 4주(20거래일) 평균  (vol_ratio > 1.0)
  2) 가격 흐름 : 1주 수익률 > 0  AND  현재가 > MA20
  (1주/1개월/YTD 수익률은 참고용 컬럼으로 함께 출력, 필터/점수는 1주 수익률만 사용)
  3) EPS 상향  : eps_chg_1m > 0  (최근 1개월 이익추정치 상향)

출력: us-market-analysis/output/sp500_screen_YYYYMMDD.xlsx
시트:
  필터링종목   — 조건 통과 종목 (종합점수 내림차순)
  전체후보     — 거래대금 급증 종목 전체 (EPS·가격 필터 적용 전)
  기준         — 분석 파라미터 및 요약 통계
"""

import io
import os
import sys
import sqlite3
import urllib.request
import warnings
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent   # global-market-watch/
load_dotenv(BASE_DIR / ".env")
OUT_DIR  = (BASE_DIR / (os.getenv("US_ANALYSIS_DIR") or "../us-market-analysis")).resolve() / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE_DIR))

DB_PATH  = BASE_DIR / "db" / "market_watch.db"
TODAY    = datetime.today().strftime("%Y-%m-%d")

# ── 파라미터 ─────────────────────────────────────────────────────────────────
RECENT_DAYS   = 5    # 최근 N 거래일 (1주)
PRIOR_DAYS    = 20   # 직전 M 거래일 (4주)
MIN_PRICE     = 5.0  # 최소 주가 ($) — 페니스톡 제외
MIN_DOLLARVOL = 5e6  # 최소 일평균 거래대금 ($) — 비유동 종목 제외
LOAD_DAYS     = RECENT_DAYS + PRIOR_DAYS + 5  # 여유분 포함 로드


# ── 1. 종목 메타데이터 ────────────────────────────────────────────────────────
def _wiki_html(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; MarketAnalysis/1.0)"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8")


def fetch_sp500_meta() -> dict[str, dict]:
    """DB(spx_constituents) 우선으로 종목 유니버스를 정하고, 이름/섹터는 Wikipedia로 보완.

    DB에 스냅샷이 없으면 Wikipedia 목록 자체를 유니버스로 사용 (폴백).
    """
    html = _wiki_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    df   = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})[0]
    wiki_meta = {}
    for _, row in df.iterrows():
        t = str(row["Symbol"]).replace(".", "-").strip()
        wiki_meta[t] = {
            "name":         str(row.get("Security", "")).strip(),
            "sector":       str(row.get("GICS Sector", "")).strip(),
            "sub_industry": str(row.get("GICS Sub-Industry", "")).strip(),
        }

    from db.db_manager import DBManager
    db_tickers = DBManager().get_latest_spx_constituents()

    if db_tickers:
        tickers = db_tickers
        print(f"[meta] DB spx_constituents → {len(tickers)}개 종목 (이름/섹터는 Wikipedia 보완)")
    else:
        tickers = list(wiki_meta.keys())
        print(f"[meta] Wikipedia S&P 500 → {len(tickers)}개 종목 (DB 없음, 폴백)")

    empty = {"name": "", "sector": "", "sub_industry": ""}
    meta = {t: wiki_meta.get(t, dict(empty)) for t in tickers}
    return meta


# ── 2. OHLCV 로드 ─────────────────────────────────────────────────────────────
def load_ohlcv(tickers: list[str], n_days: int = LOAD_DAYS, as_of: str | None = None) -> pd.DataFrame:
    placeholders = ",".join("?" * len(tickers))
    sql = f"""
        SELECT name AS ticker, date, close, volume
        FROM   market_daily
        WHERE  session  = 'us'
          AND  category = 'stock'
          AND  name     IN ({placeholders})
          AND  date    <= ?
        ORDER  BY name, date
    """
    # 기준일(as_of) 이후 데이터는 제외 — 과거 기준일 재실행 시 미래 데이터 혼입 방지
    with sqlite3.connect(str(DB_PATH)) as conn:
        df = pd.read_sql_query(sql, conn, params=tickers + [as_of or "9999-12-31"])

    if df.empty:
        return df

    df["date"] = pd.to_datetime(df["date"])
    all_dates  = sorted(df["date"].unique())
    cutoff     = all_dates[-n_days] if len(all_dates) >= n_days else all_dates[0]
    df = df[df["date"] >= cutoff].copy()
    print(f"[DB] {len(df['ticker'].unique())}개 티커 / {len(all_dates)}거래일 → 최근 {n_days}일 사용")
    return df


# ── 3. EPS 추정치 로드 ────────────────────────────────────────────────────────
def load_eps_cache(as_of: str | None = None) -> pd.DataFrame:
    """eps_cache는 종목당 여러 주(금요일)의 스냅샷을 보관 — 가장 최근 스냅샷만 사용."""
    with sqlite3.connect(str(DB_PATH)) as conn:
        df = pd.read_sql_query(
            "SELECT ticker, eps_chg_1w, eps_chg_1m, eps_chg_3m, fetched_date FROM eps_cache "
            "WHERE fetched_date = (SELECT MAX(fetched_date) FROM eps_cache WHERE fetched_date <= ?)",
            conn, params=[as_of or "9999-12-31"],
        )
    print(f"[EPS] eps_cache → {len(df)}개 종목 (최신: {df['fetched_date'].max()})")
    return df


# ── 4. 지표 계산 ──────────────────────────────────────────────────────────────
def calc_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV → 거래대금 급증 비율 + 가격 모멘텀 지표."""
    rows = []
    for ticker, grp in df.groupby("ticker"):
        grp = grp.sort_values("date").reset_index(drop=True)
        if len(grp) < RECENT_DAYS + PRIOR_DAYS:
            continue

        closes  = grp["close"].values
        vols    = grp["volume"].values
        dates   = grp["date"].values

        # 거래대금 ($) = 종가 × 거래량
        dollar_vol = closes * vols

        cur_price = float(closes[-1])
        if cur_price < MIN_PRICE:
            continue

        # 최근 5거래일 vs 직전 20거래일
        recent   = dollar_vol[-RECENT_DAYS:]
        prior    = dollar_vol[-(RECENT_DAYS + PRIOR_DAYS):-RECENT_DAYS]

        avg_recent = recent.mean()
        avg_prior  = prior.mean()

        if avg_prior < 1:   # 거래대금 0인 케이스 방지
            continue
        if avg_recent < MIN_DOLLARVOL:
            continue

        vol_ratio = avg_recent / avg_prior

        # 가격 모멘텀
        ret_1d  = (closes[-1] / closes[-2]  - 1) * 100 if len(closes) >= 2  else None
        ret_5d  = (closes[-1] / closes[-6]  - 1) * 100 if len(closes) >= 6  else None
        ret_20d = (closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 else None
        ma20    = closes[-20:].mean() if len(closes) >= 20 else None
        ma20_dev = (cur_price / ma20 - 1) * 100 if ma20 else None

        rows.append({
            "ticker":           ticker,
            "기준일":            pd.Timestamp(dates[-1]).strftime("%Y-%m-%d"),
            "현재가($)":         round(cur_price, 2),
            "최근5일_평균거래대금($M)": round(avg_recent / 1e6, 2),
            "직전4주_평균거래대금($M)": round(avg_prior  / 1e6, 2),
            "거래대금_비율":      round(vol_ratio, 2),
            "1일_수익률(%)":      round(ret_1d,   2) if ret_1d   is not None else None,
            "1주_수익률(%)":      round(ret_5d,   2) if ret_5d   is not None else None,
            "1개월_수익률(%)":    round(ret_20d,  2) if ret_20d  is not None else None,
            "MA20_이격도(%)":     round(ma20_dev, 2) if ma20_dev is not None else None,
        })

    return pd.DataFrame(rows)


# ── 4-1. YTD 수익률 (경량 로드) ─────────────────────────────────────────────────
def load_ytd_returns(tickers: list[str], cur_price: dict[str, float], date_str: str) -> dict[str, float]:
    """해당 연도 1/1 이후 종가만 로드해 YTD 수익률(%) 계산. (메인 OHLCV 윈도우와 별도)"""
    year_start = f"{pd.Timestamp(date_str).year}-01-01"
    placeholders = ",".join("?" * len(tickers))
    sql = f"""
        SELECT name AS ticker, date, close
        FROM   market_daily
        WHERE  session  = 'us'
          AND  category = 'stock'
          AND  name     IN ({placeholders})
          AND  date     >= ?
        ORDER  BY name, date
    """
    with sqlite3.connect(str(DB_PATH)) as conn:
        df = pd.read_sql_query(sql, conn, params=tickers + [year_start])
    if df.empty:
        return {}

    ytd = {}
    for ticker, grp in df.groupby("ticker"):
        base = grp.sort_values("date")["close"].iloc[0]
        cur  = cur_price.get(ticker)
        if base and cur is not None:
            ytd[ticker] = round((cur - base) / base * 100, 2)
    return ytd


# ── 5. 필터 + 스코어링 ────────────────────────────────────────────────────────
def apply_filters(metrics: pd.DataFrame, eps: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns
    -------
    screened  : 3단계 필터 모두 통과 (종합점수 내림차순)
    candidates: 거래대금 급증 종목 전체 (EPS·가격 필터 전)
    """
    # 거래대금 급증 (1단계)
    candidates = metrics[metrics["거래대금_비율"] > 1.0].copy()

    # EPS 조인
    candidates = candidates.merge(
        eps[["ticker", "eps_chg_1w", "eps_chg_1m", "eps_chg_3m", "fetched_date"]],
        on="ticker", how="left",
    )
    candidates.rename(columns={
        "eps_chg_1w":   "EPS_1주변화율(%)",
        "eps_chg_1m":   "EPS_1달변화율(%)",
        "eps_chg_3m":   "EPS_3달변화율(%)",
        "fetched_date": "EPS수집일",
    }, inplace=True)

    # 2단계: 가격 모멘텀
    price_ok = (
        (candidates["1주_수익률(%)"]  > 0) &
        (candidates["MA20_이격도(%)"] > 0)
    )

    # 3단계: EPS 상향
    eps_ok = candidates["EPS_1달변화율(%)"] > 0

    screened = candidates[price_ok & eps_ok].copy()

    # 종합 점수 (각 지표 백분위 합산)
    def pct_rank(col):
        return screened[col].rank(pct=True, na_option="bottom")

    if not screened.empty:
        screened["종합점수"] = (
            pct_rank("거래대금_비율")   * 0.35 +
            pct_rank("1주_수익률(%)")    * 0.25 +
            pct_rank("MA20_이격도(%)") * 0.15 +
            pct_rank("EPS_1달변화율(%)") * 0.25
        ).round(3)
        screened = screened.sort_values("종합점수", ascending=False).reset_index(drop=True)

    candidates = candidates.sort_values("거래대금_비율", ascending=False).reset_index(drop=True)
    print(f"[필터] 거래대금 급증: {len(candidates)}개 → 가격+EPS 필터 후: {len(screened)}개")
    return screened, candidates


# ── 6. 메타데이터 부착 ───────────────────────────────────────────────────────
def attach_meta(df: pd.DataFrame, meta: dict[str, dict]) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df.insert(1, "종목명",   df["ticker"].map(lambda t: meta.get(t, {}).get("name", "")))
    df.insert(2, "섹터",     df["ticker"].map(lambda t: meta.get(t, {}).get("sector", "")))
    df.insert(3, "세부업종", df["ticker"].map(lambda t: meta.get(t, {}).get("sub_industry", "")))
    return df


# ── 7. Excel 저장 ─────────────────────────────────────────────────────────────
def _col_width(ws, df: pd.DataFrame):
    for i, col in enumerate(df.columns, 1):
        try:
            w = max(df[col].astype(str).map(len).max(), len(str(col))) + 2
        except Exception:
            w = len(str(col)) + 2
        ws.column_dimensions[ws.cell(1, i).column_letter].width = min(w, 32)


def save_excel(
    screened:   pd.DataFrame,
    candidates: pd.DataFrame,
    date_str:   str,
) -> Path:
    fname = f"sp500_screen_{date_str.replace('-', '')}.xlsx"
    path  = OUT_DIR / fname

    # 기준 시트
    params_df = pd.DataFrame([
        {"항목": "분석 기준일",         "값": date_str},
        {"항목": "거래대금 기간(최근)",  "값": f"{RECENT_DAYS}거래일"},
        {"항목": "거래대금 기간(직전)",  "값": f"{PRIOR_DAYS}거래일 (4주)"},
        {"항목": "최소 주가",            "값": f"${MIN_PRICE:.0f}"},
        {"항목": "최소 일평균거래대금",  "값": f"${MIN_DOLLARVOL/1e6:.0f}M"},
        {"항목": "거래대금 비율 조건",   "값": "> 1.0 (최근>직전)"},
        {"항목": "가격 조건",            "값": "1주수익률>0 AND MA20_이격도>0"},
        {"항목": "EPS 조건",             "값": "1달 EPS 변화율 > 0"},
        {"항목": "거래대금 급증 후보",   "값": len(candidates)},
        {"항목": "최종 통과 종목",       "값": len(screened)},
    ])

    with pd.ExcelWriter(str(path), engine="openpyxl") as writer:
        screened.to_excel(  writer, sheet_name="필터링종목", index=False)
        candidates.to_excel(writer, sheet_name="전체후보",   index=False)
        params_df.to_excel( writer, sheet_name="기준",       index=False)
        for sheet, df in [("필터링종목", screened), ("전체후보", candidates), ("기준", params_df)]:
            _col_width(writer.sheets[sheet], df)

    print(f"[저장] {path}")
    return path


# ── MAIN ──────────────────────────────────────────────────────────────────────
def run(date_str: str = TODAY) -> tuple[pd.DataFrame, Path]:
    global TODAY
    TODAY = date_str
    print(f"\n{'='*60}")
    print(f"  S&P 500 거래대금 급증 + 모멘텀 + EPS 상향 스크리닝")
    print(f"  기준일: {date_str}")
    print(f"{'='*60}\n")

    print("▶ 종목 메타데이터 로드 (Wikipedia)")
    meta = fetch_sp500_meta()

    print("\n▶ OHLCV 로드 (DB)")
    tickers = list(meta.keys())
    ohlcv   = load_ohlcv(tickers, as_of=date_str)
    if ohlcv.empty:
        raise RuntimeError("DB에 US stock 데이터 없음")

    print("\n▶ EPS 추정치 로드 (eps_cache)")
    eps = load_eps_cache(as_of=date_str)

    print("\n▶ 지표 계산")
    metrics = calc_metrics(ohlcv)
    print(f"  유효 종목: {len(metrics)}개")

    print("\n▶ YTD 수익률 계산")
    cur_price_map = dict(zip(metrics["ticker"], metrics["현재가($)"]))
    ytd_map = load_ytd_returns(list(cur_price_map.keys()), cur_price_map, date_str)
    metrics["YTD_수익률(%)"] = metrics["ticker"].map(ytd_map)
    print(f"  YTD 계산 완료: {len(ytd_map)}개")

    print("\n▶ 필터 적용")
    screened, candidates = apply_filters(metrics, eps)

    print("\n▶ 메타데이터 부착")
    screened   = attach_meta(screened,   meta)
    candidates = attach_meta(candidates, meta)

    print("\n▶ Excel 저장")
    path = save_excel(screened, candidates, date_str)

    # 콘솔 요약
    print(f"\n{'='*60}")
    print(f"■ 최종 통과 종목 (상위 20개)")
    if screened.empty:
        print("  — 해당 없음")
    else:
        cols = ["ticker", "종목명", "섹터", "거래대금_비율",
                "1주_수익률(%)", "1개월_수익률(%)", "YTD_수익률(%)",
                "MA20_이격도(%)", "EPS_1달변화율(%)", "종합점수"]
        avail = [c for c in cols if c in screened.columns]
        print(screened[avail].head(20).to_string(index=False))

    return screened, path
