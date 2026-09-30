import unittest

from src import mapping_alert as mapping


HEADERS = [
    "수집일자", "날짜", "주문서 ID", "주문상태", "브랜드", "채널",
    "제품명", "옵션명", "매출", "결제", "수량", "원가",
    "판매자 부담 배송비", "수수료", "상품분류", "매출구분",
    "셀러명", "구분(공구여부)", "년", "월", "주", "일", "프로모션",
    "실구매옵션",
]


def sale_row(day="2026-09-16", order_id="ORDER-1", status="배송준비중",
             brand="코즈코즈", product="코즈코즈 두부베개",
             option="두부베개 1개"):
    row = [""] * 14
    row[0] = day
    row[1] = day + " 10:00:00"
    row[2] = order_id
    row[3] = status
    row[4] = brand
    row[6] = product
    row[7] = option
    return row


class FakeWorksheet:
    col_count = 24

    def __init__(self, snapshots, headers=None, formula_product="=ARRAYFORMULA(G2:G)",
                 formula_sales="=ARRAYFORMULA(D2:D)"):
        self.snapshots = list(snapshots)
        self.headers = list(headers or HEADERS)
        self.formula_product = formula_product
        self.formula_sales = formula_sales
        self.value_reads = 0

    def get(self, range_name, value_render_option=None):
        if range_name.startswith("A1:"):
            formulas = [""] * len(self.headers)
            if "상품분류" in self.headers:
                formulas[self.headers.index("상품분류")] = self.formula_product
            if "매출구분" in self.headers:
                formulas[self.headers.index("매출구분")] = self.formula_sales
            return [self.headers, formulas]

        index = min(self.value_reads, len(self.snapshots) - 1)
        self.value_reads += 1
        return self.snapshots[index]


