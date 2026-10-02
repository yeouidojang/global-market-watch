"""
미국 시장 폭 분석 — S&P 500 / NASDAQ-100 구성종목
────────────────────────────────────────────────
지표:
  · 고가대비(%)       : 250거래일 최고가 대비 현재가 (%)  — 음수일수록 많이 빠진 것
  · 저가대비(%)       : 250거래일 최저가 대비 현재가 (%)  — 양수일수록 바닥에서 오른 것
  · MA20/60/120/250 이격도(%) : (현재가 − MA) / MA × 100

시트 구성:
  SP500        — 503개 종목 (섹터 포함, 고가대비 오름차순)
  SP500_섹터   — S&P 500 섹터별 집계
  NDX100       — 101개 종목 (섹터 포함, 고가대비 오름차순)
  NDX100_섹터  — NASDAQ-100 섹터별 집계

출력: us-market-analysis/output/us_breadth_YYYYMMDD.xlsx
"""

import io
import os
import sys
import warnings
import urllib.request
from pathlib import Path
from datetime import datetime

import pandas as pd

# ── 경로 ─────────────────────────────────────────────────────────────────────
# 산출물 경로는 US_ANALYSIS_DIR 로 덮어쓸 수 있고, 미설정 시 <repo>/reports/us-market-analysis.
BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR  = Path(os.getenv("US_ANALYSIS_DIR", Path(__file__).resolve().parents[1] / "reports" / "us-market-analysis")) / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE_DIR))

DB_PATH = BASE_DIR / "db" / "market_watch.db"
TODAY   = datetime.today().strftime("%Y-%m-%d")
N_DAYS  = 250


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────
def _wiki_html(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; MarketAnalysis/1.0)"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8")


# ── 1. 섹터 매핑 ─────────────────────────────────────────────────────────────
def fetch_sp500_sectors() -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """Wikipedia S&P 500 페이지 → sector_map {ticker: (GICS Sector, Sub-Industry)}, name_map {ticker: 회사명}"""
    html = _wiki_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    df   = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})[0]
    sector_map, name_map = {}, {}
    for _, row in df.iterrows():
        ticker  = str(row["Symbol"]).replace(".", "-").strip()
        sector  = str(row.get("GICS Sector", "")).strip()
        sub_ind = str(row.get("GICS Sub-Industry", "")).strip()
        name    = str(row.get("Security", "")).strip()
        sector_map[ticker] = (sector, sub_ind)
        if name and name != "nan":
            name_map[ticker] = name
    print(f"[섹터] S&P 500 Wikipedia → {len(sector_map)}개 섹터 / {len(name_map)}개 종목명 매핑")
    return sector_map, name_map


def fetch_extra_sectors_yf(tickers: list[str]) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """S&P 500에 없는 종목의 섹터와 회사명을 yfinance로 보완."""
    if not tickers:
        return {}, {}
    import yfinance as yf

    sector_map, name_map = {}, {}
    for t in tickers:
        try:
            info   = yf.Ticker(t).info
            sector = info.get("sector", "") or ""
            indust = info.get("industry", "") or ""
            name   = info.get("shortName") or info.get("longName") or ""
            sector_map[t] = (sector, indust)
            if name:
                name_map[t] = name
        except Exception:
            sector_map[t] = ("", "")
    print(f"[섹터] yfinance 보완 → {len(sector_map)}개 섹터 / {len(name_map)}개 종목명")
    return sector_map, name_map


# ── 2. NASDAQ-100 티커 목록 ───────────────────────────────────────────────────
def fetch_ndx100_tickers() -> list[str]:
    # 2026-09 확인: Nasdaq-100 문서에서 구성종목 표가 빠지고 별도 문서로 분리됐다.
    # 새 문서를 먼저 보고, 원복될 경우를 대비해 기존 문서를 폴백으로 남긴다.
    urls = [
        "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",
        "https://en.wikipedia.org/wiki/Nasdaq-100",
    ]
    df = None
    for url in urls:
        try:
            html = _wiki_html(url)
        except Exception:
            continue
        try:
            df = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})[0]
            break
        except Exception:
            try:
                tables = pd.read_html(io.StringIO(html))
            except Exception:
                continue
            # 구성종목 표만 골라낸다 — 티커 컬럼이 있고 100종목 안팎인 표.
            df = next(
                (t for t in tables
                 if ("Ticker" in t.columns or "Symbol" in t.columns) and len(t) >= 90),
                None,
            )
            if df is not None:
                break

    if df is None:
        raise RuntimeError(
            "NASDAQ-100 구성종목 표를 찾지 못했다 — Wikipedia 문서 구조가 또 바뀌었는지 확인할 것: "
            + ", ".join(urls)
        )

    col     = "Ticker" if "Ticker" in df.columns else "Symbol"
    tickers = [str(t).replace(".", "-").strip() for t in df[col].tolist() if isinstance(t, str)]
    print(f"[NDX100] Wikipedia → {len(tickers)}개 티커")
    return tickers


