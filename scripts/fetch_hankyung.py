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
import subprocess
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import anthropic
import pdfplumber
import pytz
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from usage_log import record

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
REPORT_DIR = "data/reports"

_DRIVE_PARAMS = dict(includeItemsFromAllDrives=True, supportsAllDrives=True)

# pdfplumber/pdfminer.six는 손상된 PDF에 대해 Python 예외 대신 세그폴트(SIGSEGV)를
# 일으킬 수 있다. try/except로는 못 잡으므로 별도 프로세스에서 추출을 실행해 격리한다.
_PDF_EXTRACT_WORKER_FLAG = "--pdf-extract-worker"


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
    print(f"    → 없음")
    return None


def list_all_month_folders(service, hk_id: str) -> list[tuple[str, str]]:
    """한경 PDF 폴더 아래 YYYY-MM 형식 하위폴더를 모두 반환."""
    res = service.files().list(
        q=f"mimeType='application/vnd.google-apps.folder' and '{hk_id}' in parents and trashed=false",
        fields="files(id,name)",
        orderBy="name",
        pageSize=200,
        **_DRIVE_PARAMS,
    ).execute()
    folders = []
    for f in res.get("files", []):
        if re.match(r"^\d{4}-\d{2}$", f["name"]):
            folders.append((f["id"], f["name"]))
            print(f"    월 폴더 발견: {f['name']}")
    return sorted(folders, key=lambda x: x[1])


def _all_pdfs_in_folder(service, folder_id: str) -> list[dict]:
    res = service.files().list(
        q=f"mimeType='application/pdf' and '{folder_id}' in parents and trashed=false",
        fields="files(id,name)",
        orderBy="name",
        pageSize=200,
        **_DRIVE_PARAMS,
    ).execute()
    return res.get("files", [])


def get_dates_in_folder(service, folder_id: str) -> dict[str, list[dict]]:
    """폴더 내 PDF들을 날짜별로 분류해 {YYYYMMDD: [파일목록]} 반환."""
    all_pdfs = _all_pdfs_in_folder(service, folder_id)
    by_date: dict[str, list] = defaultdict(list)
    for f in all_pdfs:
        m = re.search(r"(\d{8})", f["name"])
        if m:
            by_date[m.group(1)].append(f)
    return {d: sorted(files, key=lambda x: x["name"]) for d, files in by_date.items()}


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


def _extract_text_worker_main(pdf_path: str, out_path: str, filename: str) -> None:
    """서브프로세스 진입점: PDF 1개를 추출해 out_path에 텍스트로 기록한다."""
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    text = extract_text(pdf_bytes, filename)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)


def extract_text_isolated(pdf_bytes: bytes, filename: str) -> str:
    """
    extract_text()를 별도 프로세스에서 실행해 세그폴트로부터 격리한다.
    손상된 PDF가 pdfminer.six 내부에서 네이티브 크래시를 일으켜도
    이 자식 프로세스만 죽고, 부모는 returncode로 감지해 다음 PDF로 넘어간다.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        pdf_path = os.path.join(tmp_dir, "input.pdf")
        out_path = os.path.join(tmp_dir, "output.txt")
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)

        print(f"    추출 시작: {filename}", flush=True)
        try:
            result = subprocess.run(
                [sys.executable, __file__, _PDF_EXTRACT_WORKER_FLAG, pdf_path, out_path, filename],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            print(f"    [오류] {filename}: 추출 타임아웃(120초) — 건너뜀", flush=True)
            return ""

        if result.returncode != 0:
            if result.returncode < 0:
                print(f"    [오류] {filename}: 시그널 {-result.returncode}로 추출 프로세스 종료 "
                      f"(세그폴트 등 네이티브 크래시로 추정) — 건너뜀", flush=True)
            else:
                print(f"    [오류] {filename}: 추출 프로세스 실패 (exit={result.returncode}) — 건너뜀", flush=True)
            if result.stderr:
                print(f"      stderr: {result.stderr.strip()[:500]}", flush=True)
            return ""

        if not os.path.exists(out_path):
            print(f"    [오류] {filename}: 출력 파일이 생성되지 않음 — 건너뜀", flush=True)
            return ""

        with open(out_path, encoding="utf-8") as f:
            return f.read()


# ── 1단계: PDF별 기사 추출 (Haiku, 병렬) ─────────────────────────────────────

def _extract_articles_from_text(text: str, filename: str) -> str:
    """
    단일 PDF 원문 → '제목 + 핵심 문장' 목록 (Haiku)
    각 PDF를 독립적으로 처리해 텍스트 잘림 없이 전체 기사를 커버한다.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": (
                "한국경제신문 PDF에서 추출한 텍스트입니다.\n"
                "기사를 식별하고 아래 형식으로만 출력하세요.\n\n"
                "출력 형식:\n"
                "## [기사 제목]\n"
                "핵심: [핵심 팩트 1줄 — 수치·날짜·고유명사 포함, 50자 이내]\n\n"
                "제외 항목: 광고, 날씨, 증시 시황표, 부고/인사, 페이지 헤더·풋터\n"
                "기사가 없으면 아무것도 출력하지 마세요.\n\n"
                f"[{filename}]\n{text[:15000]}"
            ),
        }],
    )
    record("한경-기사추출", resp, detail=filename)
    return resp.content[0].text.strip()


