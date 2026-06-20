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
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta

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


def _all_pdfs_in_folder(service, folder_id: str) -> list[dict]:
    """폴더 내 PDF 전체 목록 (날짜 필터 없음, 최대 200개)"""
    res = service.files().list(
        q=f"mimeType='application/pdf' and '{folder_id}' in parents and trashed=false",
        fields="files(id,name)",
        orderBy="name",
        pageSize=200,
        **_DRIVE_PARAMS,
    ).execute()
    return res.get("files", [])


def find_best_pdfs(service, folder_id: str, today_str: str) -> tuple[list[dict], str]:
    """
    PDF 목록과 실제 사용 날짜(YYYYMMDD)를 반환.
    1) today_str 날짜 파일이 있으면 그것을 사용.
    2) 없으면 폴더 전체에서 YYYYMMDD 패턴을 추출해 가장 최근 날짜 그룹 사용.
    """
    # 1차: 오늘 날짜
    today_pdfs = [
        f for f in _all_pdfs_in_folder(service, folder_id)
        if today_str in f["name"]
    ]
    if today_pdfs:
        print(f"  오늘({today_str}) PDF {len(today_pdfs)}개 사용")
        return sorted(today_pdfs, key=lambda x: x["name"]), today_str

    print(f"  {today_str} PDF 없음 → 폴더 내 최근 날짜 검색 중...")

    # 2차: 전체 PDF에서 날짜 패턴 추출 후 최신 그룹
    all_pdfs = _all_pdfs_in_folder(service, folder_id)
    if not all_pdfs:
        print("  폴더에 PDF가 없습니다.")
        return [], ""

    by_date: dict[str, list] = defaultdict(list)
    for f in all_pdfs:
        m = re.search(r"(\d{8})", f["name"])
        if m:
            by_date[m.group(1)].append(f)

    if not by_date:
        print("  파일명에서 날짜 패턴(YYYYMMDD)을 찾을 수 없습니다.")
        print("  파일 목록:", [f["name"] for f in all_pdfs[:5]])
        return [], ""

    latest = max(by_date.keys())
    pdfs = sorted(by_date[latest], key=lambda x: x["name"])
    print(f"  최근 날짜 사용: {latest} (PDF {len(pdfs)}개, 전체 {len(all_pdfs)}개 중)")
    return pdfs, latest


def find_month_folder(service, hk_id: str, now: datetime) -> tuple[str | None, str]:
    """현재 월 폴더 탐색. 없으면 이전 달도 시도."""
    for delta in [0, 1]:
        target = (now.replace(day=1) - timedelta(days=1)) if delta else now
        month_str = target.strftime("%Y-%m")
        folder_id = find_folder(service, month_str, hk_id)
        if folder_id:
            return folder_id, month_str
    return None, ""


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
    today_str = now.strftime("%Y%m%d")   # 20260620 — 1차 검색 기준

    print(f"[{now.strftime('%Y-%m-%d %H:%M KST')}] 한경 PDF 분석 시작")

    service = get_drive_service()

    # ── 폴더 탐색 ──
    root_id = find_folder(service, "Joshua 증권")
    if not root_id:
        raise RuntimeError(
            "'Joshua 증권' 폴더를 찾을 수 없습니다.\n"
            "서비스 계정 이메일로 해당 폴더를 공유했는지 확인하세요."
        )

    hk_id = find_folder(service, "한경 PDF", root_id)
    if not hk_id:
        raise RuntimeError("'한경 PDF' 폴더를 찾을 수 없습니다.")

    # 현재 월 → 없으면 이전 달 자동 시도
    month_id, month_str = find_month_folder(service, hk_id, now)
    if not month_id:
        print("월 폴더를 찾을 수 없습니다. 종료.")
        return

    # ── PDF 선택 (오늘 날짜 우선, 없으면 최신 날짜) ──
    pdfs, actual_date_str = find_best_pdfs(service, month_id, today_str)
    if not pdfs:
        print("사용할 PDF를 찾을 수 없습니다. 종료.")
        return

    # 실제 PDF 날짜로 date_display 결정
    date_display = f"{actual_date_str[:4]}-{actual_date_str[4:6]}-{actual_date_str[6:]}"
    is_fallback = actual_date_str != today_str
    if is_fallback:
        print(f"  ※ 오늘({today_str}) 파일 없음 → {actual_date_str} 파일로 대체")

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
        "pdf_date": actual_date_str,          # 실제 PDF 날짜
        "pdf_count": len(pdfs),
        "is_fallback": is_fallback,           # 대체 날짜 사용 여부
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