# ── 3. DB OHLCV 로드 ─────────────────────────────────────────────────────────
def load_from_db(tickers: list[str], n_days: int = N_DAYS) -> pd.DataFrame:
    import sqlite3

    placeholders = ",".join("?" * len(tickers))
    query = f"""
        SELECT name AS ticker, date, close, high, low
        FROM   market_daily
        WHERE  session  = 'us'
          AND  category = 'stock'
          AND  name     IN ({placeholders})
        ORDER  BY name, date
    """
    conn = sqlite3.connect(str(DB_PATH))
    df   = pd.read_sql_query(query, conn, params=tickers)
    conn.close()

    if df.empty:
        return df

    df         = complete_dates_only(df, len(tickers))
    all_dates  = sorted(df["date"].unique())
    cutoff     = all_dates[-n_days] if len(all_dates) >= n_days else all_dates[0]
    return df[df["date"] >= cutoff].copy()


# ── 3-1. 데이터 검증 (sp500_volume_screen도 사용) ─────────────────────────────
COVERAGE_MIN = 0.95   # 날짜별로 이 비율 이상 종목이 있어야 "완전한 날짜"
MAX_LAG_BDAYS = 3     # 요청 기준일 대비 데이터 최신일이 이보다 오래되면 실패


def complete_dates_only(df: pd.DataFrame, n_tickers: int) -> pd.DataFrame:
    """close NULL 행을 버리고, 마지막 완전한 날짜 이후의 부분 날짜를 잘라낸다.

    yfinance 미확정 봉(OHLC NaN + Volume만)이나 일부 종목만 들어온 최신일이 섞이면
    종목마다 기준일이 달라지거나 지표가 전부 NaN이 된다 (2026-09-28 산출물 사례).
    """
    df = df.dropna(subset=["close"]).copy()
    df["date"] = pd.to_datetime(df["date"])
    counts   = df.groupby("date")["ticker"].nunique()
    complete = counts[counts >= n_tickers * COVERAGE_MIN]
    if complete.empty:
        raise RuntimeError(f"완전한 날짜 없음 — 종목 {n_tickers}개 중 {COVERAGE_MIN:.0%} 이상 들어온 날이 없다")
    last = complete.index.max()
    partial = counts[counts.index > last]
    if not partial.empty:
        detail = ", ".join(f"{d:%Y-%m-%d}({n}/{n_tickers})" for d, n in partial.items())
        print(f"  [검증] 부분 날짜 제외: {detail} → 기준 데이터일 {last:%Y-%m-%d}")
    return df[df["date"] <= last]


def check_freshness(data_date, req_date: str) -> None:
    """데이터 최신일이 요청 기준일보다 MAX_LAG_BDAYS 영업일 넘게 뒤처지면 실패."""
    data_date = pd.Timestamp(data_date)
    lag = len(pd.bdate_range(data_date, pd.Timestamp(req_date))) - 1
    if lag > MAX_LAG_BDAYS:
        raise RuntimeError(
            f"DB 데이터가 오래됨 — 최신 {data_date:%Y-%m-%d}, 요청 기준일 {req_date} "
            f"({lag}영업일 차이). 수집(collect_us_stocks) 먼저 실행할 것"
        )
    print(f"  [검증] 데이터 기준일 {data_date:%Y-%m-%d} (요청 {req_date}, {lag}영업일 차이)")


# ── 4. yfinance OHLCV 보완 ───────────────────────────────────────────────────
def download_from_yfinance(tickers: list[str], n_days: int = N_DAYS) -> pd.DataFrame:
    if not tickers:
        return pd.DataFrame()
    import yfinance as yf

    period  = f"{int(n_days * 1.45)}d"
    records = []
    BATCH   = 100
    for i in range(0, len(tickers), BATCH):
        batch = tickers[i: i + BATCH]
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                raw = yf.download(
                    batch, period=period,
                    progress=False, auto_adjust=True, group_by="ticker",
                )
        except Exception as e:
            print(f"  [yf WARN] {e}")
            continue

        if raw is None or raw.empty:
            continue

        single = len(batch) == 1
        for t in batch:
            try:
                sub = (raw if single else raw[t]).dropna(subset=["Close"]).tail(n_days)
                if sub.empty:
                    continue
                for dt, row in sub.iterrows():
                    records.append({
                        "ticker": t,
                        "date":   pd.Timestamp(dt),
                        "close":  float(row["Close"]),
                        "high":   float(row["High"]),
                        "low":    float(row["Low"]),
                    })
            except Exception:
                pass

    return pd.DataFrame(records) if records else pd.DataFrame()


