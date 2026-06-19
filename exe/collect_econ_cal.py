"""
경제지표 캘린더 수집 v3 (FRED 단독)

Finnhub /api/v1/economic-calendar 는 무료 플랜에서 403 Forbidden 반환 —
economic-calendar 엔드포인트가 유료 플랜 전용으로 제한됨.
매 실행마다 403 후 fallback하는 패턴은 로그 오염 + 레이턴시 낭비이므로
Finnhub 경제지표 호출을 완전 제거하고 FRED를 단독 소스로 사용한다.

FRED: US 지표 12개 (CPI·PCE·NFP·GDP·JOLTS 등)
  - actual(실제치) + previous(이전치) 지원
  - forecast(예상치)는 FRED 미지원 → None
  - period 정보(기준기간) 포함

사용법:
    python collect_econ_cal.py              # 향후 14일
    python collect_econ_cal.py --days 7
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

import requests
import pandas as pd

from db.db_manager import DBManager

FRED_API_KEY = os.getenv("FRED_API_KEY", "")
FRED_BASE    = "https://api.stlouisfed.org/fred"

FRED_SERIES = {
    "CPIAUCSL":    {"name": "CPI YoY",         "freq": "M", "importance": "high",   "release_time_et": "08:30"},
    "CPILFESL":    {"name": "Core CPI YoY",    "freq": "M", "importance": "high",   "release_time_et": "08:30"},
    "PCEPI":       {"name": "PCE",             "freq": "M", "importance": "high",   "release_time_et": "08:30"},
    "PCEPILFE":    {"name": "Core PCE",        "freq": "M", "importance": "high",   "release_time_et": "08:30"},
    "PAYEMS":      {"name": "NFP",             "freq": "M", "importance": "high",   "release_time_et": "08:30"},
    "UNRATE":      {"name": "Unemployment",    "freq": "M", "importance": "high",   "release_time_et": "08:30"},
    "ICSA":        {"name": "Initial Claims",  "freq": "W", "importance": "medium", "release_time_et": "08:30"},
    "JTSJOL":      {"name": "JOLTS",           "freq": "M", "importance": "medium", "release_time_et": "10:00"},
    "RETAILSMNSA": {"name": "Retail Sales",    "freq": "M", "importance": "medium", "release_time_et": "08:30"},
    "INDPRO":      {"name": "Industrial Prod", "freq": "M", "importance": "medium", "release_time_et": "09:15"},
    "HOUST":       {"name": "Housing Starts",  "freq": "M", "importance": "medium", "release_time_et": "08:30"},
    "DGORDER":     {"name": "Durable Goods",   "freq": "M", "importance": "medium", "release_time_et": "08:30"},
    "GDP":         {"name": "GDP QoQ",         "freq": "Q", "importance": "high",   "release_time_et": "08:30"},
    "FEDFUNDS":    {"name": "Fed Funds Rate",  "freq": "M", "importance": "high",   "release_time_et": None},
}

MONTHS_KR = ["1월","2월","3월","4월","5월","6월","7월","8월","9월","10월","11월","12월"]


def _format_period(obs_date: str, freq: str) -> str:
    dt = pd.Timestamp(obs_date)
    if freq == "M":
        return f"{dt.strftime('%Y-%m')} ({MONTHS_KR[dt.month - 1]})"
    elif freq == "Q":
        q = (dt.month - 1) // 3 + 1
        return f"{dt.year}Q{q}"
    elif freq == "W":
        week_end = dt + pd.Timedelta(days=6)
        return f"{dt.strftime('%m/%d')}주"
    return obs_date[:7]


def _advance_date(obs_date: str, freq: str) -> str:
    dt = pd.Timestamp(obs_date)
    if freq == "M":
        return (dt + pd.DateOffset(months=1)).strftime("%Y-%m-%d")
    elif freq == "Q":
        return (dt + pd.DateOffset(months=3)).strftime("%Y-%m-%d")
    elif freq == "W":
        return (dt + pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    return (dt + pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def _fred_get(endpoint: str, params: dict) -> dict:
    if not FRED_API_KEY:
        raise RuntimeError("FRED_API_KEY가 .env에 없습니다.")
    params["api_key"]   = FRED_API_KEY
    params["file_type"] = "json"
    resp = requests.get(f"{FRED_BASE}/{endpoint}", params=params, timeout=20, verify=False)
    resp.raise_for_status()
    return resp.json()


def _fred_observations(series_id: str, n: int = 2) -> list:
    try:
        data = _fred_get("series/observations", {"series_id": series_id, "sort_order": "desc", "limit": n})
        return [o for o in data.get("observations", []) if o.get("value", ".") != "."]
    except Exception as e:
        print(f"  [FRED obs] {series_id}: {e}")
        return []


def _fred_release_dates(series_id: str, from_date: str, to_date: str) -> list:
    try:
        rel = _fred_get("series/release", {"series_id": series_id})
        rid = rel["releases"][0]["id"]
        data = _fred_get("release/dates", {
            "release_id": rid,
            "realtime_start": from_date,
            "realtime_end":   to_date,
            "include_release_dates_with_no_data": "false",
        })
        return sorted(d["date"] for d in data.get("release_dates", []))
    except Exception as e:
        print(f"  [FRED dates] {series_id}: {e}")
        return []


def _collect_fred_series(series_id: str, meta: dict, from_date: str, to_date: str) -> list[dict]:
    """from_date ~ to_date 범위에 release date가 속하는 이벤트 수집.

    과거(from_date ≤ release_date ≤ today): actual 포함
    미래(today < release_date ≤ to_date): actual=None, previous=최신 obs
    """
    freq       = meta.get("freq", "M")
    importance = meta.get("importance", "medium")
    rel_time   = meta.get("release_time_et")
    today      = date.today().strftime("%Y-%m-%d")

    # release dates: from_date 90일 전부터 to_date까지 (obs ↔ rd 매핑에 충분한 범위)
    wide_back = (pd.Timestamp(from_date) - pd.Timedelta(days=90)).strftime("%Y-%m-%d")
    all_rds   = _fred_release_dates(series_id, wide_back, to_date)
    # weekly는 window 내 obs가 여러 개이므로 넉넉하게
    n_obs = 16 if freq == "W" else 6
    obs_list  = _fred_observations(series_id, n=n_obs)   # 최신순 (내림차순)
    records   = []
    seen_rds  = set()

    # ── 발표 완료 (from_date ≤ release_date ≤ today) ─────────────────────
    for j, obs_row in enumerate(obs_list):
        obs_val = obs_row.get("value", ".")
        if obs_val in (".", None):
            continue
        obs_date_str = obs_row["date"]
        actual = float(obs_val)

        # 이 obs의 release date = obs_date 직후 최초 release date
        release_rds = [rd for rd in all_rds if rd > obs_date_str]
        if not release_rds:
            continue
        release_date = release_rds[0]
        if release_date in seen_rds:
            continue

        if from_date <= release_date <= today:
            seen_rds.add(release_date)
            prev_row = next(
                (obs_list[k] for k in range(j + 1, len(obs_list))
                 if obs_list[k].get("value", ".") not in (".", None)),
                None,
            )
            prev_val = float(prev_row["value"]) if prev_row else None
            records.append({
                "event_date": release_date, "event_time": rel_time,
                "country": "US",            "indicator": meta["name"],
                "period": _format_period(obs_date_str, freq),
                "actual": actual,           "forecast": None,
                "previous": prev_val,       "surprise": None,
                "importance": importance,   "source_id": series_id,
            })

    # ── 예정 이벤트 (today < release_date ≤ to_date) ─────────────────────
    future_rds = [rd for rd in all_rds if today < rd <= to_date and rd not in seen_rds]
    latest = next((o for o in obs_list if o.get("value", ".") not in (".", None)), None)

    for i, rd in enumerate(future_rds):
        seen_rds.add(rd)
        prev_val = float(latest["value"]) if latest else None
        if latest:
            nxt = latest["date"]
            for _ in range(i + 1):
                nxt = _advance_date(nxt, freq)
            period = _format_period(nxt, freq)
        else:
            period = None
        records.append({
            "event_date": rd,  "event_time": rel_time,
            "country": "US",   "indicator": meta["name"],
            "period": period,  "actual": None,
            "forecast": None,  "previous": prev_val,
            "surprise": None,  "importance": importance,
            "source_id": series_id,
        })

    return records


def _this_monday_next_friday() -> tuple[str, str]:
    """이번주 월요일 ~ 다음주 금요일 날짜 반환."""
    today       = date.today()
    this_monday = today - timedelta(days=today.weekday())        # 0=월
    next_friday = this_monday + timedelta(days=11)               # +1주 +4일
    return this_monday.strftime("%Y-%m-%d"), next_friday.strftime("%Y-%m-%d")


def collect_econ_calendar(from_date: str = None, to_date: str = None,
                          days_ahead: int = None, **_kwargs) -> list[dict]:
    """경제지표 캘린더 수집 (FRED 단독).

    수집 범위: from_date(기본=이번주 월요일) ~ to_date(기본=다음주 금요일).
    days_ahead 지정 시 to_date = today + days_ahead (구버전 호환).
    """
    if from_date is None:
        from_date, _ = _this_monday_next_friday()
    if to_date is None:
        if days_ahead is not None:
            to_date = (date.today() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        else:
            _, to_date = _this_monday_next_friday()

    if not FRED_API_KEY:
        print("  [collect_econ_cal] FRED_API_KEY 없음 — 건너뜀")
        return []

    print(f"  [FRED] {from_date} ~ {to_date} 경제지표 수집 중...")
    all_records = []
    for series_id, meta in FRED_SERIES.items():
        print(f"  [FRED] {series_id:15s} {meta['name']}")
        recs = _collect_fred_series(series_id, meta, from_date, to_date)
        all_records.extend(recs)
    print(f"  [FRED] 수집 완료: {len(all_records)}건")
    return all_records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14,
                        help="오늘부터 수집할 향후 일수 (기본 14)")
    args = parser.parse_args()

    print(f"[collect_econ_cal] 향후 {args.days}일 경제지표 수집 중...")
    records = collect_econ_calendar(days_ahead=args.days)
    if not records:
        print("  수집된 데이터 없음")
        return

    db = DBManager()
    n  = db.upsert_econ_event(records)
    print(f"[collect_econ_cal] 저장 완료: {n}건")

    df = pd.DataFrame(records).sort_values(
        ["event_date", "importance"], ascending=[True, True]
    )
    cols = ["event_date", "event_time", "country", "indicator",
            "actual", "forecast", "previous", "importance"]
    print(df[cols].to_string(index=False))


if __name__ == "__main__":
    main()
