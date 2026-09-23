"""
APScheduler 기반 스케줄러 — 시간대별 자동 실행

KST 기준:
  16:20 → asia      세션 (Slack 발송)
  06:10 → europe    세션 (조용히, 컨텍스트 생성용) + us 세션 (글로벌 통합 Slack 발송)
  07:00 → US Breadth 분석 (S&P500 / NASDAQ-100 지표 → Excel → Slack)
  07:30 → S&P500 스크리닝 (거래대금 급증 + 모멘텀 + EPS 상향 → Excel → Slack)
  17:00 → KR Breadth 분석 (KOSPI200 / KOSDAQ150 지표 → Excel → Slack)

실행:
    python scheduler.py            # 포어그라운드 상시 실행
    python scheduler.py --test     # 즉시 1회 실행 후 종료 (테스트용)
"""

import sys
import logging
import argparse
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
sys.path.insert(0, str(BASE_DIR))

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("market_scheduler")

TIMEZONE = "Asia/Seoul"


def _prev_weekday_date() -> str:
    """오늘 KST 기준 직전 영업일 (토→금, 일→금, 월→금)."""
    d = date.today() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _run_asia():
    today = date.today().strftime("%Y-%m-%d")
    log.info(f"▶ ASIA 세션 시작 ({today})")
    from run_session import run_session, _OpsTracker
    ops = _OpsTracker(session="asia", target_date=today)
    try:
        run_session(session="asia", target_date=today, ops=ops)
        ops.send(ops.format_success())
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log.error(f"[asia] 오류: {e}", exc_info=True)
        ops.err("예외 발생", f"{type(e).__name__}: {str(e)[:150]}")
        ops.send(ops.format_failure(e, tb))
    log.info(f"▶ ASIA 세션 완료")


