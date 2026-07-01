"""
S&P 500 구성종목 리스트 수집 → DB(spx_constituents) 저장

LSEG Chain RIC(0#.SPX)으로 현재 구성종목을 조회해 DB에 스냅샷으로 저장한다.
구성종목은 자주 바뀌지 않으므로 주기적(예: 월 1회) 수동/크론 실행을 권장한다.
(일별 파이프라인의 collect_us_stocks.py는 여전히 CSV → LSEG 순으로 자체 조회한다.)

사용법:
    python exe/collect_spx_constituents.py
    python exe/collect_spx_constituents.py --date 2026-07-01
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from db.db_manager import DBManager
from exe.collect_us_stocks import _fetch_spx_tickers_lseg

TODAY = datetime.today().strftime("%Y-%m-%d")


def run(date_str: str = TODAY):
    print(f"[collect_spx_constituents] LSEG(0#.SPX) 구성종목 조회 시작 ({date_str})")
    tickers = _fetch_spx_tickers_lseg()

    if not tickers:
        print("  → 조회 실패 또는 결과 없음")
        try:
            from summarize.notify_slack import send_ops
            send_ops(f"❌ SPX 구성종목 조회 실패 ({date_str}) — LSEG 0#.SPX 응답 없음")
        except Exception as e:
            print(f"[ops 알림 실패] {e}")
        raise RuntimeError("LSEG SPX 구성종목 조회 실패")

    records = [{"ticker": t, "fetched_date": date_str} for t in tickers]
    db = DBManager()
    saved = db.upsert_spx_constituents(records)
    print(f"  → {len(tickers)}개 종목 조회 → DB upsert {saved}건")
    return tickers


def _parse_args():
    p = argparse.ArgumentParser(description="SPX 구성종목 리스트 수집")
    p.add_argument("--date", default=TODAY, help="조회 기준일 YYYY-MM-DD (기본: 오늘)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(args.date)
