import unittest
from unittest import mock

from src import product_classifier as PC
from src import sheets

# 사용자가 만든 규칙 표 (2026-09-23 스크린샷 기준) — '상품분류규칙' 탭에 옮겨 쓴다
TABLE = [
    ["순서", "검색패턴", "옵션명패턴", "추출값", "비고"],
    ["1", "스트랩", "", "스트랩"],
    ["2", "필로우미스트", "", "필로우미스트"],
    ["3", "세탁바구니", "", "세탁바구니"],
    ["4", "에어컨커버", "", "에어컨커버"],
    ["5", "", "냉감.*베개커버", "냉감베개커버"],
    ["6", "", "러플.*베개커버", "러플베개커버"],
    ["7", "냉감.*베개커버", "", "냉감베개커버"],
    ["8", "러플.*베개커버", "", "러플베개커버"],
    ["9", "베개.*커버|순면커버", "", "두부베개커버"],
    ["10", "", "베개커버", "두부베개커버"],
    ["11", "모달.*패드", "", "모달패드"],
    ["12", "모달", "패드", "모달패드"],
    ["13", "러플|모달", "", "러플모달이불"],
    ["14", "토퍼", "", "두부토퍼"],
    ["15", "우유베개", "", "우유베개알파"],
    ["16", "두부베개|경추베개", "", "두부베개"],
    ["17", "프로즌|칠링버디|쿨매트|바디필로우", "", "프로즌"],
    ["18", "냉감이불", "", "스테이쿨냉감이불"],
    ["19", "냉감패드", "", "스테이쿨냉감패드"],
    ["20", "압축파우치", "", "이불압축파우치"],
    ["21"],
]


class Classify(unittest.TestCase):
    def setUp(self):
        self.rules = PC.parse_rules(TABLE)

    def c(self, p, o=""):
        return PC.classify(self.rules, p, o)

    def test_actual_purchase(self):
        self.assertEqual(len(self.rules), 20)
        self.assertEqual(self.c("코즈코즈 화이트 러플 베개커버"), "러플베개커버")
        self.assertEqual(self.c("코즈코즈 두부베개 커버(허브민트)"), "두부베개커버")
        self.assertEqual(self.c("시즌오프 커버특가", "러플 베개커버"), "러플베개커버")
        self.assertEqual(self.c("코즈코즈 두부베개", "구성 : 두부베개 + 냉감베개커버 1세트"), "냉감베개커버")
        self.assertEqual(self.c("모달 침구", "패드 S"), "모달패드")
        self.assertEqual(self.c("두부베개 토퍼"), "두부토퍼")
        self.assertEqual(self.c("신상품"), PC.UNCLASSIFIED)
        self.assertEqual(self.c(""), "")

    def test_bad_rule_fails_closed(self):
        with self.assertRaises(PC.RuleError):
            PC.parse_rules([TABLE[0], ["1", "냉감(", "", "x"]])
        with self.assertRaises(PC.RuleError):
            PC.parse_rules([TABLE[0], ["1", "냉감", "", ""]])


class FakeWS:
    def __init__(self, header, col_values=None, row_count=100):
        self.header = header
        self.col_values = col_values or []
        self.row_count = row_count
        self.updates, self.clears = [], []

    def get(self, rng):
        if rng == "1:1":
            return [self.header]
        import re
        m = re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", rng)
        a, b = int(m.group(2)), int(m.group(4))
        return [[v] for v in self.col_values[a - 2:b - 1]]

    def update(self, values=None, range_name=None, value_input_option=None):
        self.updates.append((range_name, values))

    def batch_clear(self, ranges):
        self.clears.extend(ranges)

    def add_rows(self, n):
        self.row_count += n


HEADER = (["수집일자", "날짜", "주문서 ID", "주문상태", "브랜드", "채널", "제품명", "옵션명",
           "매출", "결제", "수량", "원가", "판매자 부담 배송비", "수수료",
           "상품분류", "매출구분", "셀러명", "구분(공구여부)", "년", "월", "주", "일",
           "프로모션", "실구매옵션"])


class AuxColumn(unittest.TestCase):
    def test_finds_X(self):
        self.assertEqual(sheets.find_aux_column(FakeWS(HEADER), "실구매옵션"), "X")

    def test_refuses_O_and_protected(self):
        with self.assertRaisesRegex(RuntimeError, "O열"):
            sheets.find_aux_column(FakeWS(HEADER), "상품분류")
        with self.assertRaisesRegex(RuntimeError, "0개"):
            sheets.find_aux_column(FakeWS(HEADER[:23]), "실구매옵션")

    def test_write_skips_prefix_and_clears_tail(self):
        ws = FakeWS(HEADER, col_values=["a", "b", "c", "d"])
        n = sheets.write_aux_column(ws, "X", ["a", "b", "Z"], prev_count=4)
        self.assertEqual(n, 1)
        self.assertEqual(ws.updates, [("X4:X4", [["Z"]])])
        self.assertEqual(ws.clears, ["X5:X5"])

    def test_never_touches_O(self):
        ws = FakeWS(HEADER)
        sheets.write_aux_column(ws, sheets.find_aux_column(ws, "실구매옵션"), ["a"] * 3, 0)
        self.assertTrue(all(r.startswith("X") for r, _ in ws.updates))


class MainHook(unittest.TestCase):
    def test_rule_tab_missing_reports_and_skips(self):
        from src import main as M
        ws = FakeWS(HEADER)
        with mock.patch.object(M.sheets, "worksheet", side_effect=SystemExit("탭을 찾을 수 없습니다")):
            text = M.write_purchase_column(None, ws, [[""] * 14], 0)
        self.assertIn("미기록", text)
        self.assertEqual(ws.updates, [])

    def test_writes_X_with_summary(self):
        from src import main as M
        ws = FakeWS(HEADER)
        rule_ws = mock.Mock(); rule_ws.get.return_value = TABLE
        row = [""] * 14; row[6] = "코즈코즈 두부베개"
        with mock.patch.object(M.sheets, "worksheet", return_value=rule_ws):
            text = M.write_purchase_column(None, ws, [row], 0)
        self.assertEqual(ws.updates, [("X2:X2", [["두부베개"]])])
        self.assertIn("실구매옵션(X열): 규칙 20개 · 미분류 0행", text)


if __name__ == "__main__":
    unittest.main()
