"""
SPX 구성종목 OHLCV 일별 수집 (yfinance)

티커 소스: SPX_CSV_PATH 환경변수 또는 data/spx_constituents_prices.csv (RIC 헤더 → yfinance 변환)
저장 대상: DB us_stocks_daily 테이블

사용법:
    python exe/collect_us_stocks.py                        # 오늘
    python exe/collect_us_stocks.py --date 2026-06-19
    python exe/collect_us_stocks.py --start 2026-06-01 --end 2026-06-19
"""

import os
import sys
import argparse
import warnings
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
sys.path.insert(0, str(BASE_DIR))

from db.db_manager import DBManager

# S&P500 구성종목 CSV — 환경변수 SPX_CSV_PATH 우선, 없으면 프로젝트 내 data/ 폴더
SPX_CSV_PATH = Path(os.environ.get("SPX_CSV_PATH", BASE_DIR / "data" / "spx_constituents_prices.csv"))

# RIC suffix 제거 + 특수 케이스 매핑
_RIC_SPECIALS = {
    "BRKb": "BRK-B", "BRKa": "BRK-A",
    "BFb":  "BF-B",  "BFa":  "BF-A",
    "WAT_z": None,
}


def _ric_to_yf(ric: str) -> str | None:
    base = ric.rsplit(".", 1)[0] if "." in ric else ric
    if base in _RIC_SPECIALS:
        return _RIC_SPECIALS[base]
    return base


def load_spx_tickers() -> list[str]:
    """CSV 헤더에서 yfinance ticker 목록 반환."""
    if not SPX_CSV_PATH.exists():
        print(f"[ERROR] CSV not found: {SPX_CSV_PATH}")
        return []
    cols = pd.read_csv(SPX_CSV_PATH, nrows=0).columns.tolist()
    tickers = []
    for c in cols:
        if c.lower() == "date":
            continue
        yf_t = _ric_to_yf(c)
        if yf_t:
            tickers.append(yf_t)
    return tickers


def fetch_ohlcv(tickers: list[str], start: str, end: str) -> list[dict]:
    """
    yfinance 배치 다운로드 → us_stocks_daily 삽입용 레코드 리스트.

    Parameters
    ----------
    start, end : YYYY-MM-DD (yfinance end는 exclusive → 호출 측에서 +1일 처리)
    """
    import yfinance as yf

    records = []
    if not tickers:
        return records

    # yfinance는 500종목 이상 한 번에 받으면 불안정 → 배치 분할
    BATCH = 200
    for i in range(0, len(tickers), BATCH):
        batch = tickers[i: i + BATCH]
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                raw = yf.download(
                    batch, start=start, end=end,
                    progress=False, auto_adjust=True, group_by="ticker",
                )
        except Exception as e:
            print(f"    [yf download ERROR] batch {i//BATCH}: {e}")
            continue

        if raw is None or raw.empty:
            continue

        single = len(batch) == 1
        for t in batch:
            try:
                df = raw if single else raw[t]
                df = df.dropna(subset=["Close"])
                if df.empty:
                    continue
                for dt, row in df.iterrows():
                    records.append({
                        "date":   dt.strftime("%Y-%m-%d"),
                        "ticker": t,
                        "open":   round(float(row["Open"]),   4) if pd.notna(row["Open"])   else None,
                        "high":   round(float(row["High"]),   4) if pd.notna(row["High"])   else None,
                        "low":    round(float(row["Low"]),    4) if pd.notna(row["Low"])    else None,
                        "close":  round(float(row["Close"]),  4) if pd.notna(row["Close"])  else None,
                        "volume": float(row["Volume"])            if pd.notna(row["Volume"]) else None,
                    })
            except Exception:
                pass

    return records


def run(start_date: str, end_date: str):
    """start_date ~ end_date 범위 OHLCV 수집 후 DB 저장."""
    tickers = load_spx_tickers()
    if not tickers:
        return
    print(f"[collect_us_stocks] 티커 {len(tickers)}개 | {start_date} ~ {end_date}")

    # yfinance end는 exclusive → 하루 뒤 날짜 전달
    yf_end = (pd.Timestamp(end_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    records = fetch_ohlcv(tickers, start=start_date, end=yf_end)
    if not records:
        print("  → 수집된 데이터 없음")
        return

    # 날짜 범위 필터 (yfinance가 end 직전까지 반환하므로 명시적 필터)
    records = [r for r in records if start_date <= r["date"] <= end_date]

    db = DBManager()
    saved = db.upsert_us_stocks_daily(records)
    dates = sorted({r["date"] for r in records})
    print(f"  → {len(records)}건 저장 (날짜 {len(dates)}일, upsert {saved}건)")


def _parse_args():
    p = argparse.ArgumentParser(description="SPX 종목 OHLCV 수집")
    p.add_argument("--date",  help="단일 날짜 YYYY-MM-DD (기본: 오늘)")
    p.add_argument("--start", help="시작 날짜 YYYY-MM-DD")
    p.add_argument("--end",   help="종료 날짜 YYYY-MM-DD (기본: 오늘)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    today = pd.Timestamp.now().strftime("%Y-%m-%d")

    if args.date:
        start = end = args.date
    else:
        start = args.start or today
        end   = args.end   or today

    run(start, end)
