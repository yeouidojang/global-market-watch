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
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

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

    # Step 2: 경제지표 캘린더 (US 세션에서만 or 아시아에서 한 번)
    if session in ("us", "asia"):
        print(f"\n[2/4] 경제지표 캘린더 수집")
        from exe.collect_econ_cal import collect_econ_calendar
        records = collect_econ_calendar(days_ahead=5)
        n = db.upsert_econ_event(records)
        print(f"  경제지표 upserted: {n}건")
    else:
        print(f"\n[2/4] 경제지표 캘린더 수집 (skip: {session})")

    # Step 3: LLM 브리핑
    if skip_llm:
        print(f"\n[3/4] LLM 브리핑 (skip: --no-llm)")
        return

    # 한국 세션이면 주요 종목 / 특징주 수집
    stocks_data = {}
    if session == "asia":
        print(f"\n[3/4-pre] KOSPI/KOSDAQ 종목 수집")
        from exe.collect_stocks import fetch_top_stocks
        stocks_data = fetch_top_stocks(target_date)
        print(f"  주요종목 {len(stocks_data.get('major', []))}개  특징주 {len(stocks_data.get('featured', []))}개")

    elif session == "europe":
        print(f"\n[3/4-pre] 유럽 섹터 + 종목 수집")
        from exe.collect_stocks import fetch_europe_stocks
        stocks_data = fetch_europe_stocks(target_date)
        print(f"  섹터 {len(stocks_data.get('sectors', []))}개  종목 {len(stocks_data.get('top_stocks', []))}개")

    elif session == "us":
        print(f"\n[3/4-pre] 미국 섹터ETF + 종목 수집")
        from exe.collect_stocks import fetch_us_stocks
        stocks_data = fetch_us_stocks(target_date)
        print(f"  섹터ETF {len(stocks_data.get('sectors', []))}개  "
              f"시총상위 {len(stocks_data.get('mktcap_top', []))}개  "
              f"거래대금상위 {len(stocks_data.get('tradeval_top', []))}개  "
              f"급증 {len(stocks_data.get('turnover_surge', []))}개  "
              f"EPS변화 {len(stocks_data.get('eps_revision', []))}개")
        # US 세션: 당일 asia/europe 브리핑 요약을 DB에서 읽어 추가 컨텍스트로 전달
        prior_briefings = {}
        for prior_session in ("asia", "europe"):
            row = db.get_latest_briefing(prior_session)
            if row and row.get("date") == target_date:
                # 브리핑 첫 300자 (핵심 요약 부분)
                prior_briefings[prior_session] = row["content"][:400]
        if prior_briefings:
            stocks_data["prior_briefings"] = prior_briefings
            print(f"  이전 브리핑 컨텍스트: {list(prior_briefings.keys())}")

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True,
                        choices=["asia", "europe", "us", "all"])
    parser.add_argument("--date", default=None,
                        help="기준일 YYYY-MM-DD (기본: 오늘)")
    parser.add_argument("--no-llm",    action="store_true")
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()

    target_date = args.date or date.today().strftime("%Y-%m-%d")
    sessions    = ["asia", "europe", "us"] if args.session == "all" else [args.session]

    for s in sessions:
        run_session(
            session=s,
            target_date=target_date,
            skip_llm=args.no_llm,
            skip_notify=args.no_notify,
        )


if __name__ == "__main__":
    main()
