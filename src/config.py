"""환경변수 로드 및 수집 기간 계산."""
import os
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from datetime import datetime

KST = ZoneInfo("Asia/Seoul")

# ── 인증 ─────────────────────────────────────────────────────────
CIGRO_EMAIL    = os.environ.get("CIGRO_EMAIL", "")
CIGRO_PASSWORD = os.environ.get("CIGRO_PASSWORD", "")

# ── 대상 브랜드 ──────────────────────────────────────────────────
# 기본값은 brand_config.py. 환경변수(쉼표 구분)가 있으면 그쪽이 우선한다.
# 워크플로는 Variables 가 없으면 빈 문자열을 넘기므로 `or` 로 받는다.
from .brand_config import BRAND as DEFAULT_BRAND

BRANDS = [b.strip() for b in (os.environ.get("CIGRO_BRANDS", "").strip() or DEFAULT_BRAND).split(",")
          if b.strip()]


def check_brand(value, label: str) -> None:
    """브랜드가 비었거나 자리표시자면 중단. 빈 값은 cigro '전체' 수집으로 이어진다."""
    values = value if isinstance(value, list) else [value]
    bad = [v for v in values if not v or v.startswith("여기에_")]
    if not values or bad:
        raise SystemExit(f"{label} 브랜드 미설정 — src/brand_config.py 의 BRAND 를 채우세요")

# ── 구글 시트 ────────────────────────────────────────────────────
SHEET_ID  = os.environ.get("CIGRO_SHEET_ID", "")
SHEET_TAB = os.environ.get("SHEET_TAB", "씨그로 2개월_리프레시")

# true 면 파이썬이 O~V 수식을 행마다 채운다.
# false(기본) 면 O~V 를 건드리지 않는다 -> 시트 수식이 ARRAYFORMULA 여야 함.
FILL_FORMULAS = os.environ.get("FILL_FORMULAS", "false").lower() == "true"

# ── 실구매옵션(X열) 파이썬 기록 ──────────────────────────────────
# 사람이 보는 참고용 분류. 수익표·정산은 읽지 않는다.
# O열(상품분류)은 시트 수식 담당이며 파이썬은 절대 쓰지 않는다.
PURCHASE_CLASSIFY = os.environ.get("PURCHASE_CLASSIFY", "true").lower() == "true"
PURCHASE_RULE_TAB = os.environ.get("PURCHASE_RULE_TAB", "상품분류규칙")
PURCHASE_HEADER   = os.environ.get("PURCHASE_HEADER", "실구매옵션")

# ── 구글 드라이브 (원본 엑셀 보관) ───────────────────────────────
# 비워두면 드라이브 업로드를 건너뛴다.
DRIVE_FOLDER_ID   = os.environ.get("DRIVE_FOLDER_ID", "").strip()
# true 면 파일명에 실행일을 붙여 이력을 쌓고, false 면 같은 파일을 덮어쓴다.
DRIVE_KEEP_HISTORY = os.environ.get("DRIVE_KEEP_HISTORY", "false").lower() == "true"
DRIVE_PREFIX      = os.environ.get("DRIVE_PREFIX", "cigro_주문_")

# ── 실행 옵션 ────────────────────────────────────────────────────
DRY_RUN      = os.environ.get("DRY_RUN", "false").lower() == "true"
FORCE_WRITE  = os.environ.get("FORCE_WRITE", "false").lower() == "true"  # 급감 가드 무시
HEADLESS     = os.environ.get("HEADLESS", "true").lower() == "true"
CHAT_WEBHOOK = os.environ.get("GOOGLE_CHAT_WEBHOOK_URL", "")

# 직전 대비 행수가 이 비율 미만으로 떨어지면 쓰기 중단 (0.5 = 절반)
SHRINK_GUARD = float(os.environ.get("SHRINK_GUARD", "0.5"))

# 기간 수동 지정 (디버그용). 비워두면 자동 계산.
OVERRIDE_START = os.environ.get("START_DATE", "").strip()
OVERRIDE_END   = os.environ.get("END_DATE", "").strip()

OUT_DIR = "out"


