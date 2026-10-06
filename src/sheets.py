"""구글 시트 병합 기록.

핵심 원칙: 기존 데이터를 통째로 지우지 않는다.
리프레시 구간(start~end) 안에서, 이번에 수집한 브랜드의 행만 교체하고
나머지(구간 밖 과거 데이터, 수집하지 않은 브랜드)는 그대로 보존한다.
"""
import json
import os
import re
import time
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from . import mapping_alert

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# A~N = 자동화가 관리하는 영역. O 이후 수식 열은 기본적으로 건드리지 않는다.
DATA_COLS = 14          # A..N
COL_DATE  = 1           # B 날짜 (0-based)
COL_BRAND = 4           # E 브랜드

# 시트가 6만 행을 넘어가면 한 번에 읽고 쓰는 요청이 API 크기 제한에 걸린다.
# 아래 단위로 쪼개서 처리한다.
READ_CHUNK  = int(os.environ.get("READ_CHUNK", "20000"))
WRITE_CHUNK = int(os.environ.get("WRITE_CHUNK", "5000"))

# O~V 수식 템플릿 (FILL_FORMULAS=true 일 때만 사용)
FORMULA_TEMPLATES = {
    # 라이브 시트 O2/P2 ARRAYFORMULA와 같은 판정 규칙의 행별 버전이다.
    # FILL_FORMULAS=true를 쓰더라도 검사 결과가 달라지지 않아야 한다.
    "O": ('=IF(G{r}="","",IFS('
          'ISNUMBER(SEARCH("스트랩",G{r})),"스트랩",'
          'ISNUMBER(SEARCH("토퍼",G{r})),"두부토퍼",'
          'ISNUMBER(SEARCH("모달",G{r})),"러플모달이불",'
          'ISNUMBER(SEARCH("압축",G{r})),"이불압축파우치",'
          'ISNUMBER(SEARCH("우유베개",G{r})),"우유베개알파",'
          'ISNUMBER(SEARCH("프로즌",G{r}))+ISNUMBER(SEARCH("칠링버디",G{r})),"프로즌",'
          'ISNUMBER(SEARCH("베개",G{r})),"두부베개",'
          'ISNUMBER(SEARCH("스테이쿨 냉감이불",G{r})),"스테이쿨냉감이불",'
          'ISNUMBER(SEARCH("스테이쿨 냉감패드",G{r})),"스테이쿨냉감패드",'
          'ISNUMBER(SEARCH("냉감이불",G{r}))+ISNUMBER(SEARCH("냉감 이불",G{r})),"스테이쿨냉감이불",'
          'ISNUMBER(SEARCH("냉감 패드",G{r}))+ISNUMBER(SEARCH("냉감패드",G{r})),"스테이쿨냉감패드",'
          'ISNUMBER(SEARCH("화이트 바디필로우",G{r})),"프로즌",'
          'ISNUMBER(SEARCH("세탁바구니",G{r})),"세탁바구니",'
          'TRUE,""))'),
    "P": '=IF(D{r}="","",IFERROR(VLOOKUP(TRIM(D{r}),appendix!B:C,2,0),"미매핑"))',
    "Q": '=IFERROR(TRIM(REGEXEXTRACT(G{r},"\\[공구\\](.*)X")),"")',
    "R": '=IF(ISNUMBER(SEARCH("공구",G{r})),"공구",IF(ISNUMBER(SEARCH("핫딜",G{r})),"공구",""))',
    "S": '=IF(A{r}="","",YEAR(A{r}))',
    "T": '=IF(A{r}="","",MONTH(A{r}))',
    "U": '=IF(A{r}="","",WEEKNUM(A{r},2))',
    "V": '=IF(A{r}="","",DAY(A{r}))',
}


def client():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise SystemExit("GOOGLE_SERVICE_ACCOUNT_JSON 누락")
    return gspread.authorize(
        Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES))


def worksheet(gc, sheet_id: str, tab: str):
    sh = gc.open_by_key(sheet_id)
    try:
        return sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        raise SystemExit(f"탭을 찾을 수 없습니다: '{tab}' (SHEET_TAB 확인)")


