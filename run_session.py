"""
수동 원샷 실행 — 특정 세션 전체 파이프라인 실행

사용법:
    python run_session.py --session us
    python run_session.py --session asia --date 2026-06-09
    python run_session.py --session us --no-notify   # Slack 발송 생략
    python run_session.py --session us --no-llm      # 브리핑 생략, 데이터 수집만
"""

import sys, io
if hasattr(sys.stdout, 'buffer') and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import ssl, httpx
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE
_orig_c = httpx.Client.__init__
_orig_a = httpx.AsyncClient.__init__
def _fix_proxy(kw):
    kw.setdefault('verify', False)
    if 'proxies' in kw:
        proxies = kw.pop('proxies')
        if isinstance(proxies, dict):
            url = next((v for v in proxies.values() if v), None)
            if url:
                kw.setdefault('proxy', httpx.Proxy(url))
        elif proxies is not None:
            kw.setdefault('proxy', proxies)
    if 'proxy' in kw and isinstance(kw['proxy'], dict):
        url = next((v for v in kw['proxy'].values() if v), None)
        kw['proxy'] = httpx.Proxy(url) if url else None
def _pc(self, *a, **kw): _fix_proxy(kw); _orig_c(self, *a, **kw)
def _pa(self, *a, **kw): _fix_proxy(kw); _orig_a(self, *a, **kw)
httpx.Client.__init__ = _pc
httpx.AsyncClient.__init__ = _pa
import urllib3; urllib3.disable_warnings()

import sys
import argparse
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

KST = timezone(timedelta(hours=9))


def _prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:   # 5=토, 6=일
        d -= timedelta(days=1)
    return d


def _default_date(session: str) -> str:
    """세션별 기본 target_date 반환.

    us/europe/global 세션은 KST 당일 오전(< 12시)에 실행될 때
    전일 시장(= 전 영업일)을 분석 대상으로 삼는다.
    예) 2026-06-11 06:10 KST 실행 → US 시장 마감일 2026-06-10
    """
    now_kst = datetime.now(KST)
    today   = now_kst.date()
    if session in ("us", "europe", "global") and now_kst.hour < 12:
        return _prev_weekday(today).strftime("%Y-%m-%d")
    return today.strftime("%Y-%m-%d")

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env")
sys.path.insert(0, str(BASE_DIR))


