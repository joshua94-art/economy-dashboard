#!/usr/bin/env python3
"""
경제 뉴스(수동 작성 텍스트) 수집 스크립트
Google Drive "Joshua 증권 > 경제신문" 폴더의 .txt 파일을 읽어
data/economy_news/ 에 JSON으로 저장합니다.

Claude API를 호출하지 않습니다 — 순수 텍스트 처리이므로 비용이 발생하지 않습니다.

파일명 규칙: "2026년 8월 17일 한국경제신문.txt" 형식.
정규식 (\\d{4})년\\s*(\\d{1,2})월\\s*(\\d{1,2})일 로 날짜(YYYY-MM-DD)를 추출합니다.

사전 준비: fetch_hankyung.py와 동일 (서비스 계정 + GOOGLE_CREDENTIALS 시크릿).
"""

import io
import json
import os
import re

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
NEWS_DIR = "data/economy_news"

_DRIVE_PARAMS = dict(includeItemsFromAllDrives=True, supportsAllDrives=True)

_DATE_PATTERN = re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")


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


def list_txt_files(service, folder_id: str) -> list[dict]:
    res = service.files().list(
        q=f"mimeType='text/plain' and '{folder_id}' in parents and trashed=false",
        fields="files(id,name)",
        orderBy="name",
        pageSize=200,
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


# ── 파일명 → 날짜 ───────────────────────────────────────────────────────────────

def parse_date_from_filename(filename: str) -> str | None:
    """'2026년 8월 17일 한국경제신문.txt' → '2026-08-17'. 패턴 불일치 시 None."""
    m = _DATE_PATTERN.search(filename)
    if not m:
        return None
    year, month, day = m.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


# ── 인덱스 관리 ───────────────────────────────────────────────────────────────

def load_index() -> dict:
    path = f"{NEWS_DIR}/index.json"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"dates": []}


def save_index(idx: dict) -> None:
    path = f"{NEWS_DIR}/index.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)


def get_existing_dates() -> set[str]:
    """이미 처리된 날짜를 YYYYMMDD 형식의 집합으로 반환."""
    idx = load_index()
    result = set()
    for d in idx.get("dates", []):
        result.add(d.replace("-", ""))
    return result


def update_index(date_display: str) -> None:
    idx = load_index()
    if date_display not in idx["dates"]:
        idx["dates"].append(date_display)
    # 날짜 내림차순(최신순) 정렬
    idx["dates"] = sorted(set(idx["dates"]), reverse=True)
    save_index(idx)


# ── 파일 하나 처리 ───────────────────────────────────────────────────────────────

def split_title_and_content(text: str, filename: str) -> tuple[str | None, str]:
    """
    첫 non-empty 줄을 제목으로 분리하고, 나머지를 본문으로 반환한다.
    제목 뒤에 이어지는 빈 줄들은 최대 1개로 정리한다(본문 시작에 빈 줄 여러 개 남지 않게).
    전체가 공백뿐이면 (None, "") 반환.
    """
    lines = text.splitlines()

    title_idx = None
    for i, line in enumerate(lines):
        if line.strip():
            title_idx = i
            break
    if title_idx is None:
        return None, ""

    title = lines[title_idx].strip()
    rest = lines[title_idx + 1:]

    # 제목 다음의 연속된 빈 줄을 최대 1개로 축소
    j = 0
    while j < len(rest) and not rest[j].strip():
        j += 1
    if j > 0:
        rest = [""] + rest[j:]

    content = "\n".join(rest)
    return title, content


def process_file(service, date_display: str, file: dict) -> bool:
    """단일 텍스트 파일을 읽어 JSON으로 저장한다. 성공 시 True 반환."""
    print(f"  다운로드: {file['name']}")
    raw = download_file(service, file["id"])
    # Windows에서 작성된 UTF-8 BOM 텍스트도 처리
    raw_text = raw.decode("utf-8-sig", errors="replace")

    title, content = split_title_and_content(raw_text, file["name"])
    if title is None:
        print(f"  [{date_display}] 내용이 비어 있습니다. 건너뜀.")
        return False

    os.makedirs(NEWS_DIR, exist_ok=True)
    article = {
        "date": date_display,
        "title": title,
        "content": content,
    }
    path = f"{NEWS_DIR}/{date_display}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(article, f, ensure_ascii=False, indent=2)
    print(f"  저장: {path}")

    update_index(date_display)
    return True


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("경제 뉴스 수집 시작 (Claude API 미호출)")

    service = get_drive_service()

    root_id = find_folder(service, "Joshua 증권")
    if not root_id:
        raise RuntimeError(
            "'Joshua 증권' 폴더를 찾을 수 없습니다.\n"
            "서비스 계정 이메일로 해당 폴더를 공유했는지 확인하세요."
        )

    news_id = find_folder(service, "경제신문", root_id)
    if not news_id:
        raise RuntimeError("'경제신문' 폴더를 찾을 수 없습니다.")

    txt_files = list_txt_files(service, news_id)
    print(f"  → .txt 파일 {len(txt_files)}개 발견")
    if not txt_files:
        print("  처리할 파일이 없습니다. 종료.")
        return

    existing_dates = get_existing_dates()
    print(f"  이미 처리된 날짜: {len(existing_dates)}개")

    # 파일명에서 날짜 추출, 미처리 날짜만 수집
    files_to_process: list[tuple[str, dict]] = []
    for f in txt_files:
        date_display = parse_date_from_filename(f["name"])
        if date_display is None:
            print(f"  [경고] 파일명이 날짜 패턴과 맞지 않아 건너뜀: {f['name']}")
            continue
        if date_display.replace("-", "") in existing_dates:
            continue
        files_to_process.append((date_display, f))

    if not files_to_process:
        print("  새로 처리할 파일이 없습니다. 종료.")
        return

    # 날짜 오름차순으로 처리 (과거→현재)
    files_to_process.sort(key=lambda x: x[0])
    print(f"\n  처리할 날짜 {len(files_to_process)}개: {[d for d, _ in files_to_process]}")

    success = 0
    for date_display, f in files_to_process:
        try:
            if process_file(service, date_display, f):
                success += 1
        except Exception as e:
            print(f"  [{date_display}] 오류 발생: {e}")

    print(f"\n완료: {success}/{len(files_to_process)}개 파일 처리")


if __name__ == "__main__":
    main()
