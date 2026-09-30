# -*- coding: utf-8 -*-
"""통합 자체점검. 네트워크 없이 도는 것만 검증한다.

    python selftest.py

깃허브에 올리기 전에 돌려서 전부 PASS 인지 확인한다.
"""
import io
import os
import subprocess
import sys
from datetime import date, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ok = True
n = 0


def chk(label, cond, extra=""):
    global ok, n
    n += 1
    ok = ok and cond
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  -> {extra}" if extra != "" else ""))


print("=" * 70)
print("1) 전체 파일 컴파일")
files = [f"src/{f}" for f in sorted(os.listdir("src")) if f.endswith(".py")]
r = subprocess.run([sys.executable, "-m", "py_compile"] + files,
                   capture_output=True, text=True)
chk(f"{len(files)}개 모듈 컴파일", r.returncode == 0, r.stderr[:200])

# 컴파일은 통과해도 import 안 한 함수를 부르면 실행 중 NameError 로 죽는다.
# (v20: 손익이 current_brand 를 import 없이 써서 액션이 다운로드 직전에 터졌다)
# 모듈 최상위에 정의·import 되지 않은 전역 이름을 참조하면 실패로 본다.
import builtins
import symtable


def undefined_globals(path):
    src = open(path, encoding="utf-8").read()
    top = symtable.symtable(src, path, "exec")
    known = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
    for sym in top.get_symbols():
        if sym.is_assigned() or sym.is_imported() or sym.is_namespace():
            known.add(sym.get_name())
    bad = set()

    def walk(tbl):
        for child in tbl.get_children():
            for sym in child.get_symbols():
                if sym.is_global() and sym.is_referenced() and sym.get_name() not in known:
                    bad.add(sym.get_name())
            walk(child)

    walk(top)
    for sym in top.get_symbols():
        if sym.is_referenced() and not (sym.is_assigned() or sym.is_imported()) \
                and sym.get_name() not in known:
            bad.add(sym.get_name())
    return sorted(bad)


for _f in files:
    _bad = undefined_globals(_f)
    chk(f"{_f} 정의 안 된 이름 없음", not _bad, ", ".join(_bad))

print("\n2) 워크플로 YAML")
try:
    import yaml
    for f in sorted(os.listdir(".github/workflows")):
        d = yaml.safe_load(open(f".github/workflows/{f}", encoding="utf-8"))
        step = [s for s in d["jobs"]["refresh"]["steps"] if "env" in s][0]
        chk(f"{f} ({d['name']})", True, f"env {len(step['env'])}개")
except ImportError:
    print("  (pyyaml 없음 - 생략)")

print("\n3) 버전 표식 일치")
from src.cigro_ads_scraper import SCRAPER_VERSION, EXCEL_SELECTORS
ver = open("VERSION.md", encoding="utf-8").read()
# 프로젝트 버전은 VERSION.md 한 곳에서만 관리한다.
# 각 스크래퍼는 자기 코드 버전을 따로 들고 있어서, 손익을 고쳐도
# 광고 파일이 덩달아 바뀌지 않는다.
import re as _re
_m = _re.search(r"\*\*(v\d+)", ver)
chk("VERSION.md 에 버전 표기", bool(_m), _m.group(1) if _m else "없음")
chk("광고 스크래퍼는 자체 버전", SCRAPER_VERSION.startswith("ads "), SCRAPER_VERSION)
from src.cigro_pnl_scraper import SCRAPER_VERSION as PNLVER
chk("손익 스크래퍼는 자체 버전", PNLVER.startswith("pnl "), PNLVER)

print("\n4) v3 핵심 수정이 실제로 들어있나")
src = open("src/cigro_ads_scraper.py", encoding="utf-8").read()
for key, why in [
    ("import time", "벽시계 타임아웃"),
    ("async def wait_ready", "폴링 대기"),
    ("button.export-btn", "확정 EXCEL 셀렉터"),
    ("async def nav_by_ui", "사이드바 대체 경로"),
    ("BODY_TEXT_JS", "probe 본문 덤프"),
    ("time.monotonic", "경과시간 측정"),
]:
    chk(f"{key:22} ({why})", key in src)
