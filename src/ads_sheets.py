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


def col_letter(idx: int) -> str:
    """0-based 열 번호 -> 'A', 'W', 'AA' ..."""
    s, n = "", idx + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


# 브랜드 열을 새로 만들 때 쓰는 최소 위치. O~R(수동)·S~V(수식) 뒤 W열.
BRAND_COL_MIN = 22


def ensure_brand_col(ws, name: str) -> int:
    """1행에서 브랜드 열을 찾고, 없으면 V열 뒤 첫 빈 칸에 헤더를 만든다. 0-based 위치 반환."""
    row = retry(ws.row_values, 1) or []
    for i, h in enumerate(row):
        if str(h).strip() == name:
            return i
    idx = max(len(row), BRAND_COL_MIN)
    if ws.col_count < idx + 1:
        retry(ws.add_cols, idx + 1 - ws.col_count)
    retry(ws.update, values=[[name]], range_name=f"{col_letter(idx)}1",
          value_input_option="USER_ENTERED")
    print(f"  '{name}' 열 생성: {col_letter(idx)}열", flush=True)
    return idx


def read_existing(ws, brand_col: int | None = None):
    """A~N 영역을 청크로 나눠 읽어 (헤더, 데이터행) 반환.

    brand_col 을 주면 그 열까지 읽어 (헤더, 데이터행, 브랜드목록) 을 반환한다.
    브랜드목록은 데이터행과 같은 순서·길이다.
    """
    last = col_letter(brand_col) if brand_col is not None else "N"
    width = (brand_col + 1) if brand_col is not None else DATA_COLS
    total = ws.row_count
    values = []
    start = 1
    while start <= total:
        end = min(start + READ_CHUNK - 1, total)
        chunk = retry(ws.get, f"A{start}:{last}{end}") or []
        values.extend(chunk)
        if len(chunk) < (end - start + 1):
            break
        start = end + 1

    if not values:
        return ([], [], []) if brand_col is not None else ([], [])
    header = _pad(values[0], DATA_COLS)
    rows, brands = [], []
    for r in values[1:]:
        r = _pad(r, width)
        if not any(str(c).strip() for c in r[:DATA_COLS]):
            continue
        rows.append(r[:DATA_COLS])
        if brand_col is not None:
            brands.append(str(r[brand_col]).strip())
    if brand_col is not None:
        return header, rows, brands
    return header, rows


def check_columns(df, header: list) -> list[str]:
    """시트 헤더 B~N 중 엑셀에 없는 열 이름을 반환."""
    cols = {str(c).strip() for c in df.columns}
    return [str(h).strip() for h in header[1:] if str(h).strip() and str(h).strip() not in cols]


def filter_campaign_keyword(df, keyword: str, header: str = "캠페인"):
    """캠페인명에 keyword 가 들어간 행만 남긴다 (위치 무관). keyword 가 비면 그대로.

    캠페인 열이 없으면 전부 저장되는 사고를 막기 위해 중단한다.
    """
    if not keyword:
        return df
    cols = {str(c).strip(): c for c in df.columns}
    col = cols.get(header)
    if col is None:
        raise SystemExit(f"광고 엑셀에 '{header}' 열이 없어 '{keyword}' 필터를 적용할 수 없습니다")
    keep = df[col].astype(str).str.contains(keyword, regex=False)
    return df[keep].reset_index(drop=True)


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


def merge_branded(existing: list, existing_brands: list, new_rows: list, new_brands: list,
                  days: list[str], brands: list[str], legacy: str = "") -> tuple[list, list, int]:
    """브랜드 × 날짜 기준 교체. 수집한 (날짜, 브랜드) 행만 걷어내고 나머지는 보존.

    브랜드 칸이 빈 기존 행은 legacy 브랜드로 본다 (단일 브랜드 시절 데이터).
    반환: (병합된 행, 병합된 브랜드, 보존된 행수)
    """
    target_days, target_brands = set(days), set(brands)
    kept = []
    for row, b in zip(existing, existing_brands):
        b = b or legacy
        if parse_date(row[COL_DATE]) in target_days and b in target_brands:
            continue
        kept.append((row, b))
    items = kept + list(zip(new_rows, new_brands))
    items.sort(key=lambda it: parse_date(it[0][COL_DATE]) or "9999-99-99")   # 안정 정렬
    return [r for r, _ in items], [b for _, b in items], len(kept)


def _first_diff(a: list, b: list) -> int:
    """두 행 목록이 처음으로 달라지는 위치. 앞부분이 같으면 그만큼 안 써도 된다."""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def write(ws, header: list, rows: list, prev_count: int,
          fill_formulas: bool = False, existing: list | None = None,
          brand_col: int | None = None, brands: list | None = None,
          existing_brands: list | None = None) -> int:
    """병합 결과를 A~N 에 기록. 변경되지 않은 앞부분은 건너뛴다.

    brand_col·brands 를 주면 그 열에 브랜드를 함께 기록한다.
    반환: 실제로 기록한 행 수
    """
    n = len(rows)
    if ws.row_count < n + 1:
        retry(ws.add_rows, n + 1 - ws.row_count + 200)

    if header:
        retry(ws.update, values=[header], range_name="A1",
              value_input_option="USER_ENTERED")

    # 과거 데이터는 대개 그대로다. 달라지는 지점부터만 쓴다.
    if brand_col is not None and brands is not None:
        old = [list(r) + [b] for r, b in zip(existing or [], existing_brands or [])]
        skip = _first_diff(old, [list(r) + [b] for r, b in zip(rows, brands)]) if existing else 0
    else:
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

    # 브랜드 열
    if brand_col is not None and brands is not None and skip < n:
        L = col_letter(brand_col)
        for i in range(skip, n, WRITE_CHUNK):
            block = [[b] for b in brands[i:i + WRITE_CHUNK]]
            r1 = i + 2
            retry(ws.update, values=block, range_name=f"{L}{r1}:{L}{r1 + len(block) - 1}",
                  value_input_option="USER_ENTERED")

    # 행이 줄었으면 아래쪽 잔재 제거 (수식 열·브랜드 열까지 함께)
    if prev_count > n:
        last = col_letter(max(brand_col or 0, 21))
        retry(ws.batch_clear, [f"A{n + 2}:{last}{prev_count + 1}"])

    return written
