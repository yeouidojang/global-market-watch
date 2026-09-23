"""
EPS 캐시 과거 금요일 스냅샷 백필

eps_cache는 (ticker, fetched_date) 히스토리로 NTM EPS 원본 추정치(eps_mean_est)를 쌓고,
1주/1개월(4주)/3개월(12주) 변화율은 그 히스토리에서 파생 계산한다 (collect_stocks.calc_eps_changes).
과거로 갈수록 더 이전 스냅샷과 비교해야 하므로 반드시 오래된 금요일부터 순서대로 채운다
(3개월 변화율을 얻으려면 최소 12주치 히스토리가 필요).

사용법:
    python exe/backfill_eps_cache.py               # 최근 12개 금요일
    python exe/backfill_eps_cache.py --weeks 8      # 최근 8개 금요일
    python exe/backfill_eps_cache.py --date 2026-04-03            # 특정 날짜만 백필
    python exe/backfill_eps_cache.py --date 2026-04-03 --date 2026-04-10  # 복수 지정
"""

import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from db.db_manager import DBManager
from exe.collect_stocks import _lseg_screener, calc_eps_changes


def _last_n_fridays(n: int, from_date: date = None) -> list[str]:
    """오늘 이전(당일 제외) 가장 최근 금요일부터 역순으로 n개 날짜 반환."""
    d = from_date or date.today()
    offset = (d.weekday() - 4) % 7
    offset = offset if offset != 0 else 7
    last_friday = d - timedelta(days=offset)
    return [(last_friday - timedelta(weeks=i)).strftime("%Y-%m-%d") for i in range(n)]


def run(weeks: int = 12, dates: list[str] | None = None):
    tickers = DBManager().get_latest_spx_constituents()
    if not tickers:
        raise RuntimeError("spx_constituents 비어있음 — 먼저 exe/collect_spx_constituents.py 실행 필요")

    # --date 지정 시 그 날짜(들)만, 아니면 최근 N개 금요일 (오래된 순)
    target_dates = sorted(dates) if dates else sorted(_last_n_fridays(weeks))
    print(f"[backfill_eps] 대상 티커 {len(tickers)}개 | 대상일 {len(target_dates)}개: {target_dates}")

    import lseg.data as ld
    cfg_path = os.getenv("LSEG_CONFIG_PATH", str(Path.home() / "lseg-data.config.json"))
    ld.open_session(config_name=cfg_path)
    try:
        for d in target_dates:
            print(f"\n▶ {d} NTM EPS 추정치 조회 중...")
            t0 = time.time()
            meta = _lseg_screener(tickers, fetch_eps=True, target_date=d, eps_universe=tickers)
            estimates = {t: m.get("eps_mean_est") for t, m in meta.items()
                         if m.get("eps_mean_est") is not None}
            changes = calc_eps_changes(d, estimates)
            records = [
                {
                    "ticker": t, "eps_mean_est": est,
                    "eps_chg_1w": changes.get(t, {}).get("eps_chg_1w"),
                    "eps_chg_1m": changes.get(t, {}).get("eps_chg_1m"),
                    "eps_chg_3m": changes.get(t, {}).get("eps_chg_3m"),
                    "fetched_date": d,
                }
                for t, est in estimates.items()
            ]
            saved = DBManager().upsert_eps_cache(records)
            has_3m = sum(1 for r in records if r["eps_chg_3m"] is not None)
            elapsed = round(time.time() - t0)
            print(f"  → {len(records)}개 종목 원본값 수집 (3M 계산됨 {has_3m}개) → DB upsert {saved}건 ({elapsed}s)")
    finally:
        try:
            ld.close_session()
        except Exception:
            pass

    print("\n[backfill_eps] 완료")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="EPS 캐시 과거 스냅샷 백필")
    p.add_argument("--weeks", type=int, default=12, help="백필할 금요일 개수 (기본 12, 3M 계산에 필요한 최소치)")
    p.add_argument("--date", action="append", help="특정 날짜만 백필 (YYYY-MM-DD, 반복 지정 가능). 지정 시 --weeks 무시")
    args = p.parse_args()
    run(args.weeks, dates=args.date)
