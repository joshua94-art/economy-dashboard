#!/usr/bin/env python3
"""
한국경제신문 PDF 분석 스크립트

사전 준비 (최초 1회):
1. Google Cloud Console → 프로젝트 생성 → Drive API 활성화
2. IAM → 서비스 계정 생성 → JSON 키 다운로드
3. 구글 드라이브 'Joshua 증권' 폴더를 서비스 계정 이메일과 공유 (뷰어)
4. 'Joshua 증권/리포트 출력' 폴더는 편집자 권한으로 공유
5. JSON 키 전체 내용을 GitHub 시크릿 GOOGLE_CREDENTIALS 로 등록

핵심 주의사항:
- 서비스 계정은 자신의 빈 Drive를 가짐. 'root' 기준 검색은 공유 폴더를 찾지 못함.
- 모든 API 호출에 supportsAllDrives=True, includeItemsFromAllDrives=True 필요.
- 최상위 폴더("Joshua 증권") 탐색 시 parent 조건 없이 이름만으로 검색.
"""

import io
import json
import os
import tempfile
from datetime import datetime

import anthropic
import pdfplumber
import pytz
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive"]
REPORT_DIR = "data/reports"
CATEGORIES = ["경제일반", "산업/기업", "국제", "정치/정책", "금융/증권", "부동산"]

# 공통 파라미터: 서비스 계정이 공유 드라이브까지 검색하도록
_DRIVE_PARAMS = dict(includeItemsFromAllDrives=True, supportsAllDrives=True)


# ── Google Drive 유틸 ──────────────────────────────────────────────────────────

def get_drive_service():
    info = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def find_folder(service, name: str, parent_id: str | None = None) -> str | None:
    """
    폴더를 이름으로 검색하여 ID 반환.
    parent_id=None 이면 전체 Drive에서 검색 (최상위 공유 폴더 탐색용).
    """
    conditions = [
        f"name='{name}'",
        "mimeType='application/vnd.google-apps.folder'",
        "trashed=false",
    ]
    if parent_id:
        conditions.append(f"'{parent_id}' in parents")

    print(f"  폴더 검색: '{name}'" + (f" (parent={parent_id})" if parent_id else " (전체 Drive)"))
    res = service.files().list(
        q=" and ".join(conditions),
        fields="files(id,name,parents)",
        **_DRIVE_PARAMS,
    ).execute()
    files = res.get("files", [])
    if files:
        print(f"    → 발견: {files[0]['name']} (id={files[0]['id']})")
        return files[0]["id"]
    print(f"    → 없음")
    return None


def list_pdfs(service, folder_id: str, date_str: str) -> list[dict]:
    """date_str(YYYYMMDD)을 이름에 포함하는 PDF 목록 반환"""
    q = (
        f"name contains '{date_str}'"
        f" and mimeType='application/pdf'"
        f" and '{folder_id}' in parents"
        f" and trashed=false"
    )
    res = service.files().list(
        q=q,
        fields="files(id,name)",
        orderBy="name",
        **_DRIVE_PARAMS,
    ).execute()
    return res.get("files", [])


def download_file(service, file_id: str) -> bytes:
    req = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return buf.read()


def upload_html(service, folder_id: str, html: str, filename: str) -> None:
    """HTML 파일을 드라이브에 업로드 (기존 파일 덮어쓰기)"""
    q = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
    existing = service.files().list(
        q=q, fields="files(id)", **_DRIVE_PARAMS
    ).execute().get("files", [])

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", delete=False, encoding="utf-8"
    ) as f:
        f.write(html)
        tmp = f.name

    media = MediaFileUpload(tmp, mimetype="text/html")
    try:
        if existing:
            service.files().update(
                fileId=existing[0]["id"], media_body=media, supportsAllDrives=True
            ).execute()
        else:
            service.files().create(
                body={"name": filename, "parents": [folder_id]},
                media_body=media,
                supportsAllDrives=True,
            ).execute()
        print(f"  드라이브 업로드: {filename}")
    finally:
        os.unlink(tmp)


# ── PDF → 텍스트 ───────────────────────────────────────────────────────────────

def extract_text(pdf_bytes: bytes, filename: str) -> str:
    parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            text = (page.extract_text() or "").strip()
            if text:
                parts.append(f"[{filename} / {i+1}p]\n{text}")
    return "\n\n".join(parts)


# ── Claude 분석 ───────────────────────────────────────────────────────────────

def analyze(text: str, date_display: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    template = json.dumps(
        {c: [{"title": "...", "summary": "...", "keywords": ["..."]}] for c in CATEGORIES},
        ensure_ascii=False,
    )

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": "당신은 한국 경제 전문 에디터입니다. 신문 텍스트를 읽고 주요 기사를 정확히 분류·요약합니다.",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    f"다음은 한국경제신문 {date_display}자 지면 텍스트입니다.\n"
                    f"카테고리: {', '.join(CATEGORIES)}\n\n"
                    "주요 기사를 분류·요약하여 아래 형식의 JSON만 출력하세요 (마크다운 없이).\n"
                    "각 카테고리 최대 5건, 중요도 순, 기사 없으면 빈 배열.\n"
                    f"형식: {template}\n\n"
                    f"[신문 텍스트]\n{text[:40000]}"
                ),
            }
        ],
    )

    raw = resp.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ── HTML 리포트 생성 ───────────────────────────────────────────────────────────