# ── 5. 지표 계산 ──────────────────────────────────────────────────────────────
def calc_metrics(df: pd.DataFrame) -> pd.DataFrame:
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
            "현재가":           round(cur, 2),
            "250일_고가":       round(h250, 2),
            "250일_저가":       round(l250, 2),
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


# ── 6. 메타데이터 부착 ───────────────────────────────────────────────────────
def attach_meta(
    result:     pd.DataFrame,
    name_map:   dict,
    sector_map: dict[str, tuple[str, str]],
) -> pd.DataFrame:
    result = result.copy()
    result.insert(1, "종목명",    result["ticker"].map(name_map).fillna(""))
    result.insert(2, "섹터",      result["ticker"].map(lambda t: sector_map.get(t, ("", ""))[0]).fillna(""))
    result.insert(3, "세부업종",  result["ticker"].map(lambda t: sector_map.get(t, ("", ""))[1]).fillna(""))
    return result


# ── 7. 섹터 집계 ─────────────────────────────────────────────────────────────
def build_sector_summary(df: pd.DataFrame) -> pd.DataFrame:
    """섹터별 종합 집계 테이블."""
    metric_cols = ["고가대비(%)", "저가대비(%)", "1주_수익률(%)", "1개월_수익률(%)", "YTD_수익률(%)",
                   "MA20_이격도(%)", "MA60_이격도(%)", "MA120_이격도(%)", "MA250_이격도(%)"]

    rows = []
    for sector, grp in df.groupby("섹터"):
        n = len(grp)
        row = {"섹터": sector, "종목수": n}

        for col in metric_cols:
            vals = grp[col].dropna()
            if vals.empty:
                row[f"{col}_평균"] = None
                row[f"{col}_중앙값"] = None
            else:
                row[f"{col}_평균"]   = round(vals.mean(), 2)
                row[f"{col}_중앙값"] = round(vals.median(), 2)

        # 신고가권 (-5% 이내) / 강한하락 (-20% 이하) 종목 수
        h_col = grp["고가대비(%)"].dropna()
        row["신고가권(-5%이내)_수"]  = int((h_col >= -5).sum())
        row["하락권(-20%이하)_수"]   = int((h_col <= -20).sum())
        row["신고가권_비율(%)"]       = round((h_col >= -5).mean() * 100, 1)
        row["하락권_비율(%)"]         = round((h_col <= -20).mean() * 100, 1)

        rows.append(row)

    summary = pd.DataFrame(rows).sort_values("섹터")

    # 전체 합계 행
    total = {"섹터": "【전체】", "종목수": len(df)}
    for col in metric_cols:
        vals = df[col].dropna()
        total[f"{col}_평균"]   = round(vals.mean(), 2) if not vals.empty else None
        total[f"{col}_중앙값"] = round(vals.median(), 2) if not vals.empty else None

    h_all = df["고가대비(%)"].dropna()
    total["신고가권(-5%이내)_수"] = int((h_all >= -5).sum())
    total["하락권(-20%이하)_수"]  = int((h_all <= -20).sum())
    total["신고가권_비율(%)"]      = round((h_all >= -5).mean() * 100, 1)
    total["하락권_비율(%)"]        = round((h_all <= -20).mean() * 100, 1)

    return pd.concat([summary, pd.DataFrame([total])], ignore_index=True)


# ── 8. Excel 저장 ─────────────────────────────────────────────────────────────
def _auto_col_width(ws, df: pd.DataFrame):
    for col_idx, col in enumerate(df.columns, 1):
        try:
            max_len = max(df[col].astype(str).map(len).max(), len(str(col))) + 2
        except Exception:
            max_len = len(str(col)) + 2
        ws.column_dimensions[ws.cell(1, col_idx).column_letter].width = min(max_len, 30)


