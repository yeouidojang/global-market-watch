"""
Slack 파일 업로드 유틸리티
  · slack_sdk WebClient 사용 (Bot Token 필요)
  · 환경변수: SLACK_BOT_TOKEN, SLACK_BREADTH_CHANNEL
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path("/home/quant/global-market-watch/.env"))

BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
CHANNEL   = os.getenv("SLACK_BREADTH_CHANNEL", "")


def upload_file(
    file_path: str | Path,
    title: str = "",
    comment: str = "",
    channel: str = "",
) -> bool:
    """
    Slack에 파일 업로드.

    Parameters
    ----------
    file_path : 업로드할 파일 경로
    title     : Slack에 표시될 파일 제목
    comment   : 파일과 함께 보낼 메시지
    channel   : 채널 ID 또는 이름 (기본: SLACK_BREADTH_CHANNEL)

    Returns
    -------
    bool : 성공 여부
    """
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    if not BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN이 .env에 없습니다.")

    ch = channel or CHANNEL
    if not ch:
        raise RuntimeError("SLACK_BREADTH_CHANNEL이 .env에 없습니다.")

    fp = Path(file_path)
    if not fp.exists():
        raise FileNotFoundError(f"파일 없음: {fp}")

    client = WebClient(token=BOT_TOKEN)

    try:
        resp = client.files_upload_v2(
            channel=ch,
            file=str(fp),
            filename=fp.name,
            title=title or fp.name,
            initial_comment=comment,
        )
        ok = resp.get("ok", False)
        if ok:
            print(f"[Slack] 업로드 완료: {fp.name} → #{ch}")
        else:
            print(f"[Slack] 업로드 실패: {resp}")
        return ok

    except SlackApiError as e:
        print(f"[Slack] API 오류: {e.response['error']}")
        raise
