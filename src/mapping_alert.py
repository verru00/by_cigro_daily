"""매출 시트의 수식 매핑 누락 검사와 Google Chat 메시지 생성.

대상 시트는 A:N 원본 데이터를 파이썬이 기록하고, O 이후는 Google Sheets
수식이 계산한다. 따라서 기록 직후 한 번 읽는 대신 수식 결과가 연속으로
안정될 때까지 기다린 뒤 이번 리프레시 기간·브랜드의 행만 검사한다.
"""
from __future__ import annotations

import math
import re
import time
from collections import defaultdict
from typing import Callable


TARGETS = {
    "상품분류": {
        "source_index": 6,       # G 제품명
        "source_header": "제품명",
        "context_index": 7,      # H 옵션명
        "context_header": "옵션명",
    },
    "매출구분": {
        "source_index": 3,       # D 주문상태
        "source_header": "주문상태",
        "context_index": None,
        "context_header": "",
    },
}

MISSING_TOKENS = {"", "미매핑", "미분류"}
FORMULA_ERROR_PREFIXES = (
    "#N/A", "#REF!", "#VALUE!", "#ERROR!", "#NAME?", "#DIV/0!",
    "#NUM!", "#NULL!",
)
LOADING_PREFIXES = ("#LOADING", "로드 중", "계산 중")


class MappingCheckError(RuntimeError):
    """헤더·수식·재계산 상태 때문에 매핑 결과를 확정할 수 없을 때."""