def _run_us_breadth():
    """07:00 KST — US Market Breadth 분석 (S&P500 / NASDAQ-100 구성종목 지표).

    global 파이프라인(06:10 KST)이 DB를 업데이트한 뒤 실행.
    출력: /home/quant/us-market-analysis/output/us_breadth_YYYYMMDD.xlsx
    """
    import subprocess
    from pathlib import Path

    script = Path("/home/quant/global-market-watch/us_breadth/run_breadth.py")
    python = Path("/home/quant/global-market-watch/.venv/bin/python")

    log.info("▶ US Breadth 분석 시작")
    try:
        result = subprocess.run(
            [str(python), str(script)],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            log.info(f"▶ US Breadth 분석 완료\n{result.stdout.strip()}")
        else:
            log.error(f"▶ US Breadth 분석 오류 (rc={result.returncode})\n{result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        log.error("▶ US Breadth 분석 타임아웃 (600s)")
    except Exception as e:
        log.error(f"▶ US Breadth 분석 예외: {e}", exc_info=True)


def _run_kr_breadth():
    """17:00 KST — KR Market Breadth 분석 (KOSPI200 / KOSDAQ150 구성종목 지표).

    asia 파이프라인(16:20 KST)이 DB를 업데이트한 뒤 실행.
    출력: /home/quant/kr-market-analysis/output/kr_breadth_YYYYMMDD.xlsx
    """
    import subprocess
    from pathlib import Path

    script = Path("/home/quant/global-market-watch/kr_breadth/run_kr_breadth.py")
    python = Path("/home/quant/global-market-watch/.venv/bin/python")

    log.info("▶ KR Breadth 분석 시작")
    try:
        result = subprocess.run(
            [str(python), str(script)],
            capture_output=True, text=True, timeout=900,
        )
        if result.returncode == 0:
            log.info(f"▶ KR Breadth 분석 완료\n{result.stdout.strip()}")
        else:
            log.error(f"▶ KR Breadth 분석 오류 (rc={result.returncode})\n{result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        log.error("▶ KR Breadth 분석 타임아웃 (900s)")
    except Exception as e:
        log.error(f"▶ KR Breadth 분석 예외: {e}", exc_info=True)


def _run_investment_points():
    """08:00 KST — S&P500 스크리닝 종목 투자포인트 자동 조사.

    sp500_screen(07:30 KST) 완료 후 실행.
    Claude API + web_search 로 EPS 3달 상향 종목 투자포인트 조사 → Excel → Slack.
    출력: /home/quant/us-market-analysis/output/sp500_investment_points_YYYYMMDD.xlsx
    """
    import subprocess

    script = Path("/home/quant/global-market-watch/us_breadth/run_investment_points.py")
    python = Path("/home/quant/global-market-watch/.venv/bin/python")

    log.info("▶ S&P500 투자포인트 조사 시작")
    try:
        result = subprocess.run(
            [str(python), str(script)],
            capture_output=True, text=True, timeout=900,
        )
        if result.returncode == 0:
            log.info(f"▶ S&P500 투자포인트 완료\n{result.stdout.strip()}")
        else:
            log.error(f"▶ S&P500 투자포인트 오류 (rc={result.returncode})\n{result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        log.error("▶ S&P500 투자포인트 타임아웃 (900s)")
    except Exception as e:
        log.error(f"▶ S&P500 투자포인트 예외: {e}", exc_info=True)


def _run_sp500_screen():
    """07:30 KST — S&P 500 거래대금 급증 + 모멘텀 + EPS 상향 스크리닝.

    US Breadth 분석(07:00 KST) 완료 후 실행.
    출력: /home/quant/us-market-analysis/output/sp500_screen_YYYYMMDD.xlsx
    """
    import subprocess

    script = Path("/home/quant/global-market-watch/us_breadth/run_sp500_screen.py")
    python = Path("/home/quant/global-market-watch/.venv/bin/python")

    log.info("▶ S&P500 스크리닝 시작")
    try:
        result = subprocess.run(
            [str(python), str(script)],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            log.info(f"▶ S&P500 스크리닝 완료\n{result.stdout.strip()}")
        else:
            log.error(f"▶ S&P500 스크리닝 오류 (rc={result.returncode})\n{result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        log.error("▶ S&P500 스크리닝 타임아웃 (600s)")
    except Exception as e:
        log.error(f"▶ S&P500 스크리닝 예외: {e}", exc_info=True)


def _run_global():
    """06:10 KST — Europe + US 통합 파이프라인 단일 호출.

    06:10 KST 실행 시점에서 US/Europe 시장 마감일은 전 영업일이므로
    _prev_weekday_date()로 올바른 target_date를 계산한다.
    """
    target = _prev_weekday_date()
    log.info(f"▶ GLOBAL(Europe+US) 통합 파이프라인 시작 ({target})")
    from run_session import run_global_pipeline, _OpsTracker
    ops = _OpsTracker(session="global", target_date=target)
    try:
        run_global_pipeline(target_date=target, ops=ops)
        ops.send(ops.format_success())
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log.error(f"[global] 오류: {e}", exc_info=True)
        ops.err("예외 발생", f"{type(e).__name__}: {str(e)[:150]}")
        ops.send(ops.format_failure(e, tb))
    log.info(f"▶ GLOBAL 통합 파이프라인 완료")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true",
                        help="즉시 1회 실행 후 종료")
    parser.add_argument("--session", default=None,
                        choices=[None, "asia", "global", "us_breadth", "kr_breadth",
                                 "sp500_screen", "investment_points", "all"],
                        help="--test 와 함께 사용 시 특정 세션만")
    args = parser.parse_args()

    if args.test:
        target = args.session or "all"
        if target == "asia":
            _run_asia()
        elif target == "global":
            _run_global()
        elif target == "us_breadth":
            _run_us_breadth()
        elif target == "kr_breadth":
            _run_kr_breadth()
        elif target == "sp500_screen":
            _run_sp500_screen()
        elif target == "investment_points":
            _run_investment_points()
        else:  # all
            _run_asia()
            _run_global()
            _run_us_breadth()
            _run_sp500_screen()
            _run_investment_points()
            _run_kr_breadth()
        return

    scheduler = BlockingScheduler(timezone=TIMEZONE)

    # 아시아 마감: 월~금 16:20 KST (KOSPI/KOSDAQ 거래일 기준)
    scheduler.add_job(
        _run_asia, CronTrigger(hour=16, minute=20, day_of_week="mon-fri", timezone=TIMEZONE),
        id="asia_close",
        name="아시아 시장 마감",
        misfire_grace_time=300,
    )

    # 유럽+미국 통합: 화~토 06:10 KST (미국 시장 월~금 마감 다음 날 아침)
    scheduler.add_job(
        _run_global, CronTrigger(hour=6, minute=10, day_of_week="tue-sat", timezone=TIMEZONE),
        id="global_close",
        name="유럽+미국 통합 파이프라인",
        misfire_grace_time=600,
    )

    # US Breadth 분석: 화~토 06:30 KST (global 파이프라인 완료 후)
    scheduler.add_job(
        _run_us_breadth, CronTrigger(hour=6, minute=30, day_of_week="tue-sat", timezone=TIMEZONE),
        id="us_breadth",
        name="US Market Breadth 분석",
        misfire_grace_time=600,
    )

    # S&P500 스크리닝: 화~토 06:50 KST (US Breadth 완료 후)
    scheduler.add_job(
        _run_sp500_screen, CronTrigger(hour=6, minute=50, day_of_week="tue-sat", timezone=TIMEZONE),
        id="sp500_screen",
        name="S&P500 거래대금+모멘텀+EPS 스크리닝",
        misfire_grace_time=600,
    )

    # S&P500 투자포인트: 화~토 07:10 KST (sp500_screen 완료 후)
    scheduler.add_job(
        _run_investment_points, CronTrigger(hour=7, minute=10, day_of_week="tue-sat", timezone=TIMEZONE),
        id="investment_points",
        name="S&P500 투자포인트 자동 조사",
        misfire_grace_time=600,
    )

    # KR Breadth 분석: 월~금 17:00 KST (asia 파이프라인 완료 후)
    scheduler.add_job(
        _run_kr_breadth, CronTrigger(hour=17, minute=0, day_of_week="mon-fri", timezone=TIMEZONE),
        id="kr_breadth",
        name="KR Market Breadth 분석",
        misfire_grace_time=600,
    )

    log.info("=" * 55)
    log.info("  Global Market Watch Scheduler 시작")
    log.info(f"  Timezone: {TIMEZONE}")
    log.info("  트리거: 월~금 16:20 KST(아시아) / 화~토 06:10 KST(유럽+미국 통합)")
    log.info("          화~토 06:30 KST(US Breadth) / 화~토 06:50 KST(S&P500 스크리닝)")
    log.info("          화~토 07:10 KST(S&P500 투자포인트) / 월~금 17:00 KST(KR Breadth)")
    log.info("=" * 55)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("스케줄러 종료")


if __name__ == "__main__":
    main()
