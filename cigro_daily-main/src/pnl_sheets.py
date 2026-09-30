"""손익(핵심이익지표) 탭 병합 기록.

주문·광고 시트와 같은 원칙: 시트를 통째로 지우지 않는다.
수집 기간에 해당하는 날짜 행만 교체하고 나머지는 보존한다.

광고와 다른 점
  - 교체 기준이 '날짜 목록'이 아니라 '기간 범위'다 (두 달치를 한 번에 받는다).
  - 탭이 3개(공헌이익·매출이익·순이익)이고 열 구성이 서로 다르다.
    그래서 관리 열 수를 고정하지 않고 각 탭의 헤더 길이를 따른다.
  - 날짜 열 이름이 엑셀마다 다를 수 있어 후보 목록으로 찾는다.
"""
from datetime import date, timedelta

import gspread

from . import config as C
from .ads_sheets import parse_date
from .sheets import client, retry  # noqa: F401

WRITE_CHUNK = 5000
COL_DATE = 0          # A열 = 날짜


def worksheet(gc, sheet_id: str, tab: str):
    sh = gc.open_by_key(sheet_id)
    try:
        return sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        raise SystemExit(f"탭을 찾을 수 없습니다: '{tab}' (PNL_TAB_* 환경변수 확인)")


def find_date_column(df) -> str | None:
    """엑셀에서 날짜 열 이름을 찾는다. 없으면 None."""
    cols = {str(c).strip(): c for c in df.columns}
    for cand in C.PNL_DATE_HEADERS:
        if cand in cols:
            return cols[cand]
    # 후보에 없으면 '날짜'가 포함된 열을 찾아본다
    for name, col in cols.items():
        if "날짜" in name or "일자" in name:
            return col
    return None


def _pad(row, n: int) -> list:
    return (list(row) + [""] * n)[:n]


def read_existing(ws) -> tuple[list, list]:
    """헤더와 데이터 행을 읽는다."""
    values = retry(ws.get_all_values)
    if not values:
        return [], []
    header = values[0]
    width = len(header)
    rows = [_pad(r, width) for r in values[1:] if any(str(c).strip() for c in r)]
    return header, rows


def check_columns(df, header: list) -> list[str]:
    """시트 헤더 중 엑셀에 없는 열 이름을 반환.

    A열(날짜)과 파생 열(년·월·주·일)은 엑셀에 없어도 정상이므로 제외한다.
    """
    cols = {str(c).strip() for c in df.columns}
    # 수식 열(공헌이익(VAT포함)·영업이익(VAT포함))은 시트가 수식으로 들고 있는
    # 값이라 엑셀에 있을 리 없다. 이 예외를 빠뜨려서 다운로드까지 성공하고도
    # '엑셀에 없는 시트 열' 로 중단된 적이 있다.
    skip = set(C.PNL_FORMULA_TEMPLATES)
    if C.PNL_FILL_DATEPARTS:
        skip |= set(C.PNL_DATEPART_HEADERS)
    return [str(h).strip() for h in header[1:]
            if str(h).strip() and str(h).strip() not in cols
            and str(h).strip() not in skip]