chk("export-btn 이 1순위", EXCEL_SELECTORS[0] == "button.export-btn", EXCEL_SELECTORS)
# 수집 경로의 goto 는 반드시 wait_ready 로 이어져야 한다.
# (nav_by_ui 안의 settle=5000 은 사이드바 클릭 전 대기라 정상이므로
#  'settle=5000 이 없을 것'으로 검사하면 안 된다)
chk("goto 직후 wait_ready 호출", "ready = await wait_ready(page)" in src)
# v4: 기간 표시/표 헤더를 준비 신호로 쓰면 0초에 통과해버린다 (실제 발생한 버그)
# 독스트링에는 이 버그 설명이 남아 있으므로 log() 호출만 본다.
chk("기간 표시로 통과시키지 않음",
    'log(f"  화면 준비 완료 (기간 표시 확인' not in src)
chk("표 헤더로 통과시키지 않음",
    'log(f"  화면 준비 완료 (표 헤더 확인' not in src)
chk("EXCEL 버튼만 준비 신호", src.count("화면 준비 완료 (EXCEL 버튼 확인") == 1)
chk("대기 중 진행 로그 있음", "대기 중..." in src)
chk("고정 대기 후 즉시 판정하던 옛 흐름 없음",
    "settle=5000)\n\n                if not await _looks_like_ads_screen" not in src)

print("\n5) URL 조립 (사용자 제공 URL 과 대조)")
from urllib.parse import urlparse, parse_qs
from src import config as C
from src.cigro_ads_scraper import build_url
got = parse_qs(urlparse(build_url("2026-09-10", "코즈코즈")).query)
want = {"menu": "analysis", "tab": "ad", "group_by": "campaign", "brand_name": "코즈코즈"}
for k, v in want.items():
    chk(f"{k}={v}", got[k][0] == v, got[k][0])
chk("start=end (하루)", got["start_date"][0] == got["end_date"][0] == "2026-09-10")

print("\n6) 수집 날짜 규칙")
def dates_for(d):
    back = 2 if d.weekday() == 0 else 1
    return [(d - timedelta(days=x)).isoformat() for x in range(back, 0, -1)]
chk("월 2026-09-14 -> 토,일 이틀",
    dates_for(date(2026, 9, 14)) == ["2026-09-12", "2026-09-13"], dates_for(date(2026, 9, 14)))
chk("화 2026-09-15 -> 하루", dates_for(date(2026, 9, 15)) == ["2026-09-14"])
cov = {}
for i in range(200):
    d = date(2026, 1, 1) + timedelta(days=i)
    if d.weekday() == 6:
        continue
    for x in dates_for(d):
        cov[x] = cov.get(x, 0) + 1
win = [(date(2026, 1, 5) + timedelta(days=i)).isoformat() for i in range(150)]
chk("일요일 미실행 150일 - 누락 0", sum(1 for d in win if cov.get(d, 0) == 0) == 0)

print("\n7) 시트 병합 (광고 RAW A~N)")
import pandas as pd
from src import ads_sheets as A
HDR = ["수집일자", "광고채널", "캠페인", "광고비", "노출수", "클릭수", "전환수",
       "전환값", "CPM", "CTR", "CPC", "CPA", "CPI", "ROAS"]
chk("DATA_COLS=14", A.DATA_COLS == 14, A.DATA_COLS)
for s, w in [("2026-09-10", "2026-09-10"), ("2026. 9. 10", "2026-09-10"),
             ("2026/9/10", "2026-09-10"), ("합계", "")]:
    chk(f"parse_date({s!r})", A.parse_date(s) == w, repr(A.parse_date(s)))
