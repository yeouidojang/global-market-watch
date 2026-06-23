"""
어닝 캘린더 수집 (Finnhub earnings_calendar)

- 한 번의 API 호출(symbol="")로 전 종목 어닝 가져온 뒤 SPX 유니버스로 필터링
- Finnhub 응답 필드:
    date, hour (bmo|amc|dmh|""), symbol, year, quarter,
    epsEstimate, epsActual, revenueEstimate, revenueActual

사용법:
    python collect_earnings_cal.py                          # 향후 7일
    python collect_earnings_cal.py --days 14
    python collect_earnings_cal.py --from 2026-06-09 --to 2026-06-30
    python collect_earnings_cal.py --no-filter              # SPX 필터링 끔
"""

import os
import sys
import argparse
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
sys.path.insert(0, str(BASE_DIR))

import pandas as pd

from db.db_manager import DBManager

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")


def _get_finnhub_client():
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY가 .env에 없습니다.")
    import finnhub
    # finnhub-python은 내부적으로 requests 사용 → 사내망 SSL 이슈 대비
    client = finnhub.Client(api_key=FINNHUB_API_KEY)
    try:
        # 내부 세션의 SSL 검증 비활성화 (다른 모듈들과 동일한 정책)
        if hasattr(client, "_session"):
            client._session.verify = False
    except Exception:
        pass
    return client


def _load_spx_symbols() -> set[str]:
    """SPX 구성종목 ticker set. market_daily(us/stock) DB에서 로드."""
    try:
        import sqlite3
        from pathlib import Path
        db_path = BASE_DIR / "db" / "market_watch.db"
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT DISTINCT name FROM market_daily WHERE session='us' AND category='stock'"
        ).fetchall()
        conn.close()
        symbols = {r[0] for r in rows if r[0]}
        if symbols:
            print(f"  [SPX 유니버스] DB에서 {len(symbols)}개 로드 (market_daily)")
            return symbols
    except Exception as e:
        print(f"  [SPX 유니버스 DB 로딩 실패] {e}")
    return set()


def _to_record(row: dict) -> dict:
    """Finnhub API row → DB upsert 레코드."""
    eps_est = row.get("epsEstimate")
    eps_act = row.get("epsActual")
    surprise = None
    try:
        if eps_est is not None and eps_act is not None and float(eps_est) != 0:
            surprise = (float(eps_act) - float(eps_est)) / abs(float(eps_est)) * 100
            surprise = round(surprise, 2)
    except (TypeError, ValueError):
        surprise = None

    return {
        "event_date":       row.get("date"),
        "hour":             (row.get("hour") or "").lower() or None,
        "symbol":           row.get("symbol"),
        "year":             row.get("year"),
        "quarter":          row.get("quarter"),
        "eps_estimate":     eps_est,
        "eps_actual":       eps_act,
        "revenue_estimate": row.get("revenueEstimate"),
        "revenue_actual":   row.get("revenueActual"),
        "surprise_pct":     surprise,
        "source":           "finnhub",
    }


def collect_earnings_calendar(days_ahead: int = None,
                              from_date: str | None = None,
                              to_date: str | None = None,
                              filter_spx: bool = True) -> list[dict]:
    """
    Finnhub earnings_calendar 수집.

    Parameters
    ----------
    days_ahead : 오늘로부터 향후 N일 (from/to 모두 None이고 days_ahead 지정 시 사용)
    from_date  : YYYY-MM-DD (기본: 이번주 월요일)
    to_date    : YYYY-MM-DD (기본: 다음주 금요일)
    filter_spx : True면 SPX 구성종목으로 필터링
    """
    today = date.today()
    if from_date is None:
        this_monday = today - timedelta(days=today.weekday())
        from_date = this_monday.strftime("%Y-%m-%d")
    if to_date is None:
        if days_ahead is not None:
            to_date = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        else:
            this_monday = today - timedelta(days=today.weekday())
            to_date = (this_monday + timedelta(days=11)).strftime("%Y-%m-%d")

    print(f"  [Finnhub] earnings_calendar {from_date} ~ {to_date}")
    client = _get_finnhub_client()
    try:
        resp = client.earnings_calendar(_from=from_date, to=to_date, symbol="")
    except Exception as e:
        print(f"  [Finnhub ERROR] {e}")
        return []

    rows = resp.get("earningsCalendar", []) if isinstance(resp, dict) else []
    print(f"  [Finnhub] 전체 응답: {len(rows)}건")

    if filter_spx:
        spx = _load_spx_symbols()
        if spx:
            rows = [r for r in rows if r.get("symbol") in spx]
            print(f"  [Finnhub] SPX 필터링 후: {len(rows)}건 (유니버스 {len(spx)}개)")
        else:
            print(f"  [Finnhub] SPX 유니버스 없음 → 필터링 생략")

    records = [_to_record(r) for r in rows if r.get("date") and r.get("symbol")]
    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",  type=int, default=7,
                        help="향후 N일 (기본 7)")
    parser.add_argument("--from",  dest="from_date", default=None)
    parser.add_argument("--to",    dest="to_date",   default=None)
    parser.add_argument("--no-filter", action="store_true",
                        help="SPX 유니버스 필터링 끔 (전 종목 저장)")
    args = parser.parse_args()

    print(f"[collect_earnings_cal] 어닝 캘린더 수집 시작")
    records = collect_earnings_calendar(
        days_ahead=args.days,
        from_date=args.from_date,
        to_date=args.to_date,
        filter_spx=not args.no_filter,
    )
    if not records:
        print("  수집된 데이터 없음")
        return

    db = DBManager()
    n = db.upsert_earnings_event(records)
    print(f"[collect_earnings_cal] 저장 완료: {n}건")

    df = pd.DataFrame(records)
    if not df.empty:
        df = df.sort_values(["event_date", "hour", "symbol"])
        cols = ["event_date", "hour", "symbol", "year", "quarter",
                "eps_estimate", "eps_actual", "surprise_pct"]
        print(df[cols].head(30).to_string(index=False))


if __name__ == "__main__":
    main()