def date_parts(day: str) -> dict:
    """'YYYY-MM-DD' -> {년, 월, 주, 일}. 주는 월요일 시작(WEEKNUM type 2)과 동일."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return {}
    # 구글시트 WEEKNUM(date, 2) = 월요일 시작. 1월 1일이 속한 주가 1주차.
    jan1 = date(d.year, 1, 1)
    week = ((d - jan1).days + jan1.weekday()) // 7 + 1
    return {"년": d.year, "월": d.month, "주": week, "일": d.day}


def to_rows(df, header: list, date_col) -> list[list]:
    """엑셀 DataFrame -> 시트 행. A열은 날짜로 정규화해 채운다.

    헤더에 '년·월·주·일' 칸이 있으면 파이썬이 값으로 채운다(PNL_FILL_DATEPARTS).
    ARRAYFORMULA 로 두면 행이 늘수록 시트가 느려지기 때문이다.
    """
    cols = {str(c).strip(): c for c in df.columns}
    width = len(header)
    parts_on = C.PNL_FILL_DATEPARTS
    part_names = set(C.PNL_DATEPART_HEADERS)
    out = []
    for _, row in df.iterrows():
        day = parse_date(row[date_col])
        if not day:
            continue                      # 날짜를 못 읽는 행은 버린다 (합계행 등)
        rec = [day]
        parts = date_parts(day) if parts_on else {}
        for h in header[1:]:
            h = str(h).strip()
            if h in cols:
                rec.append(str(row[cols[h]]))
            elif parts_on and h in part_names and h in parts:
                rec.append(parts[h])      # 엑셀에 없는 파생 열은 여기서 채운다
            else:
                rec.append("")
        out.append(_pad(rec, width))
    return out


def merge(existing: list, new_rows: list, start: str, end: str) -> tuple[list, int]:
    """수집 기간 안의 기존 행만 걷어내고 새 데이터로 교체.

    날짜를 못 읽는 행은 보존한다 (합계·메모 등을 날리지 않기 위해).
    반환: (병합된 전체 행, 보존된 행수)
    """
    kept = []
    for r in existing:
        d = parse_date(r[COL_DATE]) if r else ""
        if d and start <= d <= end:
            continue                      # 교체 대상
        kept.append(r)

    merged = kept + new_rows
    merged.sort(key=lambda r: parse_date(r[COL_DATE]) or "9999-99-99")
    return merged, len(kept)


def _first_diff(a: list, b: list) -> int:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def _col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def write(ws, header: list, rows: list, prev_count: int,
          existing: list | None = None) -> int:
    """병합 결과를 기록. 변경되지 않은 앞부분은 건너뛴다."""
    n = len(rows)
    width = len(header)
    last = _col_letter(width)

    if ws.row_count < n + 1:
        retry(ws.add_rows, n + 1 - ws.row_count + 200)

    skip = _first_diff(existing, rows) if existing else 0
    if skip:
        print(f"  앞 {skip:,}행 동일 - 기록 생략", flush=True)

    # 수식 열 위치를 헤더 이름으로 찾는다. 여기에 빈칸을 쓰면 시트 수식이 지워진다.
    formula_at = {}
    for idx, h in enumerate(header):
        tpl = C.PNL_FORMULA_TEMPLATES.get(str(h).strip())
        if tpl:
            formula_at[idx] = tpl
    if formula_at:
        names = ", ".join(str(header[i]).strip() for i in sorted(formula_at))
        print(f"  수식 열 복원: {names}", flush=True)

    written = 0
    for i in range(skip, n, WRITE_CHUNK):
        block = [list(r) for r in rows[i:i + WRITE_CHUNK]]
        r1 = i + 2
        r2 = r1 + len(block) - 1
        for j, row in enumerate(block):
            for idx, tpl in formula_at.items():
                if idx < len(row):
                    row[idx] = tpl.format(r=r1 + j)
        retry(ws.update, values=block, range_name=f"A{r1}:{last}{r2}",
              value_input_option="USER_ENTERED")
        written += len(block)
        print(f"  기록 {r1:,}~{r2:,}행", flush=True)

    if prev_count > n:
        retry(ws.batch_clear, [f"A{n + 2}:{last}{prev_count + 1}"])

    return written


# ── 변동판관비 감시 ───────────────────────────────────────────────
def find_sga_column(header_or_df) -> str | None:
    """변동판관비 열 이름을 찾는다. 시트 헤더와 엑셀 열 양쪽에 쓴다."""
    if hasattr(header_or_df, "columns"):
        names = [str(c).strip() for c in header_or_df.columns]
    else:
        names = [str(c).strip() for c in header_or_df]
    for cand in C.PNL_SGA_HEADERS:
        if cand in names:
            return cand
    for n in names:
        if "판관비" in n:
            return n
    return None


def _to_num(v) -> float:
    """'1,234' '(1,234)' '-1234원' 같은 표기에서 숫자만 뽑는다."""
    t = str(v or "").strip()
    if not t:
        return 0.0
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").replace(",", "").replace("원", "").strip()
    try:
        n = float(t)
    except ValueError:
        return 0.0
    return -n if neg else n


def month_sum_rows(rows: list, header: list, col_name: str, ym: str) -> float:
    """시트 행에서 특정 월(YYYY-MM)의 해당 열 합계."""
    try:
        idx = [str(h).strip() for h in header].index(col_name)
    except ValueError:
        return 0.0
    total = 0.0
    for r in rows:
        d = parse_date(r[COL_DATE]) if r else ""
        if d.startswith(ym) and len(r) > idx:
            total += _to_num(r[idx])
    return total


def month_sum_df(df, date_col, col_name: str, ym: str) -> float:
    """엑셀 DataFrame 에서 특정 월의 해당 열 합계."""
    cols = {str(c).strip(): c for c in df.columns}
    if col_name not in cols:
        return 0.0
    total = 0.0
    for _, row in df.iterrows():
        d = parse_date(row[date_col])
        if d.startswith(ym):
            total += _to_num(row[cols[col_name]])
    return total


def sga_target_month(today: date) -> str | None:
    """오늘이 감시 구간이면 대상 월(YYYY-MM), 아니면 None.

    월초(1~PNL_SGA_UNTIL_DAY일)  -> 전월을 본다
    월말(말일 PNL_SGA_BEFORE_END일 전부터) -> 당월을 본다
    """
    import calendar
    if today.day <= C.PNL_SGA_UNTIL_DAY:
        prev = date(today.year, today.month, 1) - timedelta(days=1)
        return f"{prev.year:04d}-{prev.month:02d}"
    last = calendar.monthrange(today.year, today.month)[1]
    if last - today.day < C.PNL_SGA_BEFORE_END:
        return f"{today.year:04d}-{today.month:02d}"
    return None