df = pd.DataFrame([{c: "1" for c in HDR[1:]}])
rows = A.to_rows(df, HDR, "2026-09-10")
chk("A열에 수집일자 찍힘", rows[0][0] == "2026-09-10", rows[0][0])
chk("행 길이 14", len(rows[0]) == 14)
chk("열 누락 감지", A.check_columns(df.drop(columns=["CTR"]), HDR) == ["CTR"])
ex = [["2026-09-09"] + [""] * 13, ["2026-09-10", "OLD"] + [""] * 12,
      ["합계"] + [""] * 13]
m, k = A.merge(ex, rows, ["2026-09-10"])
chk("구간 밖 + 날짜없는 행 보존", k == 2, k)
chk("해당 날짜 교체됨", not any("OLD" in r for r in m))
chk("합계행 맨 끝", m[-1][0] == "합계", m[-1][0])
chk("날짜 파싱 실패율 감지", A.unparseable_ratio([["2026년 9월 10일"] + [""] * 13]) == 1.0)
chk("S~V 수식만 (O~R 미관리)", sorted(A.FORMULA_TEMPLATES) == ["S", "T", "U", "V"])

print("")
print("8) 모듈 간 참조 (실제로 존재하는 속성만 부르는가)")
# ads_main 이 ads_sheets.client() 를 부르는데 ads_sheets 에 없어서
# 다운로드까지 다 끝낸 뒤 AttributeError 로 죽은 적이 있다.
# 정적으로 훑어서 같은 사고를 막는다.
import ast
import importlib

MODS = {"ads_sheets": "src.ads_sheets", "C": "src.config",
        "drive": "src.drive", "sheets": "src.sheets",
        "pnl_sheets": "src.pnl_sheets"}

for entry in ("src/ads_main.py", "src/main.py", "src/cigro_ads_scraper.py",
              "src/pnl_main.py", "src/cigro_pnl_scraper.py"):
    tree = ast.parse(open(entry, encoding="utf-8").read())
    refs = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in MODS):
            refs.add((node.value.id, node.attr))
    bad = []
    for alias, attr in sorted(refs):
        mod = importlib.import_module(MODS[alias])
        if not hasattr(mod, attr):
            bad.append(f"{alias}.{attr}")
    chk(f"{entry} - 참조 {len(refs)}개", not bad, ", ".join(bad) if bad else "")

# 진입점이 임포트되는지도 본다 (selftest 가 ads_main 을 안 봐서 위 버그를 놓쳤다)
for m in ("src.ads_main", "src.main", "src.pnl_main"):
    try:
        importlib.import_module(m)
        chk(f"{m} 임포트", True)
    except Exception as e:
        chk(f"{m} 임포트", False, f"{type(e).__name__}: {e}")


print("")
print("9) 손익(핵심이익지표)")
from src import cigro_pnl_scraper as PS
from src import pnl_sheets as PSH

chk("다운로드 셀렉터가 file_download 기반",
    all("file_download" in x for x in PS.DOWNLOAD_SELECTORS), PS.DOWNLOAD_SELECTORS[0])
chk("EXCEL 텍스트에 의존하지 않음",
    "excel_button" not in open("src/cigro_pnl_scraper.py", encoding="utf-8").read())
