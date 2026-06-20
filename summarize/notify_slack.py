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
import math

if hasattr(sys.stdout, 'buffer') and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import requests
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
sys.path.insert(0, str(BASE_DIR))

from db.db_manager import DBManager

WEBHOOK_URL     = os.getenv("SLACK_WEBHOOK_URL", "")
OPS_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL_MARKET_WATCH_OPS", "")

SESSION_EMOJI = {
    "asia":   "🌏",
    "europe": "🇪🇺",
    "us":     "🇺🇸",
}

BLOCK_MAX = 3000  # Slack section 블록 1개 한도
MAX_BLOCKS_PER_MSG = 45  # Slack 한 메시지 블록 제한(여유치)


def _text_to_blocks(text: str) -> list[dict]:
    """텍스트를 BLOCK_MAX 단위로 나눠 section 블록 리스트 반환 (단락 경계 유지)."""
    import re

    def make_block(t: str) -> dict:
        return {"type": "section", "text": {"type": "mrkdwn", "text": t.strip()}}

    if len(text) <= BLOCK_MAX:
        return [make_block(text)]

    paragraphs = re.split(r'\n{2,}', text)
    blocks, chunk = [], ""
    for para in paragraphs:
        # 단락 자체가 BLOCK_MAX를 넘는 경우 강제 분할
        para_parts = [para[i:i + BLOCK_MAX] for i in range(0, len(para), BLOCK_MAX)] or [""]
        for idx, part in enumerate(para_parts):
            addition = part + ("\n\n" if idx == len(para_parts) - 1 else "")
            if chunk and len(chunk) + len(addition) > BLOCK_MAX:
                blocks.append(make_block(chunk))
                chunk = addition
            else:
                chunk += addition
    if chunk.strip():
        blocks.append(make_block(chunk))
    return blocks


def _send_raw(text: str) -> bool:
    """Slack Incoming Webhook으로 텍스트 1개 메시지 발송."""
    if not WEBHOOK_URL:
        raise RuntimeError("SLACK_WEBHOOK_URL이 .env에 없습니다.")

    blocks = _text_to_blocks(text)
    for i in range(0, len(blocks), MAX_BLOCKS_PER_MSG):
        payload = {"blocks": blocks[i:i + MAX_BLOCKS_PER_MSG]}
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


def _split_by_section(text: str) -> list[str]:
    """--- 구분선 기준으로 섹션 분리. 빈 섹션 제거."""
    import re
    parts = re.split(r'\n{0,2}---\n{0,2}', text)
    return [p.strip() for p in parts if p.strip()]


# Slack 메시지 1개에 담을 권장 길이 (BLOCK_MAX=3000과 가독성 균형)
PART_TARGET_LEN = 3000


def _split_units(text: str) -> list[str]:
    """본문을 분할 후보 단위(섹션 → 단락)로 분해.

    `---` 우선 분리 후, 단일 섹션이 평균보다 훨씬 길면 빈줄(`\\n\\n`) 단위로
    추가 분해해 균형 분할이 가능하게 한다.
    """
    import re
    sections = [p.strip() for p in re.split(r'\n{0,2}---\n{0,2}', text) if p.strip()]
    if not sections:
        return [text.strip()] if text.strip() else []

    avg_len = sum(len(s) for s in sections) / len(sections)
    units: list[str] = []
    for s in sections:
        # 평균의 2배를 초과하는 큰 섹션만 단락 단위로 추가 분해
        if len(s) > avg_len * 2.0 and "\n\n" in s:
            paragraphs = [p.strip() for p in re.split(r'\n{2,}', s) if p.strip()]
            units.extend(paragraphs)
        else:
            units.append(s)
    return units