def run_session(session: str, target_date: str,
                skip_llm: bool = False, skip_notify: bool = False):

    print(f"\n{'='*55}")
    print(f"  Global Market Watch  |  {session.upper()}  |  {target_date}")
    print(f"{'='*55}")

    # Step 1: 데이터 수집
    import yaml
    from exe.collect_macro import (
        load_yaml, lseg_open, lseg_close,
        collect_indices, collect_macro, DBManager
    )

    indices_cfg = load_yaml("indices.yaml")
    macro_cfg   = load_yaml("macro.yaml")
    db = DBManager()

    print(f"\n[1/4] 데이터 수집 (session={session})")
    lseg_open()
    try:
        n = collect_indices(session, target_date, indices_cfg, db)
        print(f"  지수 upserted: {n}건")
        n = collect_macro(target_date, macro_cfg, db)
        print(f"  매크로 upserted: {n}건")
    finally:
        lseg_close()

    # Step 2: 경제지표/어닝 캘린더 — 글로벌 통합 파이프라인(06:10)에서만 수집
    print(f"\n[2/4] 경제지표 캘린더 수집 (skip: {session} — global 파이프라인에서 일괄 수집)")

    # Step 3: LLM 브리핑
    if skip_llm:
        print(f"\n[3/4] LLM 브리핑 (skip: --no-llm)")
        return

    # 한국 세션이면 주요 종목 / 특징주 수집
    stocks_data = {}
    if session == "asia":
        print(f"\n[3/4-pre] KOSPI/KOSDAQ 종목 수집")
        from exe.collect_stocks import fetch_top_stocks, fetch_asia_overseas_stocks
        stocks_data = fetch_top_stocks(target_date)
        print(f"  주요종목 {len(stocks_data.get('major', []))}개  "
              f"특징주 {len(stocks_data.get('featured', []))}개  "
              f"수급데이터 {len(stocks_data.get('investor_flow', []))}개")

        # 일본 / 중국 / 홍콩 — LSEG 구성종목 분석 (이미 LSEG 세션 열려 있음)
        print(f"\n[3/4-pre2] 일본·중국·홍콩 구성종목 분석 (LSEG)")
        from exe.collect_macro import lseg_open as _lseg_open, lseg_close as _lseg_close
        _lseg_open()
        try:
            overseas = fetch_asia_overseas_stocks(target_date, n_major=8, n_featured=6)
        finally:
            _lseg_close()
        if overseas:
            stocks_data["overseas_asia"] = overseas
            for mk, d in overseas.items():
                print(f"  {mk.upper()} {d['name']}: major {len(d['major'])} / "
                      f"featured {len(d['featured'])} / sectors {len(d['sectors'])}")

    elif session == "europe":
        print(f"\n[3/4-pre] 유럽 섹터 + 종목 수집")
        from exe.collect_stocks import fetch_europe_stocks
        from exe.collect_macro import lseg_open as _lseg_open, lseg_close as _lseg_close
        _lseg_open()
        try:
            stocks_data = fetch_europe_stocks(target_date)
        finally:
            _lseg_close()
        print(f"  섹터 {len(stocks_data.get('sectors', []))}개  "
              f"시총상위 {len(stocks_data.get('mktcap_top', []))}개  "
              f"거래대금상위 {len(stocks_data.get('tradeval_top', []))}개  "
              f"급증 {len(stocks_data.get('turnover_surge', []))}개")

    elif session == "us":
        print(f"\n[3/4-pre] 미국 섹터ETF + 종목 수집")
        from exe.collect_stocks import fetch_us_stocks
        stocks_data = fetch_us_stocks(target_date)
        print(f"  섹터ETF {len(stocks_data.get('sectors', []))}개  "
              f"시총상위 {len(stocks_data.get('mktcap_top', []))}개  "
              f"거래대금상위 {len(stocks_data.get('tradeval_top', []))}개  "
              f"급증 {len(stocks_data.get('turnover_surge', []))}개  "
              f"EPS변화 {len(stocks_data.get('eps_revision', []))}개")
        # US 세션: 당일 europe 종목 데이터를 DB에서 불러와 Europe 독립 섹션에 활용
        e_stocks = db.get_stocks_daily(target_date, "europe")
        if e_stocks:
            stocks_data["europe_stocks"] = e_stocks
            print(f"  유럽 종목 데이터 로드: 섹터 {len(e_stocks.get('sectors', []))}개  "
                  f"시총상위 {len(e_stocks.get('mktcap_top', []))}개  "
                  f"급증 {len(e_stocks.get('turnover_surge', []))}개")

        # US 세션: 당일 asia/europe 브리핑 요약을 DB에서 읽어 추가 컨텍스트로 전달
        prior_briefings = {}
        for prior_session in ("asia", "europe"):
            row = db.get_latest_briefing(prior_session)
            if row and row.get("date") == target_date:
                prior_briefings[prior_session] = row["content"][:400]
        if prior_briefings:
            stocks_data["prior_briefings"] = prior_briefings
            print(f"  이전 브리핑 컨텍스트: {list(prior_briefings.keys())}")

    # 종목 데이터 DB 저장 (prior_briefings 제외)
    if stocks_data:
        save_stocks = {k: v for k, v in stocks_data.items() if k != "prior_briefings"}
        n_saved = db.upsert_stocks_daily(target_date, session, save_stocks)
        print(f"  [stocks_daily] DB 저장: {n_saved}건")

    print(f"\n[3/4] LLM 브리핑 생성")
    from summarize.llm_briefing import generate_briefing
    content = generate_briefing(session=session, target_date=target_date, save=True,
                                stocks_data=stocks_data)
    print(content[:300] + "..." if len(content) > 300 else content)

    # Step 4: Slack 발송
    if skip_notify:
        print(f"\n[4/4] Slack 발송 (skip: --no-notify)")
        return

    print(f"\n[4/4] Slack 발송")
    from summarize.notify_slack import send_briefing
    latest = db.get_latest_briefing(session)
    if latest:
        send_briefing(briefing_id=latest["id"], session=session, date=target_date)

    print(f"\n✅ 완료: {session.upper()} 세션 파이프라인")


