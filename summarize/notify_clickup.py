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

import re
import requests

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN", "")
WORKSPACE_ID  = os.getenv("CLICKUP_WORKSPACE_ID", "")
DOC_ID        = os.getenv("CLICKUP_BRIEFING_DOC_ID", "")

SESSION_LABEL = {
    "asia": "ASIA 시황",
    "us":   "Global 시황",
}


def _normalize_tables(content: str) -> str:
    """마크다운 테이블 행 컬럼 수를 최댓값으로 통일.

    ClickUp API는 테이블 행 간 컬럼 수 불일치 시 400 반환.
    LLM이 헤더/구분선을 N컬럼으로 생성했으나 데이터 행에 컬럼을 추가하는 경우 발생.
    """
    lines = content.split("\n")
    result: list[str] = []
    i = 0
    while i < len(lines):
        # 연속된 테이블 행 수집
        if lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
            table: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                table.append(lines[i])
                i += 1
            # 각 행의 컬럼 수 계산 (양쪽 | 제외)
            col_counts = [row.count("|") - 1 for row in table]
            max_cols = max(col_counts)
            fixed: list[str] = []
            for row, cnt in zip(table, col_counts):
                if cnt < max_cols:
                    diff = max_cols - cnt
                    # 구분선 행 여부 판별 (|---|--- 패턴)
                    if re.search(r"\|[\s:]*-+[\s:]*\|", row):
                        row = row.rstrip() + " ---|" * diff
                    else:
                        row = row.rstrip() + " |" * diff
                fixed.append(row)
            result.extend(fixed)
        else:
            result.append(lines[i])
            i += 1
    return "\n".join(result)


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
        "content":        _normalize_tables(content),
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
