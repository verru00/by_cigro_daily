"""실구매옵션(X열) 분류 — '상품분류규칙' 탭 규칙을 G 제품명·H 옵션명에 적용한다.

용도: 사람이 보는 참고용. 실제로 어떤 상품을 샀는지 표시한다.
      수익표·정산은 이 열을 읽지 않는다 (수익표는 O열 상품분류만 사용).

※ O열(상품분류)은 시트 수식 담당이며 이 모듈은 절대 O열에 쓰지 않는다.
※ 규칙은 '상품매핑' 탭이 아니라 '상품분류규칙' 탭에서 읽는다.
  (상품매핑 D→E 는 수익표분리 판정이 읽는다 — 건드리지 않는다)

탭 구조 (1행 헤더, 열 순서 무관 · 헤더 이름으로 찾음)
    순서 | 검색패턴(또는 제품명패턴) | 옵션명패턴 | 추출값 | (비고 등 나머지는 무시)

- 검색패턴은 G 제품명, 옵션명패턴은 H 옵션명에서 찾는다 (정규식, 대소문자 무시).
- 둘 다 채워져 있으면 둘 다 맞아야 걸린다. 비어 있는 쪽은 검사하지 않는다.
- 순서 오름차순으로 검사해 처음 걸린 추출값을 쓴다.
- 어느 규칙에도 안 걸리면 '미분류'. 제품명이 비어 있으면 빈 값.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

UNCLASSIFIED = "미분류"

HEADER_ALIASES = {
    "order":   ("순서",),
    "product": ("검색패턴", "제품명패턴"),
    "option":  ("옵션명패턴", "옵션패턴"),
    "value":   ("추출값", "분류값"),
}

COL_PRODUCT = 6   # G 제품명 (0-based)
COL_OPTION  = 7   # H 옵션명


class RuleError(RuntimeError):
    """규칙 표를 읽을 수 없거나 정규식이 잘못됐을 때."""


@dataclass
class Rule:
    order: int
    product_re: re.Pattern | None
    option_re: re.Pattern | None
    value: str
    sheet_row: int

    def matches(self, product: str, option: str) -> bool:
        if self.product_re is not None and not self.product_re.search(product):
            return False
        if self.option_re is not None and not self.option_re.search(option):
            return False
        return True


def _norm(v) -> str:
    return " ".join(str(v or "").split())


def _compile(pattern: str, row: int, label: str) -> re.Pattern | None:
    pattern = pattern.strip()
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise RuleError(f"규칙 {row}행 {label} 정규식 오류: {pattern!r} ({e})")


def parse_rules(values: list[list]) -> list[Rule]:
    if not values:
        raise RuleError("규칙 탭이 비어 있습니다")
    header = [_norm(h) for h in values[0]]
    pos: dict[str, int] = {}
    for key, aliases in HEADER_ALIASES.items():
        hits = [i for i, h in enumerate(header) if h in aliases]
        if key == "option" and not hits:
            continue
        if len(hits) != 1:
            raise RuleError(f"규칙 탭 1행 헤더 '{'/'.join(aliases)}' 가 {len(hits)}개입니다 (1개 필요)")
        pos[key] = hits[0]

    def cell(row, key):
        i = pos.get(key)
        return _norm(row[i]) if i is not None and i < len(row) else ""

    rules: list[Rule] = []
    for offset, row in enumerate(values[1:], start=2):
        product, option, value = cell(row, "product"), cell(row, "option"), cell(row, "value")
        if not product and not option:
            continue                                    # 빈 행 / 순서만 적어둔 행
        if not value:
            raise RuleError(f"규칙 {offset}행: 패턴은 있는데 추출값이 비어 있습니다")
        order_text = cell(row, "order")
        try:
            order = int(float(order_text)) if order_text else 10_000 + offset
        except ValueError:
            raise RuleError(f"규칙 {offset}행: 순서 '{order_text}' 가 숫자가 아닙니다")
        rules.append(Rule(order, _compile(product, offset, "검색패턴"),
                          _compile(option, offset, "옵션명패턴"), value, offset))
    if not rules:
        raise RuleError("규칙 탭에 유효한 규칙이 없습니다")
    rules.sort(key=lambda r: (r.order, r.sheet_row))
    return rules


def load_rules(ws) -> list[Rule]:
    return parse_rules(ws.get("A1:Z500") or [])


def classify(rules: list[Rule], product, option) -> str:
    product, option = _norm(product), _norm(option)
    if not product:
        return ""
    for rule in rules:
        if rule.matches(product, option):
            return rule.value
    return UNCLASSIFIED


def classify_rows(rules: list[Rule], rows: list[list]) -> list[str]:
    out = []
    for row in rows:
        product = row[COL_PRODUCT] if len(row) > COL_PRODUCT else ""
        option = row[COL_OPTION] if len(row) > COL_OPTION else ""
        out.append(classify(rules, product, option))
    return out


def summarize(rows: list[list], values: list[str], limit: int = 8) -> dict:
    counts: dict[str, int] = {}
    unclassified: dict[str, int] = {}
    for row, value in zip(rows, values):
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
        if value == UNCLASSIFIED:
            name = _norm(row[COL_PRODUCT] if len(row) > COL_PRODUCT else "")
            unclassified[name] = unclassified.get(name, 0) + 1
    top = sorted(unclassified.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return {"total": sum(counts.values()), "unclassified": counts.get(UNCLASSIFIED, 0),
            "by_value": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
            "unclassified_top": top, "unclassified_kinds": len(unclassified)}


def format_summary(summary: dict, rule_count: int, label: str) -> str:
    u = summary["unclassified"]
    if not u:
        return f"{label}: 규칙 {rule_count}개 · 미분류 0행"
    lines = [f"⚠️ {label}: 규칙 {rule_count}개 · 미분류 {u:,}행 ({summary['unclassified_kinds']}개 제품명)"]
    for name, n in summary["unclassified_top"]:
        lines.append(f"  • {name if len(name) <= 60 else name[:59] + '…'} — {n:,}행")
    rest = summary["unclassified_kinds"] - len(summary["unclassified_top"])
    if rest > 0:
        lines.append(f"  • 그 외 {rest}개 제품명")
    lines.append("  → 상품분류규칙 탭에 규칙을 추가하면 다음 실행에서 반영됩니다 (수익표 영향 없음)")
    return "\n".join(lines)