def run_global_pipeline(target_date: str,
                        skip_llm: bool = False, skip_notify: bool = False):
    """06:10 KST 통합 파이프라인 — Europe + US 데이터 수집/브리핑/Slack 단일 실행.

    중복 제거: LSEG 세션 1회, 매크로 1회, 경제지표 캘린더 1회.
    Europe 브리핑은 DB 저장만(Slack 미발송) → US 글로벌 통합 브리핑의
    prior_briefings 컨텍스트로 자동 흡수 → 최종 Slack 1건만 발송.
    """
    print(f"\n{'='*55}")
    print(f"  Global Market Watch  |  EUROPE+US 통합 파이프라인  |  {target_date}")
    print(f"{'='*55}")

    from exe.collect_macro import (
        load_yaml, lseg_open, lseg_close,
        collect_indices, collect_macro, DBManager
    )
    indices_cfg = load_yaml("indices.yaml")
    macro_cfg   = load_yaml("macro.yaml")
    db = DBManager()

    # Step 1: 데이터 수집 (Europe + US 지수, 매크로 — LSEG 단일 세션)
    print(f"\n[1/5] 데이터 수집 (Europe + US 지수, 매크로)")
    lseg_open()
    try:
        n_eu = collect_indices("europe", target_date, indices_cfg, db)
        print(f"  Europe 지수 upserted: {n_eu}건")
        n_us = collect_indices("us", target_date, indices_cfg, db)
        print(f"  US 지수 upserted:     {n_us}건")
        n_macro = collect_macro(target_date, macro_cfg, db)
        print(f"  매크로 upserted:      {n_macro}건")
    finally:
        lseg_close()

    # Step 2: 경제지표 + 어닝 캘린더 (1회)
    print(f"\n[2/5] 경제지표 + SPX 어닝 캘린더 수집")
    try:
        from exe.collect_econ_cal import collect_econ_calendar
        records = collect_econ_calendar(days_ahead=14)
        n = db.upsert_econ_event(records)
        print(f"  경제지표 upserted: {n}건")
    except Exception as _e:
        print(f"  [경제지표 수집 ERROR] {_e}")
    try:
        from exe.collect_earnings_cal import collect_earnings_calendar
        e_records = collect_earnings_calendar(days_ahead=14, filter_spx=True)
        n = db.upsert_earnings_event(e_records)
        print(f"  어닝 upserted: {n}건")
    except Exception as _e:
        print(f"  [어닝 수집 ERROR] {_e}")

    if skip_llm:
        print(f"\n[3-5/5] LLM 브리핑 + Slack (skip: --no-llm)")
        print(f"\n✅ 완료: EUROPE+US 데이터 수집")
        return

    # Step 3: 종목 데이터 수집 (Europe + US)
    print(f"\n[3/5] Europe + US 종목 데이터 수집")
    from exe.collect_stocks import fetch_europe_stocks, fetch_us_stocks

    lseg_open()
    try:
        europe_data = fetch_europe_stocks(target_date)
    finally:
        lseg_close()
    print(f"  Europe — 섹터 {len(europe_data.get('sectors', []))}개  "
          f"시총상위 {len(europe_data.get('mktcap_top', []))}개  "
          f"거래대금상위 {len(europe_data.get('tradeval_top', []))}개  "
          f"급증 {len(europe_data.get('turnover_surge', []))}개")
    db.upsert_stocks_daily(target_date, "europe", europe_data)

    us_data = fetch_us_stocks(target_date)
    print(f"  US     — 섹터ETF {len(us_data.get('sectors', []))}개  "
          f"시총상위 {len(us_data.get('mktcap_top', []))}개  "
          f"거래대금상위 {len(us_data.get('tradeval_top', []))}개  "
          f"급증 {len(us_data.get('turnover_surge', []))}개  "
          f"EPS변화 {len(us_data.get('eps_revision', []))}개")
    us_data["europe_stocks"] = europe_data
    db.upsert_stocks_daily(target_date, "us", us_data)

    # Step 4: 브리핑 생성 — Europe(컨텍스트용, DB만) → US(통합, Slack 대상)
    print(f"\n[4/5] LLM 브리핑 생성")
    from summarize.llm_briefing import generate_briefing

    print(f"  ▸ Europe 컨텍스트 브리핑 생성 (DB 저장만)")
    generate_briefing(session="europe", target_date=target_date,
                      save=True, stocks_data=europe_data)

    # 당일 asia/europe 브리핑을 US 통합 브리핑 컨텍스트로 주입
    prior_briefings = {}
    for prior_session in ("asia", "europe"):
        row = db.get_latest_briefing(prior_session)
        if row and row.get("date") == target_date:
            prior_briefings[prior_session] = row["content"][:400]
    if prior_briefings:
        us_data["prior_briefings"] = prior_briefings
        print(f"  ▸ 이전 브리핑 컨텍스트: {list(prior_briefings.keys())}")

    print(f"  ▸ US 글로벌 통합 브리핑 생성")
    us_content = generate_briefing(session="us", target_date=target_date,
                                   save=True, stocks_data=us_data)
    print(us_content[:300] + "..." if len(us_content) > 300 else us_content)

    # Step 5: Slack 발송 (US 통합 브리핑 1건만)
    if skip_notify:
        print(f"\n[5/5] Slack 발송 (skip: --no-notify)")
        print(f"\n✅ 완료: EUROPE+US 통합 파이프라인 (Slack 미발송)")
        return

    print(f"\n[5/5] Slack 발송 (US 통합 브리핑 1건)")
    from summarize.notify_slack import send_briefing
    latest = db.get_latest_briefing("us")
    if latest:
        send_briefing(briefing_id=latest["id"], session="us", date=target_date)

    print(f"\n✅ 완료: EUROPE+US 통합 파이프라인")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True,
                        choices=["asia", "europe", "us", "global", "all"],
                        help="global: Europe+US 통합 파이프라인 (06:10 스케줄)")
    parser.add_argument("--date", default=None,
                        help="기준일 YYYY-MM-DD (기본: 오늘)")
    parser.add_argument("--no-llm",    action="store_true")
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()

    target_date = args.date or _default_date(args.session)

    if args.session == "global":
        run_global_pipeline(
            target_date=target_date,
            skip_llm=args.no_llm,
            skip_notify=args.no_notify,
        )
        return

    if args.session == "all":
        # 호환 유지: asia 후 global 통합 실행
        run_session(session="asia", target_date=target_date,
                    skip_llm=args.no_llm, skip_notify=args.no_notify)
        run_global_pipeline(target_date=target_date,
                            skip_llm=args.no_llm, skip_notify=args.no_notify)
        return

    run_session(
        session=args.session,
        target_date=target_date,
        skip_llm=args.no_llm,
        skip_notify=args.no_notify,
    )


if __name__ == "__main__":
    main()