def extract_all_articles(service, pdfs: list[dict]) -> str:
    """
    모든 PDF를 병렬로 다운로드·추출해 기사 목록을 결합한다.
    순서는 원래 PDF 순서를 유지한다.
    """
    def process_one(item: dict) -> tuple[str, str]:
        try:
            print(f"    다운로드 시작: {item['name']}", flush=True)
            data = download_file(service, item["id"])
            text = extract_text_isolated(data, item["name"])
            if not text.strip():
                return "", item["name"]
            articles = _extract_articles_from_text(text, item["name"])
            return articles, item["name"]
        except Exception as e:
            print(f"    오류({item['name']}): {e}", flush=True)
            return "", item["name"]

    # 병렬 실행 (최대 3 동시: Anthropic rate limit 고려)
    name_to_result: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(process_one, item): item["name"] for item in pdfs}
        for future in as_completed(futures):
            articles, name = future.result()
            name_to_result[name] = articles
            status = f"{len(articles):,}자" if articles else "내용 없음"
            print(f"    완료: {name} → {status}")

    # PDF 원래 순서로 합치기
    parts = [name_to_result[item["name"]] for item in pdfs if name_to_result.get(item["name"])]
    total = sum(len(p) for p in parts)
    print(f"  기사 추출 완료: {len(parts)}/{len(pdfs)} PDF 처리 → {total:,}자")
    return "\n\n".join(parts)


# ── 2단계: 섹터 분류 + 흐름 분석 (Sonnet) ────────────────────────────────────

def _repair_truncated_json(raw: str) -> str:
    """잘린 JSON을 닫힌 브래킷으로 복구 시도."""
    in_string = False
    escape_next = False
    for ch in raw:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
    if in_string:
        raw += '"'

    stack = []
    closer = {"{": "}", "[": "]"}
    in_string = False
    escape_next = False
    for ch in raw:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if not in_string:
            if ch in "{[":
                stack.append(closer[ch])
            elif ch in "}]" and stack and stack[-1] == ch:
                stack.pop()

    raw += "".join(reversed(stack))
    return raw


