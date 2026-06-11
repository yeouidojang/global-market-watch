"""
APScheduler 기반 스케줄러 — 시간대별 자동 실행

KST 기준:
  15:40 → asia  세션
  01:40 → europe 세션
  06:10 → us    세션 (+ 글로벌 통합 브리핑)

실행:
    python scheduler.py            # 포어그라운드 상시 실행
    python scheduler.py --test     # 즉시 1회 실행 후 종료 (테스트용)
"""

import sys
import logging
import argparse
from datetime import date
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


def job(session: str):
    today = date.today().strftime("%Y-%m-%d")
    log.info(f"▶ 세션 시작: {session.upper()} ({today})")
    try:
        from run_session import run_session
        run_session(session=session, target_date=today)
    except Exception as e:
        log.error(f"[{session}] 오류: {e}", exc_info=True)
        # 오류 발생 시 Slack 에러 알림
        try:
            from summarize.notify_slack import send_text
            send_text(f"⚠️ *Market Watch 오류* [{session.upper()}]\n{str(e)[:200]}")
        except Exception:
            pass
    log.info(f"▶ 세션 완료: {session.upper()}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true",
                        help="즉시 전체 세션 1회 실행 후 종료")
    parser.add_argument("--session", default=None,
                        help="--test 와 함께 사용 시 특정 세션만")
    args = parser.parse_args()

    if args.test:
        sessions = [args.session] if args.session else ["asia", "europe", "us"]
        for s in sessions:
            job(s)
        return

    scheduler = BlockingScheduler(timezone=TIMEZONE)

    # 아시아 마감: 15:40 KST
    scheduler.add_job(
        job, CronTrigger(hour=15, minute=40, timezone=TIMEZONE),
        args=["asia"],
        id="asia_close",
        name="아시아 시장 마감",
        misfire_grace_time=300,  # 5분 내 누락 허용
    )

    # 유럽 마감: 01:40 KST (익일)
    scheduler.add_job(
        job, CronTrigger(hour=1, minute=40, timezone=TIMEZONE),
        args=["europe"],
        id="europe_close",
        name="유럽 시장 마감",
        misfire_grace_time=300,
    )

    # 미국 마감: 06:10 KST
    scheduler.add_job(
        job, CronTrigger(hour=6, minute=10, timezone=TIMEZONE),
        args=["us"],
        id="us_close",
        name="미국 시장 마감",
        misfire_grace_time=300,
    )

    log.info("=" * 55)
    log.info("  Global Market Watch Scheduler 시작")
    log.info(f"  Timezone: {TIMEZONE}")
    log.info("  트리거: 15:40(아시아) / 01:40(유럽) / 06:10(미국)")
    log.info("=" * 55)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("스케줄러 종료")


if __name__ == "__main__":
    main()