def _minus_months(d: date, n: int) -> date:
    """d 의 n개월 전 같은 달 1일."""
    y, m = d.year, d.month - n
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def period() -> tuple[str, str]:
    """수집 기간: 당월 기준 전전월 1일 ~ 어제 (KST).

    예) 2026-08-06 실행 -> 2026-06-01 ~ 2026-08-05
    """
    if OVERRIDE_START and OVERRIDE_END:
        return OVERRIDE_START, OVERRIDE_END

    today = datetime.now(KST).date()
    start = _minus_months(today, 2)          # 전전월 1일
    end   = today - timedelta(days=1)        # 어제

    # 매월 1일 실행 시: 어제가 전월 말일이 되어 당월분이 0일치가 됨 (정상 동작)
    if end < start:
        raise ValueError(f"기간 역전: {start} ~ {end}")
    return start.isoformat(), end.isoformat()


def validate() -> None:
    missing = [k for k, v in {
        "CIGRO_EMAIL": CIGRO_EMAIL,
        "CIGRO_PASSWORD": CIGRO_PASSWORD,
        "CIGRO_SHEET_ID": SHEET_ID,
    }.items() if not v]
    if missing and not DRY_RUN:
        raise SystemExit(f"필수 환경변수 누락: {', '.join(missing)}")
    check_brand(BRANDS, "주문")


# ══════════════════════════════════════════════════════════════════
# 광고(캠페인) 데일리 리프레시
#
# 대상 시트 '광고 RAW' 구조 (A~V, 22열)
#   A      수집일자   <- cigro 엑셀에 없다. 다운로드한 날짜를 직접 찍는다.
#   B~N    광고채널 캠페인 광고비 노출수 클릭수 전환수 전환값
#          CPM CTR CPC CPA CPI ROAS        <- cigro 엑셀 13열
#   O~R    셀러명 셀러ID 상품분류 구분(공구여부)   <- 건드리지 않음
#   S~V    년 월 주 일                          <- 수식. 건드리지 않음
#
# 주문 리프레시와 동일하게 A~N 만 관리한다.
#
# cigro 광고 화면은 선택한 기간을 '합산'해서 캠페인당 1행으로 보여준다.
# 따라서 이틀치를 한 번에 받으면 두 날이 합쳐진다. 하루씩 따로 받는다.
# ══════════════════════════════════════════════════════════════════

# ── 시트 ─────────────────────────────────────────────────────────
ADS_SHEET_ID  = os.environ.get("CIGRO_ADS_SHEET_ID", "").strip() or SHEET_ID
ADS_SHEET_TAB = os.environ.get("ADS_SHEET_TAB", "광고 RAW")

# 자동화가 관리하는 열 수 (A~N). O 이후는 수식/수동 영역이라 손대지 않는다.
ADS_DATA_COLS = 14
ADS_DATE_HEADER = os.environ.get("ADS_DATE_HEADER", "수집일자")   # A열 헤더 이름

# true 면 신규 행의 S~V(년월주일) 수식을 파이썬이 채운다.
# false(기본) 면 건드리지 않는다 -> 시트가 ARRAYFORMULA 여야 함.
ADS_FILL_FORMULAS = os.environ.get("ADS_FILL_FORMULAS", "false").lower() == "true"

# ── 브랜드 ───────────────────────────────────────────────────────
# '광고 RAW' 에는 브랜드 열이 없다 = 단일 브랜드 전용 시트.
# 여러 브랜드를 한 탭에 넣으려면 시트에 브랜드 열을 먼저 만들어야 한다.
ADS_BRAND = os.environ.get("CIGRO_AD_BRAND", "").strip() or DEFAULT_BRAND.strip()

# ── 화면 진입 ────────────────────────────────────────────────────
# 확인된 URL:
#   https://app.cigro.io/?menu=analysis&tab=ad&group_by=campaign
#       &brand_name=코즈코즈&start_date=2026-09-10&end_date=2026-09-10
ADS_GROUP_BY = os.environ.get("ADS_GROUP_BY", "campaign")   # ad_channel 이면 채널별

# 통째로 덮어쓸 URL 템플릿. {s} {e} {b} 치환. 비우면 위 파라미터로 조립.
ADS_URL = os.environ.get("CIGRO_ADS_URL", "").strip()

