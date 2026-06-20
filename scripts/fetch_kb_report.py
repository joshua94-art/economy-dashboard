#!/usr/bin/env python3
"""
KB증권 리포트 PDF 분석 스크립트
Google Drive "Joshua 증권/KB 리포트/YYYY-MM" 폴더에서
kb_morning_YYYYMMDD.pdf / kb_close_YYYYMMDD.pdf 를 읽어
두 리포트 간 인과관계·개념 연결을 분석합니다.
"""

import io
import json
import os
import re
from datetime import datetime, timedelta

import anthropic
import pdfplumber
import pytz
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
REPORT_DIR = "data/kb_reports"
_DRIVE_PARAMS = dict(includeItemsFromAllDrives=True, supportsAllDrives=True)


# ── Google Drive 유틸 ──────────────────────────────────────────────────────────

def get_drive_service():
    info = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def find_folder(service, name: str, parent_id: str | None = None) -> str | None:
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
    print("    → 없음")
    return None


def find_kb_pdfs(service, folder_id: str, today_str: str) -> tuple[dict | None, dict | None, str]:
    """
    kb_morning_YYYYMMDD.pdf / kb_close_YYYYMMDD.pdf 탐색.
    오늘 날짜 없으면 폴더 내 최신 날짜로 대체.
    Returns: (morning_file, close_file, actual_date_str)
    """
    res = service.files().list(
        q=f"mimeType='application/pdf' and '{folder_id}' in parents and trashed=false",
        fields="files(id,name)",
        orderBy="name",
        pageSize=200,
        **_DRIVE_PARAMS,
    ).execute()
    all_pdfs = res.get("files", [])

    def find_pair(date_str):
        morning = next((f for f in all_pdfs if f["name"] == f"kb_morning_{date_str}.pdf"), None)
        close   = next((f for f in all_pdfs if f["name"] == f"kb_close_{date_str}.pdf"),   None)
        return morning, close

    morning, close = find_pair(today_str)
    if morning or close:
        print(f"  오늘({today_str}) KB PDF — 모닝: {'있음' if morning else '없음'}, 마감: {'있음' if close else '없음'}")
        return morning, close, today_str

    print(f"  {today_str} KB PDF 없음 → 최신 날짜 검색 중...")
    dates = set()
    for f in all_pdfs:
        m = re.search(r"(\d{8})", f["name"])
        if m:
            dates.add(m.group(1))
    if not dates:
        print("  날짜 패턴을 찾을 수 없습니다.")
        return None, None, ""

    latest = max(dates)
    morning, close = find_pair(latest)
    print(f"  최신 날짜 사용: {latest} — 모닝: {'있음' if morning else '없음'}, 마감: {'있음' if close else '없음'}")
    return morning, close, latest


def download_file(service, file_id: str) -> bytes:
    req = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return buf.read()


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