def _consolidate_parts(units: list[str], target_parts: int | None = None) -> list[str]:
    """
    분할 단위 리스트를 길이 기준 묶음으로 통합.

    Parameters
    ----------
    units        : _split_units()가 반환한 분할 후보 (섹션 + 단락 혼합)
    target_parts : 강제 파트 수. None이면 전체 길이를 기준으로
                   ceil(total_len / PART_TARGET_LEN) 만큼 자동 생성.
                   단위 수가 적으면 그만큼만 생성.
    """
    if not units:
        return []

    sep_len = 2  # "\n\n"
    total = sum(len(s) for s in units) + max(0, len(units) - 1) * sep_len

    if target_parts is None:
        target_parts = max(1, math.ceil(total / PART_TARGET_LEN))
    target_parts = max(1, min(target_parts, len(units)))

    if target_parts == 1:
        return ["\n\n".join(units)]

    target_len = total / target_parts
    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    for i, unit in enumerate(units):
        unit_len = len(unit)
        remaining_units = len(units) - i
        remaining_parts = target_parts - len(parts)
        # 끊을 조건: (a) 추가하면 목표보다 더 멀어지거나 (b) 이미 목표 초과
        would_be = current_len + (sep_len if current_len else 0) + unit_len
        should_break = (
            current
            and remaining_units > remaining_parts - 1
            and (
                current_len >= target_len
                or abs(would_be - target_len) > abs(current_len - target_len)
            )
        )
        if should_break:
            parts.append("\n\n".join(current))
            current = [unit]
            current_len = unit_len
        else:
            current.append(unit)
            current_len += unit_len + (sep_len if current_len else 0)
    if current:
        parts.append("\n\n".join(current))

    # 마지막 파트가 너무 짧으면(목표의 1/4 미만) 직전 파트에 흡수
    if len(parts) >= 2 and len(parts[-1]) < target_len * 0.25:
        last = parts.pop()
        parts[-1] = parts[-1] + "\n\n" + last
    return parts


def _markdown_to_slack(md: str) -> str:
    """Markdown → Slack mrkdwn 변환.
    - ## / ### 헤더  →  *bold*
    - **text**       →  *text*  (Slack은 ** 미지원)
    - |:---|---:| 구분자 행 제거 (테이블 정렬 노이즈 제거)
    """
    import re
    lines = []
    for line in md.splitlines():
        s = line.strip()
        # 테이블 구분자 행 (|:---|---:| 등) 제거
        if s.startswith("|") and s.endswith("|") and all(c in "|-: " for c in s):
            continue
        if line.startswith("### "):
            lines.append(f"*{line[4:].strip()}*")
        elif line.startswith("## "):
            lines.append(f"*{line[3:].strip()}*")
        else:
            # **bold** → *bold*
            line = re.sub(r'\*\*(.+?)\*\*', r'*\1*', line)
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

    emoji    = SESSION_EMOJI.get(session, "📊")
    header   = f"{emoji} *{date} {session.upper()} 시황 브리핑*\n{'─' * 40}"
    body     = _markdown_to_slack(content)
    units    = _split_units(body)
    parts    = _consolidate_parts(units) or [body]

    # 첫 파트와 헤더 합쳐서 메시지 1개로 보낼 수 있으면 합치고, 아니면 헤더 단독 발송
    first = parts[0]
    if len(header) + 2 + len(first) <= BLOCK_MAX * 2:
        messages = [f"{header}\n\n{first}"] + parts[1:]
    else:
        messages = [header] + parts

    for i, msg in enumerate(messages, 1):
        _send_raw(msg)
        print(f"[notify_slack] {i}/{len(messages)} 발송 ({len(msg):,}자)")

    if briefing_id:
        db.mark_notified(briefing_id)
    print(f"[notify_slack] id={briefing_id} 완료 (분할단위 {len(units)} → {len(parts)}파트, "
          f"메시지 {len(messages)}개) ✓")

    return True


def send_text(text: str) -> bool:
    """임의 텍스트 바로 발송."""
    return _send_raw(text)


def send_ops(text: str) -> bool:
    """운영 모니터링 채널(SLACK_WEBHOOK_URL_MARKET_WATCH_OPS)로 발송."""
    if not OPS_WEBHOOK_URL:
        print("[notify_slack] SLACK_WEBHOOK_URL_MARKET_WATCH_OPS 미설정 — ops 발송 스킵")
        return False
    try:
        resp = requests.post(
            OPS_WEBHOOK_URL,
            data=json.dumps({"text": text}),
            headers={"Content-Type": "application/json"},
            timeout=10,
            verify=False,
        )
        ok = resp.status_code == 200
        if not ok:
            print(f"[notify_slack] ops 발송 실패: {resp.status_code} {resp.text[:100]}")
        return ok
    except Exception as e:
        print(f"[notify_slack] ops 발송 오류: {e}")
        return False


if __name__ == "__main__":
    # 테스트 발송
    ok = send_text("✅ Global Market Watch Slack 연결 테스트")
    print("발송 성공" if ok else "발송 실패")
