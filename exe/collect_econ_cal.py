"""
경제지표 캘린더 수집 (FRED API)

FRED에서 주요 미국 경제지표 최신 발표값·예상값 수집.
향후 발표 예정일은 FRED release dates API로 조회.

사용법:
    python collect_econ_cal.py              # 오늘 기준 과거 7일 + 향후 7일
    python collect_econ_cal.py --days 14    # 향후 14일
"""

import os
import sys
import argparse
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR.parent / ".env")
sys.path.insert(0, str(BASE_DIR))

import requests
import pandas as pd

from db.db_manager import DBManager

FRED_API_KEY = os.getenv("FRED_API_KEY", "")
FRED_BASE = "https://api.stlouisfed.org/fred"

# 추적할 FRED 시리즈 (series_id → 메타)
FRED_SERIES = {
    "CPIAUCSL":  {"name": "CPI YoY",          "country": "US"},
    "CPILFESL":  {"name": "Core CPI YoY",      "country": "US"},
    "PCEPI":     {"name": "PCE",               "country": "US"},
    "PCEPILFE":  {"name": "Core PCE",          "country": "US"},
    "PAYEMS":    {"name": "NFP",               "country": "US"},
    "UNRATE":    {"name": "Unemployment Rate", "country": "US"},
    "JTSJOL":    {"name": "JOLTS",             "country": "US"},
    "RETAILSMNSA":{"name": "Retail Sales",     "country": "US"},
    "INDPRO":    {"name": "Industrial Prod",   "country": "US"},
    "HOUST":     {"name": "Housing Starts",    "country": "US"},
    "GDP":       {"name": "GDP QoQ",           "country": "US"},
    "FEDFUNDS":  {"name": "Fed Funds Rate",    "country": "US"},
}


def fred_get(endpoint: str, params: dict) -> dict:
    """FRED REST API 호출."""
    if not FRED_API_KEY:
        raise RuntimeError("FRED_API_KEY가 .env에 없습니다. https://fred.stlouisfed.org/docs/api/api_key.html 에서 무료 발급")
    params["api_key"]      = FRED_API_KEY
    params["file_type"]    = "json"
    resp = requests.get(f"{FRED_BASE}/{endpoint}", params=params, timeout=20, verify=False)
    resp.raise_for_status()
    return resp.json()


def fetch_series_latest(series_id: str) -> dict | None:
    """시리즈 최근 발표값 1건 반환."""
    try:
        data = fred_get("series/observations", {
            "series_id": series_id,
            "sort_order": "desc",
            "limit": 2,
        })
        obs = data.get("observations", [])
        if not obs:
            return None
        latest = obs[0]
        prev   = obs[1] if len(obs) > 1 else {}
        return {
            "event_date": latest["date"],
            "actual":     float(latest["value"]) if latest["value"] != "." else None,
            "previous":   float(prev["value"])   if prev.get("value", ".") != "." else None,
        }
    except Exception as e:
        print(f"  [FRED ERROR] {series_id}: {e}")
        return None


def fetch_release_dates(series_id: str, from_date: str, to_date: str) -> list[str]:
    """시리즈 발표 예정일 목록 반환."""
    try:
        # series → release_id 조회
        rel_data = fred_get("series/release", {"series_id": series_id})
        release_id = rel_data["releases"][0]["id"]

        dates_data = fred_get("release/dates", {
            "release_id": release_id,
            "realtime_start": from_date,
            "realtime_end":   to_date,
            "include_release_dates_with_no_data": "false",
        })
        return [d["date"] for d in dates_data.get("release_dates", [])]
    except Exception as e:
        print(f"  [FRED dates ERROR] {series_id}: {e}")
        return []


def collect_econ_calendar(days_ahead: int = 7) -> list[dict]:
    """
    FRED에서 주요 경제지표 최신값 + 향후 발표일 수집.
    반환: econ_calendar 테이블 포맷 레코드 리스트
    """
    today     = date.today()
    from_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    to_date   = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    records = []
    for series_id, meta in FRED_SERIES.items():
        print(f"  [FRED] {series_id} ({meta['name']})")
        latest = fetch_series_latest(series_id)
        if latest:
            records.append({
                "event_date": latest["event_date"],
                "event_time": None,
                "country":    meta["country"],
                "indicator":  meta["name"],
                "period":     None,
                "actual":     latest.get("actual"),
                "forecast":   None,
                "previous":   latest.get("previous"),
                "surprise":   None,
            })

        # 향후 발표 예정일 (actual=None)
        upcoming = fetch_release_dates(series_id, from_date, to_date)
        for d in upcoming:
            if d > today.strftime("%Y-%m-%d"):
                records.append({
                    "event_date": d,
                    "event_time": None,
                    "country":    meta["country"],
                    "indicator":  meta["name"],
                    "period":     None,
                    "actual":     None,
                    "forecast":   None,
                    "previous":   None,
                    "surprise":   None,
                })

    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="향후 몇 일 발표 예정일 조회")
    args = parser.parse_args()

    print(f"[collect_econ_cal] 향후 {args.days}일 경제지표 수집 중...")
    records = collect_econ_calendar(days_ahead=args.days)

    if not records:
        print("  수집된 데이터 없음")
        return

    db = DBManager()
    n  = db.upsert_econ_event(records)
    print(f"[collect_econ_cal] 저장 완료: {n}건")

    # 미리보기
    df = pd.DataFrame(records).sort_values("event_date")
    print(df[["event_date", "country", "indicator", "actual", "previous"]].to_string(index=False))


if __name__ == "__main__":
    main()