class MappingAlertTests(unittest.TestCase):

    def test_zero_and_dash_are_not_arbitrarily_treated_as_missing(self):
        self.assertFalse(mapping.is_missing_result(0))
        self.assertFalse(mapping.is_missing_result("-"))
        self.assertTrue(mapping.is_missing_result("  "))
        self.assertTrue(mapping.is_missing_result("#N/A"))

    def test_scope_and_grouping_use_only_current_period_and_brand(self):
        rows = [
            sale_row(product="[시크릿] 시즌오프 커버", option="냉감베개커버"),
            sale_row(order_id="ORDER-2", status="새 주문상태"),
            sale_row(day="2026-06-30", order_id="OLD", product="과거 상품"),
            sale_row(order_id="OTHER", brand="수면공감", product="다른 브랜드"),
        ]
        scope = mapping.scoped_rows(
            rows, ["코즈코즈"], "2026-07-01", "2026-09-16")
        values = {
            2: {"상품분류": "", "매출구분": "매출"},
            3: {"상품분류": "두부베개", "매출구분": "미매핑"},
        }

        report = mapping.analyze_mapping_results(scope, values)

        self.assertEqual(report["counts"], {"상품분류": 1, "매출구분": 1})
        self.assertEqual(report["scanned_rows"], 2)
        product = report["groups"]["상품분류"][0]
        self.assertEqual(product["source"], "[시크릿] 시즌오프 커버")
        self.assertEqual(product["contexts"], ["냉감베개커버"])

    def test_recalculation_blank_is_not_reported_if_it_later_maps(self):
        ws = FakeWorksheet([
            [["", ""]],
            [["두부베개", "매출"]],
            [["두부베개", "매출"]],
        ])

        report = mapping.check_worksheet(
            ws, [sale_row()], ["코즈코즈"], "2026-09-16", "2026-09-16",
            initial_wait=0, poll_interval=0, stable_reads=2,
            sleep_fn=lambda _seconds: None,
        )

        self.assertEqual(report["status"], "ok")
        self.assertGreaterEqual(ws.value_reads, 3)

    def test_stable_blank_and_literal_unmapped_are_alerted(self):
        ws = FakeWorksheet([[['', '미매핑']]] * 3)

        report = mapping.check_worksheet(
            ws,
            [sale_row(status="새 주문상태", product="신상품", option="신옵션")],
            ["코즈코즈"], "2026-09-16", "2026-09-16",
            initial_wait=0, poll_interval=0, stable_reads=3,
            sleep_fn=lambda _seconds: None,
        )

        self.assertEqual(report["status"], "unmapped")
        self.assertEqual(report["counts"], {"상품분류": 1, "매출구분": 1})
        message = mapping.format_webhook(
            report, "씨그로 2개월_리프레시", "2026-09-16", "2026-09-16",
            ["코즈코즈"],
        )
        self.assertIn("상품분류 1행 · 매출구분 1행", message)
        self.assertIn("신상품", message)
        self.assertIn("새 주문상태", message)

    def test_formula_error_is_an_unmapped_result(self):
        scope = mapping.scoped_rows(
            [sale_row()], ["코즈코즈"], "2026-09-16", "2026-09-16")
        report = mapping.analyze_mapping_results(scope, {
            2: {"상품분류": "#REF!", "매출구분": "매출"},
        })
        self.assertEqual(report["counts"]["상품분류"], 1)

    def test_missing_header_or_formula_fails_closed(self):
        without_header = [value for value in HEADERS if value != "매출구분"]
        with self.assertRaisesRegex(mapping.MappingCheckError, "매출구분"):
            mapping.check_worksheet(
                FakeWorksheet([[['두부베개', '매출']]], headers=without_header),
                [sale_row()], ["코즈코즈"], "2026-09-16", "2026-09-16",
                initial_wait=0, poll_interval=0,
            )

        with self.assertRaisesRegex(mapping.MappingCheckError, "2행 수식"):
            mapping.check_worksheet(
                FakeWorksheet([[['두부베개', '매출']]], formula_product=""),
                [sale_row()], ["코즈코즈"], "2026-09-16", "2026-09-16",
                initial_wait=0, poll_interval=0,
            )

    def test_permanent_blank_sales_mapping_is_calculation_failure(self):
        ws = FakeWorksheet([[['두부베개', '']]] * 5)
        with self.assertRaisesRegex(mapping.MappingCheckError, "제한시간 초과"):
            mapping.check_worksheet(
                ws, [sale_row()], ["코즈코즈"], "2026-09-16", "2026-09-16",
                initial_wait=0, poll_interval=0, stable_reads=3,
                sleep_fn=lambda _seconds: None,
            )

    def test_message_caps_unique_examples(self):
        rows = [sale_row(order_id=f"O-{i}", product=f"신상품 {i}") for i in range(12)]
        scope = mapping.scoped_rows(
            rows, ["코즈코즈"], "2026-09-16", "2026-09-16")
        values = {
            item["sheet_row"]: {"상품분류": "", "매출구분": "매출"}
            for item in scope
        }
        report = mapping.analyze_mapping_results(scope, values)
        message = mapping.format_webhook(
            report, "씨그로 2개월_리프레시", "2026-09-16", "2026-09-16",
            ["코즈코즈"], max_groups=10,
        )
        self.assertEqual(message.count("• 상품분류 ←"), 10)
        self.assertIn("그 외 2개 원본값", message)

    def test_message_shows_multiple_option_examples_for_same_product(self):
        rows = [
            sale_row(order_id="A", product="시즌오프 커버", option="냉감베개커버"),
            sale_row(order_id="B", product="시즌오프 커버", option="러플 베개커버"),
        ]
        scope = mapping.scoped_rows(
            rows, ["코즈코즈"], "2026-09-16", "2026-09-16")
        values = {
            item["sheet_row"]: {"상품분류": "", "매출구분": "매출"}
            for item in scope
        }
        report = mapping.analyze_mapping_results(scope, values)
        message = mapping.format_webhook(
            report, "씨그로 2개월_리프레시", "2026-09-16", "2026-09-16",
            ["코즈코즈"],
        )
        self.assertIn("냉감베개커버", message)
        self.assertIn("러플 베개커버", message)


if __name__ == "__main__":
    unittest.main()
