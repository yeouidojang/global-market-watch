"""
마크다운 브리핑 → PDF 변환

markdown  : Markdown → HTML
weasyprint: HTML + CSS → PDF (A4 Portrait)

사용법:
    from summarize.generate_pdf import generate_pdf
    from pathlib import Path
    pdf_bytes = generate_pdf(content, "2026-06-22 Asia 시황 브리핑",
                             save_path=Path("logs/briefings_pdf/asia/2026-06-22.pdf"))
"""

import re
from pathlib import Path

# 폰트 의존 이모지 대신 CSS 스타일링한 ASCII span 사용 (렌더링 보장).
# 🚨+ → 빨간 bold (!) + 등락 부호
_ALERT = '<span style="color:#c0392b;font-weight:bold">(!)</span>'
_EMOJI_MAP = {
    '🚨+': f'{_ALERT}+',
    '🚨-': f'{_ALERT}-',
    '🚨':   _ALERT,
}
_EMOJI_RE = re.compile(r'[\U00010000-\U0010FFFF]')


def _sanitize_emoji(text: str) -> str:
    for src, dst in _EMOJI_MAP.items():
        text = text.replace(src, dst)
    return _EMOJI_RE.sub('', text)

BASE_DIR = Path(__file__).resolve().parent.parent

# ── 스타일시트 ──────────────────────────────────────────────────────────────
_CSS = """
/* Korean font: 서버에 Noto CJK 없으면 sans-serif fallback */
@font-face {
    font-family: 'NotoKR';
    src: local('Noto Sans CJK KR'), local('NanumGothic'), local('Malgun Gothic');
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: 'NotoKR', 'Noto Sans CJK KR', 'NanumGothic', 'Malgun Gothic',
                 'Noto Sans Symbols2', 'Noto Sans Symbols', 'Apple SD Gothic Neo', sans-serif;
    font-size: 8.5pt;
    line-height: 1.55;
    color: #1a1a2e;
    background: #fff;
}

/* ── 커버 헤더 ── */
.cover-header {
    background: linear-gradient(135deg, #0d1b2a 0%, #1b4f72 100%);
    color: #e8f4fd;
    padding: 14px 20px 12px;
    margin-bottom: 14px;
}
.cover-header .title   { font-size: 15pt; font-weight: 700; letter-spacing: -0.5px; }
.cover-header .subtitle{ font-size: 8.5pt; color: #a9cce3; margin-top: 3px; }

/* ── 헤딩 ── */
h1 {
    font-size: 13pt; color: #0d1b2a;
    border-bottom: 2px solid #1b4f72;
    padding-bottom: 5px; margin: 14px 0 7px;
}
h2 {
    font-size: 10.5pt; color: #1b4f72;
    border-left: 3px solid #2e86ab;
    padding-left: 8px; margin: 12px 0 5px;
}
h3 { font-size: 9.5pt; color: #2e4057; margin: 9px 0 4px; }

p  { margin: 3px 0 7px; }
hr { border: none; border-top: 1px solid #d5dce3; margin: 8px 0; }

/* ── 테이블 ── */
table {
    width: 100%;
    border-collapse: collapse;
    margin: 5px 0 11px;
    font-size: 7.8pt;
}
th {
    background: #1b4f72;
    color: #fff;
    padding: 4px 6px;
    font-weight: 600;
    white-space: nowrap;
    text-align: left;
}
td {
    padding: 3px 6px;
    border-bottom: 1px solid #e4ecf3;
    white-space: nowrap;
}
tr:nth-child(even) td { background: #f4f8fb; }

/* 숫자 컬럼(4번째 이후)은 우측 정렬 */
td:nth-child(n+4) { text-align: right; }
th:nth-child(n+4) { text-align: right; }

/* ── 코드 블록 ── */
code { background: #f0f4f8; padding: 1px 4px; border-radius: 3px; font-size: 7.8pt; }
pre  { background: #f0f4f8; padding: 8px 10px; border-radius: 4px; font-size: 7.5pt;
       overflow-x: auto; margin: 5px 0 10px; }

/* ── 강조 ── */
strong { font-weight: 700; }
em     { font-style: italic; color: #444; }

/* ── 리스트 ── */
ul, ol { margin: 3px 0 7px 16px; }
li     { margin: 2px 0; }

/* ── 페이지 설정 (A4 가로) ── */
@page {
    size: A4 portrait;
    margin: 14mm 12mm;
}
"""


def _build_html(body_html: str, title: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <title>{title}</title>
  <style>{_CSS}</style>
</head>
<body>
  <div class="cover-header">
    <div class="title">Global Market Watch</div>
    <div class="subtitle">{title}</div>
  </div>
  {body_html}
</body>
</html>"""


def generate_pdf(content: str, title: str, save_path: "Path | None" = None) -> bytes:
    """마크다운 브리핑 → PDF 바이너리 변환.

    Parameters
    ----------
    content   : 브리핑 마크다운 전문
    title     : 문서 제목 (예: "2026-06-22 Asia 시황 브리핑")
    save_path : 로컬 저장 경로 (None이면 저장 생략)

    Returns
    -------
    bytes  PDF 바이너리

    Raises
    ------
    ImportError  weasyprint 또는 markdown 미설치 시
    """
    import markdown as _md
    from weasyprint import HTML

    body_html = _md.markdown(
        _sanitize_emoji(content),
        extensions=["tables", "fenced_code", "nl2br"],
    )
    html_str  = _build_html(body_html, title)
    pdf_bytes = HTML(string=html_str, base_url=str(BASE_DIR)).write_pdf()

    if save_path:
        save_path = Path(save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(pdf_bytes)
        print(f"[generate_pdf] 저장: {save_path}  ({len(pdf_bytes):,} bytes)")

    return pdf_bytes
