#!/usr/bin/env python3
"""
WSJ(월스트리트저널) 텍스트 수집 스크립트
Google Drive "Joshua 증권 > WSJ" 폴더의 .txt 파일을 읽어
data/wsj/ 에 클러스터별 본문 + 영어 단어 목록 JSON으로 저장합니다.

Claude API를 호출하지 않습니다 — 순수 텍스트 처리이므로 비용이 발생하지 않습니다.
(scripts/fetch_economy_news.py와 Drive 인증/폴더 접근/날짜 추출/dedup 패턴 동일)

파일명 규칙: "2026년 8월 16일 월스트리트저널.txt" 형식.
정규식 (\\d{4})년\\s*(\\d{1,2})월\\s*(\\d{1,2})일 로 날짜(YYYY-MM-DD)를 추출합니다.

원문 구조:
- "📰 클러스터 N. 제목" 으로 시작하는 섹션이 여러 개 나열됨
- 각 섹션 본문 뒤에 "📖 영어 단어" 라벨과 "word — 뜻" 형식의 줄들이 이어짐
  (다음 "📰 클러스터"가 나오기 전까지)

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
WSJ_DIR = "data/wsj"

_DRIVE_PARAMS = dict(includeItemsFromAllDrives=True, supportsAllDrives=True)

_DATE_PATTERN = re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")

_SECTION_PATTERN = re.compile(r"^📰\s*(.+)$")
_VOCAB_LABEL = "📖 영어 단어"
# "word — 뜻" / "word – 뜻" / "word - 뜻" 모두 허용 (대시 앞뒤에 공백 필수 —
# 단어 내부 하이픈(cost-effective 등)과 구분하기 위함)
_VOCAB_LINE = re.compile(r"^(.+?)\s+[—–-]\s+(.+)$")
# 한 줄에 " — "가 여러 번 나오면(예: "heckle 없음 — 대신 berate — 심하게 야단치다")
# 첫 매치의 word에 메타성 텍스트가 통째로 들어갈 수 있어 필터링한다.
_VOCAB_META_MARKERS = ("없음", "아님", "대신")
_VOCAB_WORD_MAX_LEN = 15


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
    """'2026년 8월 16일 월스트리트저널.txt' → '2026-08-16'. 패턴 불일치 시 None."""
    m = _DATE_PATTERN.search(filename)
    if not m:
        return None
    year, month, day = m.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


# ── 인덱스 관리 ───────────────────────────────────────────────────────────────

def load_index() -> dict:
    path = f"{WSJ_DIR}/index.json"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"dates": []}


def save_index(idx: dict) -> None:
    path = f"{WSJ_DIR}/index.json"
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


# ── 제목/본문 분리 ───────────────────────────────────────────────────────────────

def split_title_and_content(text: str, filename: str) -> tuple[str | None, str]:
    """
    첫 non-empty 줄을 제목으로 분리하고, 나머지를 본문으로 반환한다.
    제목 뒤에 이어지는 빈 줄들은 최대 1개로 정리한다.
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

    j = 0
    while j < len(rest) and not rest[j].strip():
        j += 1
    if j > 0:
        rest = [""] + rest[j:]

    content = "\n".join(rest)
    return title, content


# ── 클러스터/단어 파싱 ───────────────────────────────────────────────────────────