def analyze(morning_text: str, close_text: str, date_display: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    template = {
        "morning_summary": "모닝 코멘트 핵심 3-4문장 요약",
        "close_summary": "장마감 코멘트 핵심 3-4문장 요약",
        "causal_chains": [
            {
                "title": "인과관계 제목 (간결하게)",
                "morning_factor": "모닝에서 언급한 요인/예측/전망",
                "close_outcome": "마감에서 확인된 실제 결과/전개",
                "mechanism": "인과 메커니즘 설명 (왜/어떻게 연결되는지 2-3문장)",
                "concept_keywords": ["관련 경제 개념1", "개념2"]
            }
        ],
        "concept_connections": [
            {
                "concept": "경제 개념/용어",
                "morning_context": "모닝에서 이 개념이 등장한 맥락",
                "close_context": "마감에서 이 개념이 등장한 맥락",
                "explanation": "두 맥락 간 연결 설명 및 개념 이해 포인트 (2-3문장)"
            }
        ],
        "key_learnings": [
            "오늘 리포트에서 배울 수 있는 핵심 경제 학습 포인트 1",
            "핵심 학습 포인트 2",
            "핵심 학습 포인트 3"
        ]
    }

    morning_section = f"[모닝 코멘트]\n{morning_text[:15000]}" if morning_text else "[모닝 코멘트]\n(파일 없음)"
    close_section   = f"[장마감 코멘트]\n{close_text[:15000]}"  if close_text  else "[장마감 코멘트]\n(파일 없음)"

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": (
                    "당신은 경제·금융 교육 전문가입니다. "
                    "KB증권 모닝 코멘트와 장마감 코멘트를 읽고, "
                    "두 리포트 사이의 인과관계와 경제 개념 연결을 분석하여 "
                    "투자자의 경제 공부를 돕는 학습 리포트를 작성합니다. "
                    "인과관계는 '아침 예측→실제 결과' 흐름으로, "
                    "개념 연결은 두 리포트에 공통으로 등장하는 경제 용어/개념을 중심으로 분석하세요."
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    f"다음은 KB증권 {date_display} 리포트입니다.\n\n"
                    "두 리포트 간 인과관계(아침 예측 vs 실제 결과)와 경제 개념 연결을 분석하여 "
                    "아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 JSON만).\n"
                    "causal_chains 3-5개, concept_connections 3-5개, key_learnings 3-5개 작성하세요.\n\n"
                    f"형식:\n{json.dumps(template, ensure_ascii=False, indent=2)}\n\n"
                    f"{morning_section}\n\n"
                    f"{close_section}"
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
    today_str = now.strftime("%Y%m%d")

    print(f"[{now.strftime('%Y-%m-%d %H:%M KST')}] KB 리포트 분석 시작")

    service = get_drive_service()

    root_id = find_folder(service, "Joshua 증권")
    if not root_id:
        raise RuntimeError(
            "'Joshua 증권' 폴더를 찾을 수 없습니다.\n"
            "서비스 계정 이메일로 해당 폴더를 공유했는지 확인하세요."
        )

    kb_id = find_folder(service, "KB 리포트", root_id)
    if not kb_id:
        raise RuntimeError("'KB 리포트' 폴더를 찾을 수 없습니다.")

    # 현재 월 폴더 탐색, 없으면 이전 달 시도
    month_id, month_str = None, ""
    for delta in [0, 1]:
        target = (now.replace(day=1) - timedelta(days=1)) if delta else now
        m_str = target.strftime("%Y-%m")
        month_id = find_folder(service, m_str, kb_id)
        if month_id:
            month_str = m_str
            break
    if not month_id:
        print("월 폴더를 찾을 수 없습니다. 종료.")
        return

    morning_file, close_file, actual_date_str = find_kb_pdfs(service, month_id, today_str)
    if not morning_file and not close_file:
        print("KB PDF 파일이 없습니다. 종료.")
        return

    date_display = f"{actual_date_str[:4]}-{actual_date_str[4:6]}-{actual_date_str[6:]}"
    is_fallback = actual_date_str != today_str
    if is_fallback:
        print(f"  ※ 오늘({today_str}) 파일 없음 → {actual_date_str} 파일로 대체")

    morning_text, close_text = "", ""

    if morning_file:
        print(f"  다운로드: {morning_file['name']}")
        morning_text = extract_text(download_file(service, morning_file["id"]), morning_file["name"])
        print(f"    → {len(morning_text):,}자 추출")
    else:
        print("  모닝 코멘트 PDF 없음")

    if close_file:
        print(f"  다운로드: {close_file['name']}")
        close_text = extract_text(download_file(service, close_file["id"]), close_file["name"])
        print(f"    → {len(close_text):,}자 추출")
    else:
        print("  장마감 코멘트 PDF 없음")

    if not morning_text and not close_text:
        print("  텍스트 추출 실패. 종료.")
        return

    print("  Claude 분석 중...")
    analysis = analyze(morning_text, close_text, date_display)

    os.makedirs(REPORT_DIR, exist_ok=True)
    report = {
        "date": date_display,
        "generated_at": now.strftime("%Y-%m-%d %H:%M KST"),
        "actual_date": actual_date_str,
        "is_fallback": is_fallback,
        "has_morning": morning_file is not None,
        "has_close": close_file is not None,
        **analysis,
    }
    path = f"{REPORT_DIR}/{date_display}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  저장: {path}")

    update_index(date_display)
    print("완료")


if __name__ == "__main__":
    main()