def build_html(categories: dict, date_display: str) -> str:
    ICONS = {
        "경제일반": "📊", "산업/기업": "🏭", "국제": "🌐",
        "정치/정책": "🏛️", "금융/증권": "💹", "부동산": "🏢",
    }
    sections = ""
    for cat, articles in categories.items():
        if not articles:
            continue
        rows = "".join(
            f"""<div style="background:#f6f8fa;border-radius:8px;padding:14px 18px;margin-bottom:10px">
              <div style="font-weight:700;margin-bottom:6px">{a.get('title','')}</div>
              <div style="font-size:.88rem;color:#57606a;line-height:1.7">{a.get('summary','')}</div>
              <div style="margin-top:8px">{''.join(
                  f'<span style="background:#ddf4ff;color:#0969da;border-radius:4px;padding:1px 8px;font-size:.72rem;margin-right:4px">{k}</span>'
                  for k in a.get('keywords', [])
              )}</div>
            </div>"""
            for a in articles
        )
        sections += f"""
        <div style="margin-bottom:28px">
          <h2 style="font-size:1rem;color:#cf222e;border-left:4px solid #cf222e;padding-left:10px;margin-bottom:12px">
            {ICONS.get(cat,'')} {cat}
          </h2>{rows}
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8">
<title>한경 리포트 {date_display}</title>
<style>body{{font-family:'Malgun Gothic',sans-serif;max-width:900px;margin:40px auto;padding:0 20px;color:#1f2328;line-height:1.7}}
h1{{color:#0969da;border-bottom:2px solid #0969da;padding-bottom:8px}}</style>
</head><body>
<h1>📰 한국경제신문 리포트</h1>
<p style="color:#57606a">{date_display} · Generated by Claude AI</p>
{sections}
</body></html>"""


# ── 인덱스 관리 ───────────────────────────────────────────────────────────────

def update_index(date_display: str) -> None:
    path = f"{REPORT_DIR}/index.json"
    idx = {"dates": []}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            idx = json.load(f)
    if date_display not in idx["dates"]:
        idx["dates"].insert(0, date_display)
    idx["dates"] = idx["dates"][:60]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    kst = pytz.timezone("Asia/Seoul")
    now = datetime.now(kst)
    date_str = now.strftime("%Y%m%d")        # 20260620
    date_display = now.strftime("%Y-%m-%d")  # 2026-06-20
    month_folder = now.strftime("%Y-%m")     # 2026-06

    print(f"[{date_display}] 한경 PDF 분석 시작")

    service = get_drive_service()

    # ── 폴더 탐색 ──
    # 1단계: 최상위 "Joshua 증권" — parent 없이 전체 Drive 검색 (공유된 폴더)
    root_id = find_folder(service, "Joshua 증권")
    if not root_id:
        raise RuntimeError(
            "'Joshua 증권' 폴더를 찾을 수 없습니다.\n"
            "서비스 계정 이메일로 해당 폴더를 공유했는지 확인하세요."
        )

    # 2단계: 하위 폴더들은 parent_id 지정하여 검색
    hk_id = find_folder(service, "한경 PDF", root_id)
    if not hk_id:
        raise RuntimeError("'한경 PDF' 폴더를 찾을 수 없습니다.")

    month_id = find_folder(service, month_folder, hk_id)
    if not month_id:
        print(f"'{month_folder}' 폴더 없음. 종료.")
        return

    # ── PDF 목록 ──
    pdfs = list_pdfs(service, month_id, date_str)
    if not pdfs:
        print(f"  {date_str} 날짜 PDF 없음. 종료.")
        return
    print(f"  {len(pdfs)}개 PDF 발견")

    # ── 텍스트 추출 ──
    texts = []
    for item in pdfs:
        print(f"  다운로드: {item['name']}")
        data = download_file(service, item["id"])
        t = extract_text(data, item["name"])
        if t:
            texts.append(t)
        else:
            print(f"    ※ 텍스트 없음 (이미지 기반 페이지일 수 있음)")

    combined = "\n\n".join(texts)
    print(f"  총 {len(combined):,}자 추출")
    if not combined.strip():
        print("  텍스트 추출 실패. 종료.")
        return

    # ── Claude 분석 ──
    print("  Claude 분석 중...")
    categories = analyze(combined, date_display)

    # ── 로컬 저장 ──
    os.makedirs(REPORT_DIR, exist_ok=True)
    report = {
        "date": date_display,
        "generated_at": now.strftime("%Y-%m-%d %H:%M KST"),
        "pdf_count": len(pdfs),
        "categories": categories,
    }
    path = f"{REPORT_DIR}/{date_display}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  저장: {path}")

    update_index(date_display)

    # ── 드라이브 HTML 업로드 ──
    out_id = find_folder(service, "리포트 출력", root_id)
    if out_id:
        html = build_html(categories, date_display)
        upload_html(service, out_id, html, f"한경리포트_{date_display}.html")
    else:
        print("  '리포트 출력' 폴더 없음 — 드라이브 업로드 생략")

    print("완료")


if __name__ == "__main__":
    main()