# v10: 브랜드를 못 고르면 전사 데이터가 시트를 덮는다. 반드시 중단해야 한다.
_ps = open("src/cigro_pnl_scraper.py", encoding="utf-8").read()
# 주문 스크래퍼의 검증된 select_brand 를 쓴다. 직접 구현했을 때
# div.cnvaYaC2(하위 그룹)를 눌러서 계속 튕겼다.
# 브랜드는 계정 전역 설정. 자동화마다 각자 건다.
# 주문 화면용 select_brand 는 div.coldw1 조기반환 때문에 손익에서 오작동했다.
# 매출과 같은 방식(텍스트 기반 select_brand)을 써야 한다.
# 대시보드 캡처의 클래스(div.cnvdaJ0)를 박았다가 핵심이익지표 화면에
# 그게 없어 브랜드 버튼을 아예 못 찾았다 (로그: 현재 브랜드: '').
# 브랜드는 계정 전역 설정. 매출·광고·손익이 같은 함수를 써야 한다.
_ads = open("src/cigro_ads_scraper.py", encoding="utf-8").read()
_ord = open("src/cigro_scraper.py", encoding="utf-8").read()
chk("공통 ensure_brand 정의", "async def ensure_brand" in _ord)
chk("손익이 공통 함수 사용", "ensure_brand(page, brand)" in _ps)
chk("광고도 공통 함수 사용", "ensure_brand(page, brand)" in _ads)
chk("광고가 확인 실패 시 중단", "브랜드 확인 실패, 중단" in _ads)
# 주석에 남은 경위 설명은 괜찮다. 실제로 locator 로 쓰면 안 된다.
chk("클래스명으로 브랜드 버튼 안 찾음",
    'locator("div.cnvdaJ0")' not in _ps + _ads and "BRAND_OPENER" not in _ps + _ads)
chk("브랜드 확인은 텍스트로", "BRAND_ALL_TEXT" in _ord)
# 구간 슬라이스로 다른 함수까지 지운 적이 있다. 전부 살아있는지 본다.
for _fn in ("render_budget", "build_url", "download_button", "wait_rendered",
            "prep_clicks", "wait_ready", "download_excel", "fetch_pnl"):
    chk(f"손익 {_fn} 정의됨", f"def {_fn}(" in _ps)
# floating-group 은 상시 요소다. 사라지길 기다리면 90초를 날린다.
chk("오버레이 소멸을 기다리지 않음", "OVERLAY_SELECTORS" not in _ps)
# v15: select_brand 가 True 를 반환하고도 전사 데이터가 수집된 적이 있다.
# 반환값이 아니라 화면 표시를 읽어 검증한다.
chk("브랜드를 화면에서 검증", "async def current_brand" in _ord)
chk("표시가 다르면 중단", "화면이 '{brand}' 를 가리키지 않음" in _ord)
chk("다운로드 직전 스크린샷", "before_download.png" in _ps)
chk("prep_clicks 가 성공 여부를 반환", "async def prep_clicks(page, brand: str = \"\") -> bool:" in _ps)
chk("실패 시 수집 중단", "브랜드 선택 실패, 중단 (전사 데이터 유입 방지)" in _ps)
# 드롭다운에 브랜드명이 헤더+항목으로 두 번 나온다. 헤더를 누르면 안 닫힌다.
# v13: 빈 화면을 클릭하면 floating-group 오버레이에 튕긴다
chk("클릭 전 렌더 대기", "async def wait_rendered" in _ps)
chk("렌더 대기가 prep_clicks 보다 먼저", _ps.index("await wait_rendered(page)") < _ps.index("if not await prep_clicks"))
for a, b, rw in [("2026-09-14","2026-09-14",15), ("2026-07-01","2026-09-14",45),
                 ("2026-01-01","2026-09-14",90)]:
    chk(f"구간별 렌더 대기 {a}~{b} = {rw}초", PS.render_budget(a, b)[0] == rw * 1000)

chk("공헌이익(VAT포함) 수식 등록", C.PNL_FORMULA_TEMPLATES.get("공헌이익(VAT포함)") == "=C{r}*1.1")
chk("영업이익(VAT포함) 수식 등록", C.PNL_FORMULA_TEMPLATES.get("영업이익(VAT포함)") == "=C{r}*1.1")

PHDR = ["수집일자","날짜","공헌이익","결제금액","수익","원가","배송비","수수료",
        "광고비","변동(판관)비","VAT","공헌이익(VAT포함)","년","월","주","일"]
