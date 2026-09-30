# -*- coding: utf-8 -*-
"""과거 상품분류(O)를 주문서 ID 로 보존하는지.

손으로 맞춰둔 값을 매일 규칙으로 덮으면 안 된다. 병합은 날짜순으로
다시 정렬해 행이 밀리고, 수집 구간 안 행은 원본이 통째로 교체된다.
행 번호로는 못 따라간다. C열 주문서 ID(+제품명+옵션명)로 찍는다.
"""
import unittest

from src import sheets


def row(date, oid, name, opt=""):
    r = [""] * 14
    r[sheets.COL_DATE] = date
    r[sheets.COL_ORDER] = oid
    r[sheets.COL_BRAND] = "코즈코즈"
    r[sheets.COL_PRODUCT] = name
    r[sheets.COL_OPTION] = opt
    return r


def preserve(existing, prev_class, merged, fresh):
    """main.py 가 하는 것과 같은 합치기."""
    known = sheets.class_by_key(existing, prev_class)
    out, new_idx = [], []
    for i, (r, new) in enumerate(zip(merged, fresh)):
        old = known.get(sheets.row_key(r), "")
        out.append(old or new)
        if not old:
            new_idx.append(i)
    return out, new_idx


class PreserveByOrderIdTest(unittest.TestCase):

    def test_행이_밀려도_값이_따라간다(self):
        existing = [row("2026-09-01", "A1", "옛상품"), row("2026-09-05", "B2", "옛상품")]
        prev = ["수기A", "수기B"]
        # 9/02, 9/03 이 새로 들어와 9/05 행이 2행 -> 4행으로 밀린다
        merged, _ = sheets.merge(existing,
                                 [row("2026-09-02", "N1", "신규"), row("2026-09-03", "N2", "신규")],
                                 ["코즈코즈"], "2026-09-02", "2026-09-03")
        out, new_idx = preserve(existing, prev, merged, ["규칙"] * 4)
        self.assertEqual(out, ["수기A", "규칙", "규칙", "수기B"])
        self.assertEqual(new_idx, [1, 2])

    def test_구간_안_행도_같은_ID_면_보존(self):
        """원본(A~N)이 교체돼도 주문서 ID 가 같으면 손으로 고친 분류가 남는다."""
        existing = [row("2026-09-10", "X9", "두부토퍼, 두부베개", "[단품] 두부베개")]
        prev = ["두부베개"]                       # 손으로 고쳐둔 값
        new_rows = [row("2026-09-10", "X9", "두부토퍼, 두부베개", "[단품] 두부베개")]
        merged, kept = sheets.merge(existing, new_rows, ["코즈코즈"], "2026-09-10", "2026-09-10")
        self.assertEqual(kept, 0)                 # 원본은 교체됐지만
        out, new_idx = preserve(existing, prev, merged, ["두부토퍼"])   # 규칙은 틀리게 말해도
        self.assertEqual(out, ["두부베개"])       # 손으로 고친 값이 남는다
        self.assertEqual(new_idx, [])

    def test_한_주문에_여러_상품이면_제품명으로_구분(self):
        existing = [row("2026-09-01", "S1", "두부토퍼", "Q"), row("2026-09-01", "S1", "두부베개", "1EA")]
        prev = ["두부토퍼", "두부베개"]
        out, _ = preserve(existing, prev, existing, ["X", "X"])
        self.assertEqual(out, ["두부토퍼", "두부베개"])

    def test_빈_칸은_다시_분류(self):
        existing = [row("2026-09-01", "A1", "상품")]
        out, new_idx = preserve(existing, [""], existing, ["규칙값"])
        self.assertEqual(out, ["규칙값"])
        self.assertEqual(new_idx, [0])

    def test_새_주문은_규칙으로(self):
        existing = [row("2026-09-01", "A1", "상품")]
        merged = existing + [row("2026-09-02", "B2", "상품")]
        out, new_idx = preserve(existing, ["수기"], merged, ["규칙", "규칙"])
        self.assertEqual(out, ["수기", "규칙"])
        self.assertEqual(new_idx, [1])


if __name__ == "__main__":
    unittest.main()