# 엑셀 다운로드 버튼 (화면 라벨은 'EXCEL'). 비우면 자동 탐색.
ADS_EXCEL_SELECTOR = os.environ.get("ADS_EXCEL_SELECTOR", "").strip()

# ── 실행 옵션 ────────────────────────────────────────────────────
# true 면 다운로드 없이 각 단계 스크린샷 + 클릭 가능 요소 목록만 남긴다.
ADS_PROBE = os.environ.get("ADS_PROBE", "false").lower() == "true"

ADS_PREFIX  = os.environ.get("ADS_DRIVE_PREFIX", "cigro_광고_")
ADS_OUT_DIR = "out_ads"


def ads_dates() -> list[str]:
    """수집할 날짜 목록 (KST). 화면이 기간을 합산하므로 하루씩 나눈다.

    평일   : 전일 하루          -> ['2026-09-10']
    월요일 : 전전일 ~ 전일 이틀 -> ['2026-09-12', '2026-09-13']
    """
    if OVERRIDE_START and OVERRIDE_END:
        s = date.fromisoformat(OVERRIDE_START)
        e = date.fromisoformat(OVERRIDE_END)
        if e < s:
            raise ValueError(f"기간 역전: {s} ~ {e}")
        return [(s + timedelta(days=i)).isoformat() for i in range((e - s).days + 1)]

    today = datetime.now(KST).date()
    back  = 2 if today.weekday() == 0 else 1        # 0 = 월요일
    return [(today - timedelta(days=n)).isoformat() for n in range(back, 0, -1)]


def validate_ads() -> None:
    missing = [k for k, v in {
        "CIGRO_EMAIL": CIGRO_EMAIL,
        "CIGRO_PASSWORD": CIGRO_PASSWORD,
        "CIGRO_ADS_SHEET_ID (또는 CIGRO_SHEET_ID)": ADS_SHEET_ID,
    }.items() if not v]
    if missing and not DRY_RUN:
        raise SystemExit(f"필수 환경변수 누락: {', '.join(missing)}")
    if not ADS_PROBE:
        check_brand(ADS_BRAND, "광고")


# ══════════════════════════════════════════════════════════════════
# 손익(핵심이익지표) 데일리 리프레시
#
# cigro '분석 > 핵심이익지표' 화면의 엑셀 한 파일 안에
# 공헌이익 / 매출이익 / 순이익 세 시트가 들어 있다. 각각 별도 탭에 쌓는다.
#
# 이 지표들은 매출·원가만으로 재현할 수 없다.
#   · 공헌이익 = 매출이익 - 수수료·배송비·광고비 - 변동 판관비
#   · 순이익   = 공헌이익 - 고정비
# 변동 판관비와 고정비는 cigro 에 직접 입력되는 값이라 크롤링이 필요하다.
#
# 판관비는 월말·월초에 입력된다. 그래서 수집 구간을 주문과 동일하게
# '전전월 1일 ~ 어제'로 잡는다. 매일 지난 두 달을 다시 받으므로
# 판관비가 언제 입력되든 다음 실행에서 반영된다.
# ══════════════════════════════════════════════════════════════════

PNL_SHEET_ID = os.environ.get("CIGRO_PNL_SHEET_ID", "").strip() or SHEET_ID

# 엑셀 시트명 -> 구글 시트 탭명. 이름이 다르면 여기만 고친다.
PNL_TABS = {
    "공헌이익": os.environ.get("PNL_TAB_CM", "공헌이익"),
    "매출이익": os.environ.get("PNL_TAB_GP", "매출이익"),
    "순이익":   os.environ.get("PNL_TAB_NP", "순이익"),
}

PNL_BRAND = os.environ.get("CIGRO_PNL_BRAND", "").strip() or ADS_BRAND

# 확인된 URL:
#   https://app.cigro.io/?menu=analysis&tab=key_metrics
#       &brand_name=코즈코즈&start_date=...&end_date=...&group=gross_profit
PNL_GROUP = os.environ.get("PNL_GROUP", "gross_profit")
PNL_URL   = os.environ.get("CIGRO_PNL_URL", "").strip()   # {s} {e} {b} 치환