XLS = pd.DataFrame([{"날짜":"2026-09-14","공헌이익":"1","결제금액":"1","수익":"0","원가":"1",
                     "배송비":"1","수수료":"1","광고비":"1","변동(판관)비":"0","VAT":"1"}])
# v8: 수식 열을 '엑셀에 없는 열' 로 잡아 중단됐던 실제 사고
chk("수식 열을 누락으로 보지 않음", PSH.check_columns(XLS, PHDR) == [],
    PSH.check_columns(XLS, PHDR))
chk("진짜 누락은 여전히 감지", PSH.check_columns(XLS.drop(columns=["광고비"]), PHDR) == ["광고비"])
PHDR_NP = PHDR[:11] + ["영업이익(VAT포함)"] + PHDR[12:]
chk("순이익 탭도 통과", PSH.check_columns(XLS, PHDR_NP) == [])

class _WS:
    row_count = 500
    def __init__(self): self.writes = []
    def update(self, values=None, range_name=None, **kw): self.writes.append((range_name, values))
    def add_rows(self, n): pass
    def batch_clear(self, r): pass
_rows = [["2026-09-14"] + [""] * 15 for _ in range(3)]
_ws = _WS(); PSH.write(_ws, PHDR, _rows, prev_count=0, existing=None)
_v = _ws.writes[0][1]
chk("L열에 행번호별 수식 기록",
    [x[11] for x in _v] == ["=C2*1.1", "=C3*1.1", "=C4*1.1"], [x[11] for x in _v])
chk("입력 rows 는 변형되지 않음", _rows[0][11] == "")

def _wk(d):
    j = date(d.year, 1, 1)
    return (d - (j - timedelta(days=j.weekday()))).days // 7 + 1
_bad = [d for i in range(1461) for d in [date(2024,1,1)+timedelta(days=i)]
        if PSH.date_parts(d.isoformat()) != {"년":d.year,"월":d.month,"주":_wk(d),"일":d.day}]
chk("년월주일 4년치 기존 로직과 일치", not _bad, f"{len(_bad)}건 불일치")


print("")
print("10) 앱스크립트가 모든 워크플로를 알고 있나")
# 손익 워크플로를 추가하고 앱스크립트를 안 고쳐서 슈팅이 안 됐다.
# 워크플로 파일이 늘면 여기서 바로 잡힌다.
_gs = open("apps_script/cigro_trigger.gs", encoding="utf-8").read()
for _f in sorted(os.listdir(".github/workflows")):
    chk(f"{_f} 슈팅 등록", f"'{_f}'" in _gs)
# 트리거로도 걸려 있어야 매일 돈다
for _h in ("dispatchCigroRefresh", "dispatchCigroAdsRefresh", "dispatchCigroPnlRefresh"):
    chk(f"{_h} 트리거 설치", f"ScriptApp.newTrigger('{_h}')" in _gs)
# 실패 검증 핸들러도 짝이 맞아야 한다
for _v in ("verifyLastRun", "verifyLastAdsRun", "verifyLastPnlRun"):
    chk(f"{_v} 정의됨", f"function {_v}()" in _gs)
    chk(f"{_v} HANDLERS 등록", f"'{_v}'" in _gs.split("var HANDLERS")[1].split("]")[0])

# 트리거 설치 로그와 파일 헤더에 세 작업이 다 설명돼 있어야 한다
_head = _gs.split("*/")[0]
_setup = _gs.split("function setupTriggers")[1].split("\n}")[0]
for _label in ("매출", "광고", "손익"):
    chk(f"헤더에 {_label} 설명", _label in _head)
    chk(f"설치 로그에 {_label}", _label in _setup)
for _tab in ("씨그로 2개월_리프레시", "광고 RAW", "공헌이익"):
    chk(f"설치 로그에 대상 탭 '{_tab}'", _tab in _setup)


print("=" * 70)
print(f"{n}개 검사 / RESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
sys.exit(0 if ok else 1)
