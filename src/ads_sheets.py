"""'광고 RAW' 탭 병합 기록.

주문 시트(sheets.py)와 같은 원칙: 시트를 통째로 지우지 않는다.
수집한 날짜(A열 수집일자)에 해당하는 행만 교체하고 나머지는 보존한다.

관리 범위는 A~N. O~V(셀러명·상품분류·년월주일 수식)는 건드리지 않는다.

주문과 다른 점
  - 교체 기준이 브랜드+날짜가 아니라 날짜 하나다 (탭이 단일 브랜드 전용).
  - A열 수집일자는 cigro 엑셀에 없다. 다운로드한 날짜를 직접 채운다.
"""
import re

import gspread

from . import config as C
# client: 서비스 계정 인증 (주문과 동일)
# retry:  429/5xx 지수 백오프
# ads_main 이 ads_sheets.client() 로 부르므로 여기서 재노출한다.
from .sheets import client, retry  # noqa: F401

DATA_COLS = C.ADS_DATA_COLS      # A..N
COL_DATE = 0                     # A 수집일자

READ_CHUNK = 20000
WRITE_CHUNK = 5000

# S~V 수식 (ADS_FILL_FORMULAS=true 일 때만). A열 수집일자 기준.
FORMULA_TEMPLATES = {
    "S": '=IF(A{r}="","",YEAR(A{r}))',
    "T": '=IF(A{r}="","",MONTH(A{r}))',
    "U": '=IF(A{r}="","",WEEKNUM(A{r},2))',
    "V": '=IF(A{r}="","",DAY(A{r}))',
}

# '2026-09-10' '2026/9/10' '2026. 9. 10' '2026.09.10' 전부 흡수.
# 시트 표시 형식이 로케일에 따라 흔들려도 날짜를 놓치지 않기 위함.
_DATE_RE = re.compile(r"(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})")


def parse_date(v) -> str:
    """다양한 표기에서 'YYYY-MM-DD' 만 뽑는다. 실패하면 빈 문자열."""
    if v is None:
        return ""
    m = _DATE_RE.match(str(v).strip())
    if not m:
        return ""
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def worksheet(gc, sheet_id: str, tab: str):
    sh = gc.open_by_key(sheet_id)
    try:
        return sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        raise SystemExit(f"탭을 찾을 수 없습니다: '{tab}' (ADS_SHEET_TAB 확인)")


def _pad(row, n: int) -> list:
    return (list(row) + [""] * n)[:n]


def read_existing(ws) -> tuple[list, list]:
    """A~N 영역을 청크로 나눠 읽어 (헤더, 데이터행) 반환."""
    total = ws.row_count
    values = []
    start = 1
    while start <= total:
        end = min(start + READ_CHUNK - 1, total)
        chunk = retry(ws.get, f"A{start}:N{end}") or []
        values.extend(chunk)
        if len(chunk) < (end - start + 1):
            break
        start = end + 1

    if not values:
        return [], []
    header = _pad(values[0], DATA_COLS)
    rows = [_pad(r, DATA_COLS) for r in values[1:]
            if any(str(c).strip() for c in r)]
    return header, rows


def check_columns(df, header: list) -> list[str]:
    """시트 헤더 B~N 중 엑셀에 없는 열 이름을 반환."""
    cols = {str(c).strip() for c in df.columns}
    return [str(h).strip() for h in header[1:] if str(h).strip() and str(h).strip() not in cols]


def to_rows(df, header: list, day: str) -> list[list]:
    """엑셀 DataFrame -> 시트 A~N 행. A(수집일자)는 day 로 채운다."""
    cols = {str(c).strip(): c for c in df.columns}
    out = []
    for _, row in df.iterrows():
        rec = [day]
        for h in header[1:]:
            h = str(h).strip()
            rec.append(str(row[cols[h]]) if h in cols else "")
        out.append(_pad(rec, DATA_COLS))
    return out


def unparseable_ratio(existing: list) -> float:
    """A열 날짜를 못 읽는 행의 비율.

    시트 표시 형식이 예상과 다르면 교체 대상을 하나도 못 골라
    같은 날 데이터가 계속 쌓인다. 그 사고를 사전에 잡기 위한 지표.
    """
    if not existing:
        return 0.0
    bad = sum(1 for r in existing if not parse_date(r[COL_DATE]))
    return bad / len(existing)


def merge(existing: list, new_rows: list, days: list[str]) -> tuple[list, int]:
    """수집한 날짜의 기존 행만 걷어내고 새 데이터로 교체.

    날짜를 못 읽는 행은 보존한다 (합계·메모 등을 날리지 않기 위해).
    반환: (병합된 전체 행, 보존된 행수)
    """
    target = set(days)
    kept = [r for r in existing if parse_date(r[COL_DATE]) not in target]

    merged = kept + new_rows
    merged.sort(key=lambda r: parse_date(r[COL_DATE]) or "9999-99-99")
    return merged, len(kept)


def _first_diff(a: list, b: list) -> int:
    """두 행 목록이 처음으로 달라지는 위치. 앞부분이 같으면 그만큼 안 써도 된다."""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def write(ws, header: list, rows: list, prev_count: int,
          fill_formulas: bool = False, existing: list | None = None) -> int:
    """병합 결과를 A~N 에 기록. 변경되지 않은 앞부분은 건너뛴다.

    반환: 실제로 기록한 행 수
    """
    n = len(rows)
    if ws.row_count < n + 1:
        retry(ws.add_rows, n + 1 - ws.row_count + 200)

    if header:
        retry(ws.update, values=[header], range_name="A1",
              value_input_option="USER_ENTERED")

    # 과거 데이터는 대개 그대로다. 달라지는 지점부터만 쓴다.
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

    # 신규 행에만 S~V 수식을 채운다 (앞부분은 이미 있음)
    if fill_formulas and n and skip < n:
        for i in range(skip, n, WRITE_CHUNK):
            cnt = len(rows[i:i + WRITE_CHUNK])
            r1 = i + 2
            block = [[FORMULA_TEMPLATES[c].format(r=r1 + j) for c in "STUV"]
                     for j in range(cnt)]
            retry(ws.update, values=block, range_name=f"S{r1}:V{r1 + cnt - 1}",
                  value_input_option="USER_ENTERED")

    # 행이 줄었으면 아래쪽 잔재 제거 (수식 열까지 함께)
    if prev_count > n:
        retry(ws.batch_clear, [f"A{n + 2}:V{prev_count + 1}"])

    return written