def parse_date(v) -> str:
    """다양한 표기에서 'YYYY-MM-DD' 만 뽑는다. 실패하면 빈 문자열."""
    if v is None:
        return ""
    s = str(v).strip()
    # '2026. 9. 23' 처럼 점 뒤에 공백이 있는 한국어 날짜 표시도 읽는다.
    # 못 읽으면 기존 행이 교체되지 않고 매 실행마다 같은 주문이 쌓인다 (2026-10 실제 발생).
    m = re.match(r"(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _pad(row, n: int) -> list:
    return (list(row) + [""] * n)[:n]


def retry(fn, *args, **kwargs):
    """429/5xx 는 지수 백오프로 재시도. 요청이 잘게 쪼개질수록 필요하다."""
    last = None
    for i in range(5):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            last = e
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (429, 500, 502, 503) and i < 4:
                wait = 3 * (2 ** i)
                print(f"  API {code} - {wait}초 후 재시도 ({i+1}/4)", flush=True)
                time.sleep(wait)
                continue
            raise
    raise last


def read_existing(ws) -> tuple[list, list]:
    """A~N 영역을 청크로 나눠 읽어 (헤더, 데이터행) 반환."""
    total = ws.row_count
    values = []
    start = 1
    while start <= total:
        end = min(start + READ_CHUNK - 1, total)
        chunk = retry(ws.get, f"A{start}:N{end}") or []
        values.extend(chunk)
        # 빈 청크가 나오면 그 아래는 데이터가 없다고 보고 중단
        if len(chunk) < (end - start + 1):
            break
        start = end + 1

    if not values:
        return [], []
    header = _pad(values[0], DATA_COLS)
    rows = [_pad(r, DATA_COLS) for r in values[1:]
            if any(str(c).strip() for c in r)]
    return header, rows


def _first_diff(a: list, b: list) -> int:
    """두 행 목록이 처음으로 달라지는 위치. 앞부분이 같으면 그만큼 안 써도 된다."""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def merge(existing: list, new_rows: list, brands: list[str],
          start: str, end: str) -> tuple[list, int]:
    """구간 안 + 수집 브랜드 행만 걷어내고 새 데이터로 교체.

    반환: (병합된 전체 행, 보존된 행수)
    """
    brand_set = {b.strip() for b in brands}
    kept = []
    for r in existing:
        d = parse_date(r[COL_DATE])
        if d and start <= d <= end and str(r[COL_BRAND]).strip() in brand_set:
            continue          # 교체 대상 -> 버림
        kept.append(r)        # 그 외는 전부 보존

    merged = kept + new_rows
    merged.sort(key=lambda r: (parse_date(r[COL_DATE]) or "9999-99-99",
                               str(r[COL_DATE])))
    return merged, len(kept)


def write(ws, header: list, rows: list, prev_count: int,
          fill_formulas: bool = False, existing: list | None = None) -> int:
    """병합 결과를 청크 단위로 기록. 변경되지 않은 앞부분은 건너뛴다.

    반환: 실제로 기록한 행 수
    """
    n = len(rows)
    if ws.row_count < n + 1:
        retry(ws.add_rows, n + 1 - ws.row_count + 200)

    if header:
        retry(ws.update, values=[header], range_name="A1",
              value_input_option="USER_ENTERED")

    # 구간 밖 과거 데이터는 대개 그대로다. 달라지는 지점부터만 쓴다.
    skip = _first_diff(existing, rows) if existing else 0
    if skip:
        print(f"  앞 {skip:,}행 동일 - 기록 생략", flush=True)

    written = 0
    for i in range(skip, n, WRITE_CHUNK):
        block = rows[i:i + WRITE_CHUNK]
        r1 = i + 2
        r2 = r1 + len(block) - 1
        retry(ws.update, values=block, range_name=f"A{r1}:N{r2}",
              value_input_option="USER_ENTERED")
        written += len(block)
        print(f"  기록 {r1:,}~{r2:,}행", flush=True)

    if fill_formulas and n:
        for i in range(0, n, WRITE_CHUNK):
            cnt = len(rows[i:i + WRITE_CHUNK])
            r1 = i + 2
            block = [[FORMULA_TEMPLATES[c].format(r=r1 + j) for c in "OPQRSTUV"]
                     for j in range(cnt)]
            retry(ws.update, values=block, range_name=f"O{r1}:V{r1 + cnt - 1}",
                  value_input_option="USER_ENTERED")

    # 행이 줄었으면 아래쪽 잔재 제거 (수식 열까지 함께)
    if prev_count > n:
        retry(ws.batch_clear, [f"A{n + 2}:V{prev_count + 1}"])

    return written


# 파이썬이 값으로 쓸 수 있는 보조 열의 최소 위치 (0-based).
# A~N 은 수집 데이터, O~V 는 시트 수식 영역이라 보조 열은 W(22) 이후만 허용한다.
MIN_AUX_COL = 22
PROTECTED_HEADERS = {"상품분류", "매출구분", "셀러명"}


def col_letter(idx: int) -> str:
    """0-based 열 번호 -> A1 표기 열 문자."""
    s, n = "", idx + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def find_aux_column(ws, header_name: str) -> str:
    """1행에서 header_name 열을 찾아 열 문자를 돌려준다.

    O~V 이전 열이거나 보호 헤더면 거부한다 (파이썬이 O열 등에 쓰는 사고 방지).
    """
    row = (retry(ws.get, "1:1") or [[]])[0]
    names = [" ".join(str(h).split()) for h in row]
    hits = [i for i, h in enumerate(names) if h == header_name]
    if len(hits) != 1:
        raise RuntimeError(f"1행에 '{header_name}' 헤더가 {len(hits)}개입니다 (1개 필요)")
    idx = hits[0]
    if idx < MIN_AUX_COL or names[idx] in PROTECTED_HEADERS:
        raise RuntimeError(f"'{header_name}' 가 {col_letter(idx)}열에 있습니다 — "
                           f"W열 이후만 기록 허용 (A~V 는 수집·수식 영역)")
    return col_letter(idx)


def read_column(ws, col: str, n: int) -> list[str]:
    """2행~n+1행의 한 열을 청크로 읽는다."""
    out: list[str] = []
    start, last = 2, n + 1
    while start <= last:
        end = min(start + READ_CHUNK - 1, last)
        chunk = retry(ws.get, f"{col}{start}:{col}{end}") or []
        vals = [str(r[0]) if r else "" for r in chunk]
        vals += [""] * ((end - start + 1) - len(vals))
        out.extend(vals)
        start = end + 1
    return out[:max(n, 0)]


def write_aux_column(ws, col: str, values: list[str], prev_count: int) -> int:
    """보조 열(2행부터)을 값으로 기록. 앞부분이 같으면 건너뛰고,
    행이 줄었으면 아래 잔재를 지운다. 반환: 기록한 행 수"""
    n = len(values)
    if ws.row_count < n + 1:
        retry(ws.add_rows, n + 1 - ws.row_count + 200)
    existing = read_column(ws, col, prev_count) if prev_count else []
    skip = _first_diff(existing, values) if existing else 0
    if skip:
        print(f"  {col}열 앞 {skip:,}행 동일 - 기록 생략", flush=True)
    written = 0
    for i in range(skip, n, WRITE_CHUNK):
        block = [[v] for v in values[i:i + WRITE_CHUNK]]
        r1 = i + 2
        retry(ws.update, values=block, range_name=f"{col}{r1}:{col}{r1 + len(block) - 1}",
              value_input_option="RAW")
        written += len(block)
    if prev_count > n:
        retry(ws.batch_clear, [f"{col}{n + 2}:{col}{prev_count + 1}"])
    return written


def check_mappings(ws, rows: list[list], brands: list[str],
                   start: str, end: str, **kwargs) -> dict:
    """수식 계산을 기다린 뒤 이번 갱신분의 상품분류·매출구분을 검사한다."""
    return mapping_alert.check_worksheet(
        ws,
        rows,
        brands,
        start,
        end,
        retry_fn=retry,
        **kwargs,
    )