def save_excel(sheets: dict[str, pd.DataFrame], fname: str):
    path = OUT_DIR / fname
    with pd.ExcelWriter(str(path), engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            _auto_col_width(writer.sheets[sheet_name], df)
    print(f"[저장] {path}")
    return path


# ── MAIN ──────────────────────────────────────────────────────────────────────
def run():
    import sqlite3

    print(f"[us_breadth] 기준일: {TODAY}  분석 윈도우: {N_DAYS} 거래일\n")

    # ── 섹터 매핑 + 종목명 (S&P 500 Wikipedia) ───────────────
    print("▶ 섹터 매핑 / 종목명 로드 (Wikipedia S&P 500)")
    sp500_sector_map, wiki_name_map = fetch_sp500_sectors()

    # ── S&P 500 데이터 ───────────────────────────────────────
    print("\n▶ S&P 500 OHLCV 로드 (DB)")
    conn = sqlite3.connect(str(DB_PATH))
    sp_tickers = pd.read_sql_query(
        "SELECT DISTINCT name FROM market_daily WHERE session='us' AND category='stock'",
        conn,
    )["name"].tolist()
    conn.close()

    # Wikipedia 종목명 기본, stocks_daily 이름으로 보완 (실제 회사명만)
    name_map = dict(wiki_name_map)
    print(f"  DB 티커 수: {len(sp_tickers)}")

    sp_df     = load_from_db(sp_tickers)
    if sp_df.empty:
        raise RuntimeError("DB에 US stock 데이터 없음")
    check_freshness(sp_df["date"].max(), TODAY)
    sp_result = calc_metrics(sp_df)
    sp_result = attach_meta(sp_result, name_map, sp500_sector_map)
    sp_result = sp_result.sort_values(["섹터", "고가대비(%)"])
    print(f"  → 지표 계산 완료: {len(sp_result)}개 종목")

    sp_sector = build_sector_summary(sp_result)
    print(f"  → 섹터 집계: {len(sp_sector)-1}개 섹터\n")

    # ── NASDAQ-100 데이터 ────────────────────────────────────
    print("▶ NASDAQ-100 티커 조회 (Wikipedia)")
    ndx_tickers = fetch_ndx100_tickers()

    sp_set    = set(sp_tickers)
    in_db     = [t for t in ndx_tickers if t in sp_set]
    not_in_db = [t for t in ndx_tickers if t not in sp_set]
    print(f"  DB 보유: {len(in_db)}개 | yfinance 보완: {len(not_in_db)}개 → {not_in_db}")

    ndx_df = pd.concat(
        [load_from_db(in_db) if in_db else pd.DataFrame(),
         download_from_yfinance(not_in_db) if not_in_db else pd.DataFrame()],
        ignore_index=True,
    )

    # NDX에만 있는 종목 섹터·종목명 보완
    print("  섹터/종목명 보완 (NDX 전용 종목)")
    extra_sector_map, extra_name_map = fetch_extra_sectors_yf(not_in_db)
    ndx_sector_map = {**sp500_sector_map, **extra_sector_map}
    name_map = {**name_map, **extra_name_map}

    ndx_result = calc_metrics(ndx_df)
    ndx_result = attach_meta(ndx_result, name_map, ndx_sector_map)
    ndx_result = ndx_result.sort_values(["섹터", "고가대비(%)"])
    print(f"  → 지표 계산 완료: {len(ndx_result)}개 종목")

    ndx_sector = build_sector_summary(ndx_result)
    print(f"  → 섹터 집계: {len(ndx_sector)-1}개 섹터\n")

    # ── 저장 ────────────────────────────────────────────────
    fname = f"us_breadth_{TODAY.replace('-','')}.xlsx"
    sheets = {
        "SP500":       sp_result,
        "SP500_섹터":  sp_sector,
        "NDX100":      ndx_result,
        "NDX100_섹터": ndx_sector,
    }
    save_excel(sheets, fname)

    # ── 콘솔 요약 ────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("■ S&P 500 섹터별 요약")
    print(sp_sector[["섹터","종목수","고가대비(%)_평균","MA20_이격도(%)_평균",
                      "신고가권(-5%이내)_수","하락권(-20%이하)_수"]].to_string(index=False))

    print("\n■ NASDAQ-100 섹터별 요약")
    print(ndx_sector[["섹터","종목수","고가대비(%)_평균","MA20_이격도(%)_평균",
                       "신고가권(-5%이내)_수","하락권(-20%이하)_수"]].to_string(index=False))

    return sp_result, ndx_result, sp_sector, ndx_sector


if __name__ == "__main__":
    run()
