"""
Slack Webhook 알림 발송

사용법:
    from summarize.notify_slack import send_briefing
    send_briefing(briefing_id=1)       # DB에서 브리핑 불러와 발송
    send_briefing(text="직접 텍스트")  # 텍스트 직접 전달
"""

import os
import sys
import io
import json

if hasattr(sys.stdout, 'buffer') and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import requests
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR.parent / ".env")
sys.path.insert(0, str(BASE_DIR))

from db.db_manager import DBManager

WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

SESSION_EMOJI = {
    "asia":   "🌏",
    "europe": "🇪🇺",
    "us":     "🇺🇸",
}

MAX_SLACK_LEN = 3000  # Slack 블록 텍스트 한도


def _send_raw(text: str) -> bool:
    """Slack Incoming Webhook 발송."""
    if not WEBHOOK_URL:
        raise RuntimeError("SLACK_WEBHOOK_URL이 .env에 없습니다.")

    payload = {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": text[:MAX_SLACK_LEN],
                },
            }
        ]
    }

    resp = requests.post(
        WEBHOOK_URL,
        data=json.dumps(payload),
        headers={"Content-Type": "application/json"},
        timeout=10,
        verify=False,
    )

    if resp.status_code != 200:
        raise RuntimeError(f"Slack 발송 실패: {resp.status_code} {resp.text}")
    return True


def _markdown_to_slack(md: str) -> str:
    """기본 Markdown → Slack mrkdwn 변환."""
    lines = []
    for line in md.splitlines():
        if line.startswith("## "):
            lines.append(f"*{line[3:]}*")
        elif line.startswith("**") and line.endswith("**"):
            lines.append(line)  # bold 유지
        else:
            lines.append(line)
    return "\n".join(lines)


def send_briefing(briefing_id: int = None, text: str = None,
                  session: str = None, date: str = None) -> bool:
    """
    Parameters
    ----------
    briefing_id : int  DB briefings.id (지정 시 DB에서 로드)
    text        : str  직접 텍스트 전달 (briefing_id 없을 때)
    session     : str  헤더 표시용 (asia/europe/us)
    date        : str  헤더 표시용 날짜

    Returns
    -------
    bool  발송 성공 여부
    """
    db = DBManager()

    if briefing_id is not None:
        row = db.get_latest_briefing(session) if session else None
        if briefing_id:
            import sqlite3
            conn = sqlite3.connect(db.db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM briefings WHERE id=?", (briefing_id,)).fetchone()
            conn.close()
            if row:
                row = dict(row)
        if not row:
            raise ValueError(f"briefing id={briefing_id} 없음")
        content = row["content"]
        session = session or row.get("session", "")
        date    = date or row.get("date", "")
    elif text:
        content = text
    else:
        raise ValueError("briefing_id 또는 text 중 하나 필요")

    emoji  = SESSION_EMOJI.get(session, "📊")
    header = f"{emoji} *{date} {session.upper()} 시황 브리핑*\n{'─' * 40}\n"
    body   = _markdown_to_slack(content)
    full   = header + body

    ok = _send_raw(full)

    if ok and briefing_id:
        db.mark_notified(briefing_id)
        print(f"[notify_slack] id={briefing_id} Slack 발송 완료 ✓")

    return ok


def send_text(text: str) -> bool:
    """임의 텍스트 바로 발송."""
    return _send_raw(text)


if __name__ == "__main__":
    # 테스트 발송
    ok = send_text("✅ Global Market Watch Slack 연결 테스트")
    print("발송 성공" if ok else "발송 실패")
