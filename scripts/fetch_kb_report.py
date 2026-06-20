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

# 입력 텍스트를 각 5000자로 제한해 응답 길이를 예측 가능하게 유지
_INPUT_LIMIT = 5000

SYSTEM_TEXT = (
    "당신은 경제·금융 교육 전문가입니다. "
    "KB증권 모닝 코멘트와 장마감 코멘트를 읽고, "
    "두 리포트 사이의 인과관계와 경제 개념 연결을 분석합니다. "
    "반드시 순수 JSON만 출력하세요. 마크다운, 설명, 코드블록 없이 {{ 로 시작하고 }} 로 끝내세요."
)


def _build_prompt(morning_text: str, close_text: str, date_display: str, n: int = 3) -> str:
    """분석 프롬프트 생성. n = 각 섹션 항목 수"""
    morning_section = f"[모닝 코멘트]\n{morning_text[:_INPUT_LIMIT]}" if morning_text else "[모닝 코멘트]\n(파일 없음)"
    close_section   = f"[장마감 코멘트]\n{close_text[:_INPUT_LIMIT]}"  if close_text  else "[장마감 코멘트]\n(파일 없음)"

    return (
        f"KB증권 {date_display} 리포트를 분석하여 아래 JSON 구조로만 응답하세요.\n"
        f"causal_chains {n}개, concept_connections {n}개, key_learnings {n}개.\n"
        "각 문자열 필드는 100자 이내로 간결하게 작성하세요.\n\n"
        '{"morning_summary":"(모닝 요약 100자 이내)",'
        '"close_summary":"(마감 요약 100자 이내)",'
        '"causal_chains":[{"title":"(제목)","morning_factor":"(모닝 요인)","close_outcome":"(마감 결과)","mechanism":"(메커니즘 설명)","concept_keywords":["개념1","개념2"]}],'
        '"concept_connections":[{"concept":"(용어)","morning_context":"(모닝 맥락)","close_context":"(마감 맥락)","explanation":"(연결 설명)"}],'
        '"key_learnings":["(학습 포인트1)","(학습 포인트2)","(학습 포인트3)"]}\n\n'
        f"{morning_section}\n\n"
        f"{close_section}"
    )


def _parse_json(raw: str) -> dict:
    """마크다운 코드블록 제거 후 JSON 파싱. 실패 시 { } 범위로 재시도."""
    text = raw.strip()

    # 코드블록 제거
    if "```" in text:
        after = text.split("```", 1)[1]
        if after.startswith("json"):
            after = after[4:]
        text = after.split("```")[0].strip()

    # 직접 파싱 시도
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 첫 { 부터 마지막 } 까지 추출해 재시도 (앞뒤 불필요한 텍스트 제거)
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass

    raise json.JSONDecodeError("JSON 파싱 실패", text, 0)


def analyze(morning_text: str, close_text: str, date_display: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system = [{"type": "text", "text": SYSTEM_TEXT, "cache_control": {"type": "ephemeral"}}]

    for attempt, n_items in enumerate([3, 2], start=1):
        prompt = _build_prompt(morning_text, close_text, date_display, n=n_items)
        print(f"  API 호출 (시도 {attempt}, 항목 수={n_items})...")

        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = resp.content[0].text
        print(f"  응답 {len(raw)}자 수신 (stop_reason={resp.stop_reason})")

        try:
            return _parse_json(raw)
        except json.JSONDecodeError as e:
            print(f"  [WARN] JSON 파싱 실패 (시도 {attempt}): {e}")
            print(f"  응답 끝 300자: {raw[-300:]!r}")
            if attempt == 2:
                raise RuntimeError(f"JSON 파싱 2회 실패. 마지막 응답:\n{raw[:500]}") from e


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
