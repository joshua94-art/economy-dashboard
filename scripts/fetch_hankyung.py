#!/usr/bin/env python3
"""
한국경제신문 PDF 분석 스크립트

사전 준비 (최초 1회):
1. Google Cloud Console → 프로젝트 생성 → Drive API 활성화
2. IAM → 서비스 계정 생성 → JSON 키 다운로드
3. 구글 드라이브 'Joshua 증권' 폴더를 서비스 계정 이메일과 공유 (뷰어)
4. JSON 키 전체 내용을 GitHub 시크릿 GOOGLE_CREDENTIALS 로 등록

리포트 출력: GitHub 레포 data/reports/ 에 JSON으로 저장 (GitHub Actions가 커밋)

핵심 주의사항:
- 서비스 계정은 자신의 빈 Drive를 가짐. 'root' 기준 검색은 공유 폴더를 찾지 못함.
- 모든 API 호출에 supportsAllDrives=True, includeItemsFromAllDrives=True 필요.
- 최상위 폴더("Joshua 증권") 탐색 시 parent 조건 없이 이름만으로 검색.
"""

import io
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta

import anthropic
import pdfplumber
import pytz
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
REPORT_DIR = "data/reports"
SECTORS = [
    "반도체/AI", "배터리/EV", "바이오/제약", "자동차/모빌리티",
    "에너지/소재", "금융/증권", "부동산/건설", "글로벌/경제", "정책/정치", "유통/소비",
]

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



# ── PDF → 텍스트 ───────────────────────────────────────────────────────────────

def extract_text(pdf_bytes: bytes, filename: str) -> str:
    parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            text = (page.extract_text() or "").strip()
            if text:
                parts.append(f"[{filename} / {i+1}p]\n{text}")
    return "\n\n".join(parts)


# ── Claude 분석 (2단계 파이프라인) ───────────────────────────────────────────

def extract_article_list(text: str) -> str:
    """
    1단계: PDF 원문 → 기사 제목+핵심 문장 목록으로 압축 (Haiku 사용)
    광고·시황표·날씨 등 비기사 콘텐츠를 제거하고 구조화된 목록으로 만든다.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4000,
        messages=[
            {
                "role": "user",
                "content": (
                    "신문 PDF에서 추출한 텍스트입니다. 각 기사를 식별하고 아래 형식으로만 출력하세요.\n\n"
                    "출력 형식:\n"
                    "## [기사 제목]\n"
                    "핵심: [가장 중요한 팩트 1줄 — 수치·날짜·고유명사 포함, 50자 이내]\n\n"
                    "규칙:\n"
                    "- 광고, 날씨, 증시 시황표, 인명/부고, 페이지 헤더·풋터는 제외\n"
                    "- 기사 제목이 없으면 내용에서 핵심 주제를 15자 이내로 작성\n"
                    "- 기사당 '핵심' 줄은 반드시 1개만, 설명 없이 출력\n\n"
                    f"[원문]\n{text[:60000]}"
                ),
            }
        ],
    )
    return resp.content[0].text.strip()


def analyze(article_list: str, date_display: str) -> dict:
    """
    2단계: 압축된 기사 목록 → 섹터 분류 + 인과 흐름 서술 (Sonnet 사용)
    같은 섹터 기사들을 연결해 하나의 서사로 설명한다.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    template = json.dumps(
        {s: {"flow": "...", "articles": [{"title": "...", "key_sentence": "..."}]} for s in SECTORS},
        ensure_ascii=False,
    )

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        system=[
            {
                "type": "text",
                "text": (
                    "당신은 한국 경제 전문 에디터입니다. "
                    "산업 섹터별로 기사를 분류하고, 같은 섹터 기사들이 어떤 인과 흐름으로 연결되는지 "
                    "하나의 서사로 설명합니다."
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    f"다음은 한국경제신문 {date_display}자 기사 목록입니다.\n\n"
                    "▶ 지침:\n"
                    "1. 각 기사를 아래 섹터 중 1개에 배정:\n"
                    f"   {', '.join(SECTORS)}\n\n"
                    "2. 각 기사에서:\n"
                    "   - title: 기사 제목 원문 그대로\n"
                    "   - key_sentence: 핵심 팩트 1개 (수치·고유명사 포함, 40자 이내, 명사형 종결)\n"
                    "     좋은 예) '삼성전자 2Q 영업이익 12조, 전년比 +60%'\n"
                    "     나쁜 예) '반도체 업황이 개선되고 있다'\n\n"
                    "3. 섹터별 '흐름' 작성 (3~5문장):\n"
                    "   - 섹터 내 기사들의 인과관계·트렌드를 연결해 하나의 스토리로 서술\n"
                    "   - 구체적 기업명·수치·정책명을 언급하며\n"
                    "     'A가 → B로 이어지고 → C를 시사'하는 흐름 구조로\n"
                    "   - 단순 나열 금지. 기사들이 왜 함께 읽혀야 하는지 맥락을 설명\n"
                    "   - 기사 1건이면 해당 사안의 배경·맥락·시사점을 2~3문장으로\n\n"
                    "4. 기사 없는 섹터는 JSON에서 제외\n\n"
                    f"출력 형식 (JSON만, 마크다운 코드블록 없이):\n{template}\n\n"
                    f"[기사 목록]\n{article_list}"
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

    # ── 1단계: 기사 목록 압축 (Haiku) ──
    print("  [1/2] 기사 목록 압축 중 (Haiku)...")
    article_list = extract_article_list(combined)
    print(f"  압축 완료: {len(article_list):,}자")

    # ── 2단계: 섹터 분류 + 흐름 분석 (Sonnet) ──
    print("  [2/2] 섹터 흐름 분석 중 (Sonnet)...")
    sectors = analyze(article_list, date_display)

    # ── 로컬 저장 ──
    os.makedirs(REPORT_DIR, exist_ok=True)
    report = {
        "date": date_display,
        "generated_at": now.strftime("%Y-%m-%d %H:%M KST"),
        "pdf_date": actual_date_str,
        "pdf_count": len(pdfs),
        "is_fallback": is_fallback,
        "sectors": sectors,
    }
    path = f"{REPORT_DIR}/{date_display}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  저장: {path}")

    update_index(date_display)

    print("완료")


if __name__ == "__main__":
    main()