def normalize(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def column_letter(number: int) -> str:
    """1-based 열 번호를 A1 열 문자로 바꾼다."""
    if number < 1:
        raise ValueError("열 번호는 1 이상이어야 합니다")
    out = ""
    while number:
        number, rem = divmod(number - 1, 26)
        out = chr(65 + rem) + out
    return out


def parse_date(value) -> str:
    """시트 날짜에서 비교 가능한 YYYY-MM-DD를 뽑는다."""
    m = re.match(
        r"\s*(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})",
        str(value or ""),
    )
    if not m:
        return ""
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def is_missing_result(value) -> bool:
    text = normalize(value)
    upper = text.upper()
    return (
        text in MISSING_TOKENS
        or any(upper.startswith(prefix) for prefix in FORMULA_ERROR_PREFIXES)
    )


def _is_loading(value) -> bool:
    text = normalize(value)
    upper = text.upper()
    return any(upper.startswith(prefix.upper()) for prefix in LOADING_PREFIXES)


def scoped_rows(rows: list[list], brands: list[str], start: str,
                end: str) -> list[dict]:
    """이번 실행에서 검사할 데이터행과 실제 시트 행 번호를 반환한다."""
    brand_set = {normalize(value) for value in brands if normalize(value)}
    found = []
    for data_index, original in enumerate(rows):
        row = (list(original) + [""] * 14)[:14]
        day = parse_date(row[1])       # B 날짜
        brand = normalize(row[4])      # E 브랜드
        if day and start <= day <= end and brand in brand_set:
            found.append({
                "data_index": data_index,
                "sheet_row": data_index + 2,
                "row": row,
            })
    return found


def analyze_mapping_results(scope: list[dict],
                            values_by_row: dict[int, dict[str, str]]) -> dict:
    """안정화된 O/P 값에서 미매핑 행을 원본값별로 집계한다."""
    counts = {target: 0 for target in TARGETS}
    grouped: dict[str, dict[str, dict]] = {
        target: defaultdict(dict) for target in TARGETS
    }

    for item in scope:
        row = item["row"]
        sheet_row = item["sheet_row"]
        result_row = values_by_row.get(sheet_row, {})
        for target, spec in TARGETS.items():
            source = normalize(row[spec["source_index"]])
            if not source:
                continue
            result = normalize(result_row.get(target, ""))
            if not is_missing_result(result):
                continue

            counts[target] += 1
            bucket = grouped[target].get(source)
            if not bucket:
                bucket = {
                    "source": source,
                    "source_header": spec["source_header"],
                    "count": 0,
                    "sheet_rows": [],
                    "order_ids": [],
                    "contexts": [],
                    "results": [],
                }
                grouped[target][source] = bucket
            bucket["count"] += 1
            if len(bucket["sheet_rows"]) < 3:
                bucket["sheet_rows"].append(sheet_row)
            order_id = normalize(row[2])
            if order_id and order_id not in bucket["order_ids"] \
                    and len(bucket["order_ids"]) < 2:
                bucket["order_ids"].append(order_id)
            context_index = spec["context_index"]
            if context_index is not None:
                context = normalize(row[context_index])
                if context and context not in bucket["contexts"] \
                        and len(bucket["contexts"]) < 2:
                    bucket["contexts"].append(context)
            shown_result = result or "공란"
            if shown_result not in bucket["results"]:
                bucket["results"].append(shown_result)

    groups = {
        target: sorted(items.values(), key=lambda x: (-x["count"], x["source"]))
        for target, items in grouped.items()
    }
    total = sum(counts.values())
    return {
        "status": "unmapped" if total else "ok",
        "scanned_rows": len(scope),
        "total": total,
        "counts": counts,
        "groups": groups,
    }


def _call(retry_fn: Callable | None, fn: Callable, *args, **kwargs):
    if retry_fn:
        return retry_fn(fn, *args, **kwargs)
    return fn(*args, **kwargs)


def _pad_matrix(values, rows: int, cols: int) -> list[list]:
    matrix = []
    for row in list(values or [])[:rows]:
        matrix.append((list(row) + [""] * cols)[:cols])
    matrix.extend([[""] * cols for _ in range(rows - len(matrix))])
    return matrix


def check_worksheet(ws, rows: list[list], brands: list[str], start: str,
                    end: str, *, retry_fn: Callable | None = None,
                    initial_wait: float = 5.0, poll_interval: float = 2.0,
                    timeout_seconds: float = 45.0, stable_reads: int = 3,
                    sleep_fn: Callable[[float], None] = time.sleep) -> dict:
    """Google Sheets 수식 결과가 안정된 뒤 매핑 누락을 검사한다.

    ``rows`` 는 시트에 기록한 A:N 병합 결과와 같은 순서여야 한다.
    매출구분(P)은 원본 주문상태가 있으면 정상 결과 또는 ``미매핑``을
    반드시 내므로, P가 공란인 동안은 재계산 중/수식 손상으로 보고 기다린다.
    """
    try:
        column_count = max(24, int(getattr(ws, "col_count", 24) or 24))
    except (TypeError, ValueError):
        column_count = 24
    end_column = column_letter(column_count)
    top = _call(
        retry_fn,
        ws.get,
        f"A1:{end_column}2",
        value_render_option="FORMULA",
    ) or []
    header = [normalize(value) for value in (top[0] if top else [])]
    formula_row = list(top[1]) if len(top) > 1 else []

    positions: dict[str, int] = {}
    for target in TARGETS:
        matches = [index for index, value in enumerate(header) if value == target]
        if len(matches) != 1:
            raise MappingCheckError(
                f"1행 헤더 '{target}'가 {len(matches)}개입니다 (정확히 1개 필요)"
            )
        positions[target] = matches[0]

    formula_row = (formula_row + [""] * len(header))[:len(header)]
    missing_formulas = [
        target for target, index in positions.items()
        if not normalize(formula_row[index]).startswith("=")
    ]
    if missing_formulas:
        raise MappingCheckError(
            f"2행 수식이 없습니다: {', '.join(missing_formulas)}"
        )

    scope = scoped_rows(rows, brands, start, end)
    if not scope:
        return analyze_mapping_results([], {})

    first_sheet_row = scope[0]["sheet_row"]
    last_sheet_row = scope[-1]["sheet_row"]
    left_index = min(positions.values())
    right_index = max(positions.values())
    width = right_index - left_index + 1
    value_range = (
        f"{column_letter(left_index + 1)}{first_sheet_row}:"
        f"{column_letter(right_index + 1)}{last_sheet_row}"
    )
    expected_rows = last_sheet_row - first_sheet_row + 1

    if initial_wait > 0:
        sleep_fn(initial_wait)
    if poll_interval > 0:
        attempts = max(stable_reads, math.ceil(timeout_seconds / poll_interval) + 1)
    else:
        attempts = max(stable_reads + 2, 5)

    previous = None
    stable = 0
    pending_count = 0
    values_by_row: dict[int, dict[str, str]] = {}
    for attempt in range(attempts):
        raw = _call(
            retry_fn,
            ws.get,
            value_range,
            value_render_option="FORMATTED_VALUE",
        ) or []
        matrix = _pad_matrix(raw, expected_rows, width)
        values_by_row = {}
        snapshot = []
        pending_count = 0
        for item in scope:
            sheet_row = item["sheet_row"]
            cells = matrix[sheet_row - first_sheet_row]
            mapped = {
                target: normalize(cells[index - left_index])
                for target, index in positions.items()
            }
            values_by_row[sheet_row] = mapped
            snapshot.append((sheet_row,) + tuple(mapped[target] for target in TARGETS))

            # 라이브 수식은 D 주문상태가 있으면 P 매출구분을 항상 반환한다.
            sales_source = normalize(item["row"][TARGETS["매출구분"]["source_index"]])
            sales_result = mapped["매출구분"]
            row_pending = (
                sales_source and (not sales_result or _is_loading(sales_result))
            ) or any(_is_loading(value) for value in mapped.values())
            if row_pending:
                pending_count += 1

        frozen = tuple(snapshot)
        if pending_count:
            stable = 0
            previous = None
        else:
            stable = stable + 1 if frozen == previous else 1
            previous = frozen
            if stable >= stable_reads:
                return analyze_mapping_results(scope, values_by_row)

        if attempt < attempts - 1 and poll_interval > 0:
            sleep_fn(poll_interval)

    if pending_count:
        raise MappingCheckError(
            f"수식 계산 제한시간 초과: 매출구분 공란/계산 중 {pending_count:,}행"
        )
    raise MappingCheckError(
        f"수식 결과가 연속 {stable_reads}회 안정되지 않았습니다"
    )


def _shorten(value: str, limit: int) -> str:
    value = normalize(value)
    return value if len(value) <= limit else value[:limit - 1] + "…"


def format_webhook(report: dict, tab: str, start: str, end: str,
                   brands: list[str], max_groups: int = 10) -> str:
    """미매핑 결과를 길이 제한이 있는 Google Chat용 텍스트로 만든다."""
    if report.get("status") != "unmapped":
        return ""

    counts = report["counts"]
    lines = [
        "⚠️ cigro 매출 매핑 누락",
        f"탭: {tab}",
        f"기간: {start} ~ {end}",
        f"브랜드: {', '.join(brands)}",
        (f"검사 {report['scanned_rows']:,}행 · "
         f"상품분류 {counts.get('상품분류', 0):,}행 · "
         f"매출구분 {counts.get('매출구분', 0):,}행"),
    ]

    all_groups = []
    for target in ("상품분류", "매출구분"):
        all_groups.extend((target, group) for group in report["groups"].get(target, []))
    all_groups.sort(key=lambda item: (-item[1]["count"], item[0], item[1]["source"]))

    for target, group in all_groups[:max_groups]:
        source = _shorten(group["source"], 80)
        rows = ", ".join(str(value) for value in group["sheet_rows"])
        result = "/".join(group["results"])
        line = (
            f"• {target} ← {group['source_header']}: {source} "
            f"— {group['count']:,}행 (결과 {result}, 시트 {rows}행)"
        )
        if group["contexts"]:
            contexts = " / ".join(_shorten(value, 60) for value in group["contexts"])
            line += f" / 옵션 예: {contexts}"
        lines.append(line)

    hidden = len(all_groups) - min(len(all_groups), max_groups)
    if hidden:
        lines.append(f"• 그 외 {hidden:,}개 원본값")
    lines.append("상품분류 수식 또는 appendix 매출구분 표를 확인해 주세요.")
    return "\n".join(lines)