def analyze(article_list: str, date_display: str) -> dict:
    """
    압축된 기사 목록 → 헤드라인 전수 나열 + 유동적 클러스터링 + 클러스터별 설명 (Sonnet)
    고정 섹터가 아니라 그날 기사 내용에 따라 클러스터를 형성하고,
    클러스터에 묶이지 않는 기사는 단독 기사로 남긴다.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    prompt_messages = [{
        "role": "user",
        "content": (
            f"다음은 한국경제신문 {date_display}자 기사 목록입니다.\n\n"
            "▶ STEP A — 헤드라인 전수 나열\n"
            "받은 기사 목록의 모든 제목을 누락 없이 그대로 나열하세요 (요약·분석·분류 없이).\n\n"
            "▶ STEP B — 연결되는 것만 묶는 클러스터링\n"
            "- 주제가 유사하거나 서로 영향을 주는 기사들만 묶으세요. 모든 기사를 억지로 묶지 마세요.\n"
            "- 분류군은 고정 카테고리가 아니라 그날 기사 내용에 따라 유동적으로 형성하세요\n"
            "  (예: '한국은행 금리인상', 'AI 인프라 수요' 등 그날그날 다른 이름).\n"
            "- 핵심 클러스터는 최대 7개까지만 선정하세요 (그 이상 묶이면 관련성 높은 순으로 7개만 남기세요).\n"
            "- 클러스터에 안 묶이는 기사는 '단독 기사' 목록으로 별도로 남기세요 (제목만).\n\n"
            "▶ STEP C — 클러스터별 공부용 설명 (용어 설명은 제외)\n"
            "- 각 클러스터마다 배경·인과관계·맥락을 풀어쓴 문단 (3~6문장)을 작성하세요.\n"
            "- 기사 나열이 아니라 '왜 함께 읽혀야 하는지' 흐름으로 설명하세요.\n"
            "- 구체적 기업명·수치·정책명을 언급하세요.\n"
            "- 단정적 결론이나 투자 유도 표현은 쓰지 마세요.\n\n"
            "출력: JSON만 (마크다운 코드블록 없이)\n"
            "형식:\n"
            '{"headlines": ["제목1", "제목2", ...], '
            '"clusters": [{"title": "클러스터명", "articles": ["관련 제목1", "관련 제목2"], "explanation": "문단 설명..."}], '
            '"standalone": ["단독 기사 제목1", "단독 기사 제목2"]}\n'
            "clusters는 최대 7개.\n\n"
            f"[기사 목록]\n{article_list}"
        ),
    }]

    for attempt in range(3):
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=16000,
            system=[{
                "type": "text",
                "text": (
                    "당신은 신문을 해석해주는 사람이 아니라, 같이 읽고 이해를 돕는 공부 파트너입니다. "
                    "논설위원처럼 단정하지 않고, 투자 판단을 유도하지 않습니다. "
                    "의견을 제시할 때는 반드시 근거를 함께 제시하며 '이렇게 볼 수 있다' 수준으로만 말합니다. "
                    "어투는 존댓말 대신 친근하지만 분석적인 톤으로 씁니다."
                ),
                "cache_control": {"type": "ephemeral"},
            }],
            messages=prompt_messages,
        )
        record("한경-섹터종합", resp, detail=f"attempt{attempt+1}")

        raw = resp.content[0].text.strip()
        # 마크다운 코드블록 제거 (```json ... ``` 형태 대응)
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
        if match:
            raw = match.group(1).strip()

        if resp.stop_reason == "max_tokens":
            print(f"  [경고] Sonnet 응답이 max_tokens에서 잘림 (시도 {attempt + 1}/3), JSON 복구 시도...")
            raw = _repair_truncated_json(raw)

        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            if attempt < 2:
                print(f"  [경고] JSONDecodeError (시도 {attempt + 1}/3): {e} — 재시도...")
            else:
                raise


# ── 인덱스 관리 ───────────────────────────────────────────────────────────────

def load_index() -> dict:
    path = f"{REPORT_DIR}/index.json"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"dates": []}


def save_index(idx: dict) -> None:
    path = f"{REPORT_DIR}/index.json"
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
    # 날짜 내림차순 정렬 후 최근 60개 유지
    idx["dates"] = sorted(set(idx["dates"]), reverse=True)[:60]
    save_index(idx)


# ── 날짜 하나 처리 ─────────────────────────────────────────────────────────────

def process_date(service, date_str: str, pdfs: list[dict], generated_at: str) -> bool:
    """단일 날짜의 PDF를 분석해 리포트를 저장한다. 성공 시 True 반환."""
    date_display = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    print(f"\n  ── [{date_display}] 처리 시작 ({len(pdfs)}개 PDF) ──")

    print(f"  [1/2] 기사 추출 중 (Haiku, 병렬)...")
    article_list = extract_all_articles(service, pdfs)
    if not article_list.strip():
        print(f"  [{date_display}] 기사 추출 실패. 건너뜀.")
        return False

    print(f"  [2/2] 클러스터 분석 중 (Sonnet)...")
    result = analyze(article_list, date_display)

    os.makedirs(REPORT_DIR, exist_ok=True)
    report = {
        "date": date_display,
        "generated_at": generated_at,
        "pdf_date": date_str,
        "pdf_count": len(pdfs),
        "is_fallback": False,
        "headlines": result.get("headlines", []),
        "clusters": result.get("clusters", []),
        "standalone": result.get("standalone", []),
    }
    path = f"{REPORT_DIR}/{date_display}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  저장: {path}")

    update_index(date_display)
    return True


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    kst = pytz.timezone("Asia/Seoul")
    now = datetime.now(kst)
    generated_at = now.strftime("%Y-%m-%d %H:%M KST")

    print(f"[{generated_at}] 한경 PDF 분석 시작")

    service = get_drive_service()

    root_id = find_folder(service, "Joshua 증권")
    if not root_id:
        raise RuntimeError(
            "'Joshua 증권' 폴더를 찾을 수 없습니다.\n"
            "서비스 계정 이메일로 해당 폴더를 공유했는지 확인하세요."
        )

    hk_id = find_folder(service, "한경 PDF", root_id)
    if not hk_id:
        raise RuntimeError("'한경 PDF' 폴더를 찾을 수 없습니다.")

    # 모든 월 폴더 검색
    print("\n  월 폴더 목록 검색 중...")
    month_folders = list_all_month_folders(service, hk_id)
    if not month_folders:
        print("  월 폴더를 찾을 수 없습니다. 종료.")
        return
    print(f"  → {len(month_folders)}개 월 폴더 발견")

    # 이미 처리된 날짜 로드
    existing_dates = get_existing_dates()
    print(f"  이미 처리된 날짜: {len(existing_dates)}개")

    # 미처리 날짜 수집
    dates_to_process: list[tuple[str, list[dict]]] = []
    for folder_id, month_str in month_folders:
        dates_in_folder = get_dates_in_folder(service, folder_id)
        for date_str, pdfs in dates_in_folder.items():
            if date_str not in existing_dates:
                dates_to_process.append((date_str, pdfs))

    if not dates_to_process:
        print("  새로 처리할 날짜가 없습니다. 종료.")
        return

    # 날짜 오름차순으로 처리 (과거→현재)
    dates_to_process.sort(key=lambda x: x[0])
    print(f"\n  처리할 날짜 {len(dates_to_process)}개: {[d for d, _ in dates_to_process]}")

    success = 0
    for date_str, pdfs in dates_to_process:
        try:
            if process_date(service, date_str, pdfs, generated_at):
                success += 1
        except Exception as e:
            date_display = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
            print(f"  [{date_display}] 오류 발생: {e}")

    print(f"\n완료: {success}/{len(dates_to_process)}개 날짜 처리")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == _PDF_EXTRACT_WORKER_FLAG:
        _extract_text_worker_main(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        main()
