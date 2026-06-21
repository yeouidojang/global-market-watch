"""
market_daily.market 컬럼 과거 데이터 백필

pykrx에서 현재 KOSPI/KOSDAQ 종목 리스트를 가져와
session='asia', category='stock', market IS NULL 인 레코드를 업데이트.

실행:
    python exe/backfill_market.py
    python exe/backfill_market.py --dry-run   # 실제 쓰기 없이 통계만 출력
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# .env 로드 (KRX_ID / KRX_PW 등)
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from pykrx import stock as krx
from db.db_manager import DBManager


def _last_business_day() -> str:
    """오늘 포함 직전 영업일 (주말 건너뜀)."""
    from datetime import date, timedelta
    d = date.today()
    while d.weekday() >= 5:      # 5=토, 6=일
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def main(dry_run: bool = False) -> None:
    db   = DBManager()
    conn = db._connect()

    null_count = conn.execute(
        "SELECT COUNT(*) FROM market_daily "
        "WHERE session='asia' AND category='stock' AND market IS NULL"
    ).fetchone()[0]
    print(f"[backfill] market=NULL 레코드: {null_count:,}건")

    if null_count == 0:
        print("[backfill] 백필할 데이터 없음")
        conn.close()
        return

    # 고유 티커 목록
    rows    = conn.execute(
        "SELECT DISTINCT name FROM market_daily "
        "WHERE session='asia' AND category='stock' AND market IS NULL"
    ).fetchall()
    tickers = [r[0] for r in rows]
    print(f"[backfill] 대상 티커: {len(tickers):,}개")

    # pykrx로 KOSPI / KOSDAQ 매핑 빌드 (직전 영업일 날짜 명시)
    bday = _last_business_day()
    print(f"[backfill] pykrx KOSPI/KOSDAQ 티커 조회 중... (기준일: {bday})")
    kospi_tickers  = set(krx.get_market_ticker_list(date=bday, market="KOSPI"))
    kosdaq_tickers = set(krx.get_market_ticker_list(date=bday, market="KOSDAQ"))
    print(f"           KOSPI {len(kospi_tickers):,}개 / KOSDAQ {len(kosdaq_tickers):,}개")

    ticker_to_market: dict[str, str] = {t: "KOSPI"  for t in kospi_tickers}
    ticker_to_market.update(          {t: "KOSDAQ" for t in kosdaq_tickers})

    # 분류
    updates   = [(ticker_to_market[t], t) for t in tickers if t in ticker_to_market]
    unmapped  = [t for t in tickers if t not in ticker_to_market]

    print(f"[backfill] 매핑 성공: {len(updates):,}개 / 미매핑(ETF·상폐 등): {len(unmapped):,}개")
    if unmapped[:10]:
        print(f"           미매핑 예시: {unmapped[:10]}")

    if dry_run:
        print("[backfill] --dry-run 모드: DB 쓰기 생략")
        conn.close()
        return

    # 업데이트 (배치)
    conn.executemany(
        "UPDATE market_daily SET market=? "
        "WHERE name=? AND session='asia' AND category='stock' AND market IS NULL",
        updates,
    )
    conn.commit()

    updated = conn.execute(
        "SELECT COUNT(*) FROM market_daily "
        "WHERE session='asia' AND category='stock' AND market IS NOT NULL"
    ).fetchone()[0]
    conn.close()

    print(f"[backfill] 완료 — market_daily 중 market 설정된 레코드: {updated:,}건")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="DB 쓰기 없이 통계만 출력")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
