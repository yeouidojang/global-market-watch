"""
ClickUp Docs 브리핑 발송

Slack과 병행 발송. Doc 1개에 일자별 Page를 누적하는 구조.

필요 환경변수 (.env):
  CLICKUP_API_TOKEN       — Personal token (pk_...)
  CLICKUP_WORKSPACE_ID    — Workspace ID (GET /api/v2/team 으로 확인)
  CLICKUP_BRIEFING_DOC_ID — 미리 생성한 "Global 시황 브리핑" Doc ID

사전 준비 (1회성):
  1. ClickUp → Settings → Apps → API Token 복사
  2. GET https://api.clickup.com/api/v2/team 으로 workspace_id 확인
  3. "Global 시황 브리핑" Doc 수동 생성 후 doc_id를 .env에 등록
  이후 매 실행마다 해당 Doc 안에 일자별 Page만 추가됨.
"""

import os
import sys
import io
from pathlib import Path
from dotenv import load_dotenv

if hasattr(sys.stdout, 'buffer') and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR.parent / ".env")

import requests

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN", "")
WORKSPACE_ID  = os.getenv("CLICKUP_WORKSPACE_ID", "")
DOC_ID        = os.getenv("CLICKUP_BRIEFING_DOC_ID", "")

SESSION_LABEL = {
    "asia":   "ASIA 시황",
    "europe": "EUROPE 시황",
    "us":     "US/Global 시황",
}


def send_briefing_to_docs(content: str, session: str = "", date: str = "") -> str | None:
    """브리핑 마크다운을 ClickUp Docs 페이지로 생성.

    Parameters
    ----------
    content : str  분할 전 원본 마크다운 전문 (Slack 파트 분할 전)
    session : str  asia / europe / us
    date    : str  YYYY-MM-DD

    Returns
    -------
    str | None  생성된 page_id, 실패 시 None (예외 격리 — 호출부에 전파 안 함)
    """
    if not all([CLICKUP_TOKEN, WORKSPACE_ID, DOC_ID]):
        missing = [k for k, v in {
            "CLICKUP_API_TOKEN":       CLICKUP_TOKEN,
            "CLICKUP_WORKSPACE_ID":    WORKSPACE_ID,
            "CLICKUP_BRIEFING_DOC_ID": DOC_ID,
        }.items() if not v]
        print(f"[notify_clickup] 환경변수 미설정: {missing} — 발송 건너뜀")
        return None

    label   = SESSION_LABEL.get(session, session.upper())
    title   = f"{date} {label} 브리핑"
    url     = (f"https://api.clickup.com/api/v3/workspaces/{WORKSPACE_ID}"
               f"/docs/{DOC_ID}/pages")
    headers = {
        "Authorization": CLICKUP_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {
        "name":           title,
        "content":        content,
        "content_format": "text/md",
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30, verify=False)
        resp.raise_for_status()
        page_id = resp.json().get("id", "")
        print(f"[notify_clickup] 페이지 생성 완료: {title} (page_id={page_id})")
        return page_id
    except requests.HTTPError:
        print(f"[notify_clickup] HTTP 오류 [{resp.status_code}]: {resp.text[:300]}")
        return None
    except Exception as e:
        print(f"[notify_clickup] 예외: {e}")
        return None