def parse_sections(content: str) -> tuple[list[dict], list[dict], int]:
    """
    '📰 클러스터 N. 제목' 기준으로 섹션을 나누고, 각 섹션의
    '📖 영어 단어' 이후 'word — 뜻' 줄들을 vocab으로 뽑아낸다.
    Returns: (sections, vocab, skipped) — vocab은 전체 섹션을 합친 하나의 목록,
    skipped는 의심스러운 word로 걸러져 제외된 줄 수.
    """
    lines = content.split("\n")

    boundaries: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _SECTION_PATTERN.match(line.strip())
        if m:
            boundaries.append((i, m.group(1).strip()))

    if not boundaries:
        return [], [], 0

    sections: list[dict] = []
    vocab: list[dict] = []
    skipped = 0

    for idx, (start, title) in enumerate(boundaries):
        end = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(lines)
        block = lines[start + 1:end]

        label_idx = next((j for j, l in enumerate(block) if l.strip() == _VOCAB_LABEL), None)
        if label_idx is None:
            body_lines, vocab_lines = block, []
        else:
            body_lines, vocab_lines = block[:label_idx], block[label_idx + 1:]

        sections.append({"title": title, "body": "\n".join(body_lines).strip()})

        for vl in vocab_lines:
            vl = vl.strip()
            if not vl:
                continue
            vm = _VOCAB_LINE.match(vl)
            if not vm:
                continue
            word, meaning = vm.group(1).strip(), vm.group(2).strip()

            # " — "가 줄 안에 여러 번 있으면 첫 매치의 word에 메타성 텍스트가
            # 통째로 섞여 들어갈 수 있다 (예: "heckle 없음 — 대신 berate — ...").
            # 정상적인 영단어/숙어라 보기 어려운 word는 걸러낸다.
            if any(marker in word for marker in _VOCAB_META_MARKERS) or len(word) > _VOCAB_WORD_MAX_LEN:
                print(f"    [경고] vocab 줄 스킵 (의심스러운 word): {vl!r}")
                skipped += 1
                continue

            vocab.append({"word": word, "meaning": meaning})

    return sections, vocab, skipped


# ── 파일 하나 처리 ───────────────────────────────────────────────────────────────

def process_file(service, date_display: str, file: dict) -> tuple[bool, int]:
    """단일 텍스트 파일을 읽어 JSON으로 저장한다. Returns: (성공 여부, 스킵된 vocab 줄 수)."""
    print(f"  다운로드: {file['name']}")
    raw = download_file(service, file["id"])
    # Windows에서 작성된 UTF-8 BOM 텍스트도 처리
    raw_text = raw.decode("utf-8-sig", errors="replace")

    title, content = split_title_and_content(raw_text, file["name"])
    if title is None:
        print(f"  [{date_display}] 내용이 비어 있습니다. 건너뜀.")
        return False, 0

    sections, vocab, skipped = parse_sections(content)
    if not sections:
        print(f"  [경고] [{date_display}] '📰 클러스터' 패턴을 찾을 수 없습니다. 건너뜀.")
        return False, 0

    os.makedirs(WSJ_DIR, exist_ok=True)
    article = {
        "date": date_display,
        "title": title,
        "sections": sections,
        "vocab": vocab,
    }
    path = f"{WSJ_DIR}/{date_display}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(article, f, ensure_ascii=False, indent=2)
    print(f"  저장: {path} (섹션 {len(sections)}개, 단어 {len(vocab)}개, 스킵 {skipped}개)")

    update_index(date_display)
    return True, skipped


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("WSJ 수집 시작 (Claude API 미호출)")

    service = get_drive_service()

    root_id = find_folder(service, "Joshua 증권")
    if not root_id:
        raise RuntimeError(
            "'Joshua 증권' 폴더를 찾을 수 없습니다.\n"
            "서비스 계정 이메일로 해당 폴더를 공유했는지 확인하세요."
        )

    wsj_id = find_folder(service, "WSJ", root_id)
    if not wsj_id:
        raise RuntimeError("'WSJ' 폴더를 찾을 수 없습니다.")

    txt_files = list_txt_files(service, wsj_id)
    print(f"  → .txt 파일 {len(txt_files)}개 발견")
    if not txt_files:
        print("  처리할 파일이 없습니다. 종료.")
        return

    existing_dates = get_existing_dates()
    print(f"  이미 처리된 날짜: {len(existing_dates)}개")

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

    files_to_process.sort(key=lambda x: x[0])
    print(f"\n  처리할 날짜 {len(files_to_process)}개: {[d for d, _ in files_to_process]}")

    success = 0
    total_skipped = 0
    for date_display, f in files_to_process:
        try:
            ok, skipped = process_file(service, date_display, f)
            if ok:
                success += 1
            total_skipped += skipped
        except Exception as e:
            print(f"  [{date_display}] 오류 발생: {e}")

    print(f"\n완료: {success}/{len(files_to_process)}개 파일 처리")
    print(f"스킵된 vocab 줄: {total_skipped}개")


if __name__ == "__main__":
    main()
