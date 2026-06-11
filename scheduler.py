"""
APScheduler 기반 스케줄러 — 시간대별 자동 실행

KST 기준:
  16:10 → asia   세션 (Slack 발송)
  06:10 → europe 세션 (조용히, 컨텍스트 생성용) + us 세션 (글로벌 통합 Slack 발송)

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
load_dotenv(BASE_DIR.parent / ".env")
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
    try:
        from run_session import run_session
        run_session(session="asia", target_date=today)
    except Exception as e:
        log.error(f"[asia] 오류: {e}", exc_info=True)
        try:
            from summarize.notify_slack import send_text
            send_text(f"⚠️ *Market Watch 오류* [ASIA]\n{str(e)[:200]}")
        except Exception:
            pass
    log.info(f"▶ ASIA 세션 완료")


def _run_global():
    """06:10 KST — Europe + US 통합 파이프라인 단일 호출.

    06:10 KST 실행 시점에서 US/Europe 시장 마감일은 전 영업일이므로
    _prev_weekday_date()로 올바른 target_date를 계산한다.
    """
    target = _prev_weekday_date()
    log.info(f"▶ GLOBAL(Europe+US) 통합 파이프라인 시작 ({target})")
    try:
        from run_session import run_global_pipeline
        run_global_pipeline(target_date=target)
    except Exception as e:
        log.error(f"[global] 오류: {e}", exc_info=True)
        try:
            from summarize.notify_slack import send_text
            send_text(f"⚠️ *Market Watch 오류* [GLOBAL]\n{str(e)[:200]}")
        except Exception:
            pass
    log.info(f"▶ GLOBAL 통합 파이프라인 완료")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true",
                        help="즉시 1회 실행 후 종료")
    parser.add_argument("--session", default=None,
                        choices=[None, "asia", "global", "all"],
                        help="--test 와 함께 사용 시 특정 세션만")
    args = parser.parse_args()

    if args.test:
        target = args.session or "all"
        if target == "asia":
            _run_asia()
        elif target == "global":
            _run_global()
        else:  # all
            _run_asia()
            _run_global()
        return

    scheduler = BlockingScheduler(timezone=TIMEZONE)

    # 아시아 마감: 16:10 KST
    scheduler.add_job(
        _run_asia, CronTrigger(hour=16, minute=10, timezone=TIMEZONE),
        id="asia_close",
        name="아시아 시장 마감",
        misfire_grace_time=300,
    )

    # 유럽+미국 통합: 06:10 KST (단일 파이프라인 → Slack 1건)
    scheduler.add_job(
        _run_global, CronTrigger(hour=6, minute=10, timezone=TIMEZONE),
        id="global_close",
        name="유럽+미국 통합 파이프라인",
        misfire_grace_time=600,
    )

    log.info("=" * 55)
    log.info("  Global Market Watch Scheduler 시작")
    log.info(f"  Timezone: {TIMEZONE}")
    log.info("  트리거: 16:10(아시아) / 06:10(유럽+미국 통합)")
    log.info("=" * 55)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("스케줄러 종료")


if __name__ == "__main__":
    main()