# 다운로드 아이콘 셀렉터 덮어쓰기. 비우면 검증된 기본 목록을 쓴다.
PNL_DOWNLOAD_SELECTOR = os.environ.get("PNL_DOWNLOAD_SELECTOR", "").strip()

# 다운로드 아이콘이 나타날 때까지 기다리는 최대 시간(ms).
# 준비 클릭과 렌더 대기를 마친 뒤부터 잰다.
PNL_WAIT_MS = int(os.environ.get("PNL_WAIT_MS", "180000"))

# 시트가 수식으로 들고 있는 열. 엑셀에 없는 값이라 파이썬이 빈칸으로 덮으면
# 기존 수식이 통째로 날아간다. 헤더 이름으로 찾아 행 번호만 끼워 넣는다.
# (기존 cigro_total_auto.py 의 apply_cm_formulas / apply_np_formulas 와 동일)
#   공헌이익 탭 : 공헌이익(VAT포함) = C열 * 1.1
#   순이익   탭 : 영업이익(VAT포함) = C열 * 1.1
PNL_FORMULA_TEMPLATES = {
    "공헌이익(VAT포함)": "=C{r}*1.1",
    "영업이익(VAT포함)": "=C{r}*1.1",
}

# 날짜 열 후보. 엑셀 열 이름이 바뀌어도 찾아내기 위해 여러 개 둔다.
PNL_DATE_HEADERS = [h.strip() for h in
                    os.environ.get("PNL_DATE_HEADERS", "날짜,수집일자,일자").split(",")
                    if h.strip()]

# 년·월·주·일을 파이썬이 '값'으로 채운다 (기본 켜짐).
# ARRAYFORMULA 는 행이 늘수록 시트 전체를 다시 계산해 느려진다.
# 손익 탭은 매일 두 달치를 덮어쓰므로 값으로 넣는 편이 가볍다.
# 시트에 직접 수식을 걸어 쓰시려면 false 로 두고 해당 열 헤더를 비워두면 된다.
PNL_FILL_DATEPARTS = os.environ.get("PNL_FILL_DATEPARTS", "true").lower() == "true"

# 날짜 파생 열의 헤더 이름. 시트 헤더에 이 이름이 있으면 그 칸을 채운다.
PNL_DATEPART_HEADERS = {
    "년": "year", "월": "month", "주": "week", "일": "day",
}

# ── 변동판관비 변동 감시 ─────────────────────────────────────────
# 변동판관비는 월말·월초에 cigro 에 입력된다. 입력 전에는 공헌이익이
# 과대 표시되므로, 그 구간에 값이 들어왔는지 매일 확인해 알린다.
PNL_SGA_HEADERS = [h.strip() for h in
                   os.environ.get("PNL_SGA_HEADERS", "변동판관비,변동 판관비,판관비").split(",")
                   if h.strip()]
# 알림 구간: 말일 N일 전부터 ~ 익월 M일까지
PNL_SGA_BEFORE_END = int(os.environ.get("PNL_SGA_BEFORE_END", "3"))
PNL_SGA_UNTIL_DAY  = int(os.environ.get("PNL_SGA_UNTIL_DAY", "7"))

PNL_PROBE = os.environ.get("PNL_PROBE", "false").lower() == "true"
PNL_PREFIX  = os.environ.get("PNL_DRIVE_PREFIX", "cigro_손익_")
PNL_OUT_DIR = "out_pnl"


def pnl_period() -> tuple[str, str]:
    """손익 수집 기간. 주문과 동일하게 전전월 1일 ~ 어제."""
    return period()


def validate_pnl() -> None:
    missing = [k for k, v in {
        "CIGRO_EMAIL": CIGRO_EMAIL,
        "CIGRO_PASSWORD": CIGRO_PASSWORD,
        "CIGRO_PNL_SHEET_ID (또는 CIGRO_SHEET_ID)": PNL_SHEET_ID,
    }.items() if not v]
    if missing and not DRY_RUN:
        raise SystemExit(f"필수 환경변수 누락: {', '.join(missing)}")
    if not PNL_PROBE:
        check_brand(PNL_BRAND, "손익")
