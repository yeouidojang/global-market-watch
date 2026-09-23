"""
SPX 구성종목 OHLCV 일별 수집 (yfinance)

티커 소스 (우선순위):
  1. SPX_CSV_PATH 환경변수 또는 data/spx_constituents.csv (있는 경우)
  2. LSEG get_data(universe="0#.SPX")
  3. Wikipedia S&P 500 목록 (폴백)
저장 대상: DB market_daily (session=us, category=stock)

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
from dotenv import load_dotenv

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
sys.path.insert(0, str(BASE_DIR))

from db.db_manager import DBManager

# S&P500 구성종목 CSV — 환경변수 SPX_CSV_PATH 또는 data/ 하위 기본 경로
SPX_CSV_PATH = Path(os.getenv("SPX_CSV_PATH", str(BASE_DIR / "data" / "spx_constituents.csv")))

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


def _fetch_spx_tickers_lseg() -> list[str]:
    """LSEG Chain RIC(0#.SPX)으로 S&P 500 구성종목 티커 조회."""
    import lseg.data as ld

    session_opened = False
    try:
        try:
            state = ld.session.get_default().open_state.name
        except Exception:
            state = "Closed"
        if state != "Opened":
            cfg_path = os.getenv("LSEG_CONFIG_PATH", str(Path.home() / "lseg-data.config.json"))
            ld.open_session(config_name=cfg_path)
            session_opened = True

        df = ld.get_data(universe="0#.SPX", fields=["TR.RIC"])
        if df is None or df.empty:
            return []

        rics = df["Instrument"].dropna().tolist()
        tickers = [_ric_to_yf(str(r)) for r in rics]
        tickers = [t for t in tickers if t]
        print(f"[INFO] LSEG SPX 구성종목: {len(tickers)}개")
        return tickers
    except Exception as e:
        print(f"[WARN] LSEG SPX 구성종목 조회 실패: {e}")
        return []
    finally:
        if session_opened:
            try:
                ld.close_session()
            except Exception:
                pass


def load_spx_tickers() -> list[str]:
    """yfinance ticker 목록 반환.

    우선순위: DB(spx_constituents) → CSV → LSEG(0#.SPX)
    LSEG 실패 시 Slack 알림 후 RuntimeError 발생.
    """
    try:
        db_tickers = DBManager().get_latest_spx_constituents()
        if db_tickers:
            print(f"[INFO] DB spx_constituents → {len(db_tickers)}개 종목")
            return db_tickers
    except Exception as e:
        print(f"[WARN] DB spx_constituents 조회 실패: {e}")

    if SPX_CSV_PATH.exists():
        cols = pd.read_csv(SPX_CSV_PATH, nrows=0).columns.tolist()
        tickers = [_ric_to_yf(c) for c in cols if c.lower() != "date"]
        tickers = [t for t in tickers if t]
        if tickers:
            return tickers

    print("[INFO] SPX CSV/DB 없음 → LSEG에서 SPX 구성종목 조회")
    tickers = _fetch_spx_tickers_lseg()
    if tickers:
        return tickers

    msg = "🚨 *Market Watch 오류* [collect_us_stocks]\nLSEG SPX 구성종목 조회 실패 — 파이프라인 중단"
    try:
        from summarize.notify_slack import send_text
        send_text(msg)
    except Exception:
        pass
    raise RuntimeError("LSEG SPX 구성종목 조회 실패")


def fetch_ohlcv(tickers: list[str], start: str, end: str) -> list[dict]:
    """
    yfinance 배치 다운로드 → market_daily 삽입용 레코드 리스트.

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
                        "date":     dt.strftime("%Y-%m-%d"),
                        "session":  "us",
                        "category": "stock",
                        "name":     t,
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
    saved = db.upsert_market_daily(records)
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
