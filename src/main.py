"""cigro 주문내역 -> 구글 시트(병합) + 드라이브 데일리 리프레시."""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from . import config as C
from . import drive, mapping_alert, product_classifier, sheets
from .cigro_scraper import fetch_all, log


def notify(text: str) -> None:
    if not C.CHAT_WEBHOOK:
        return
    try:
        response = requests.post(C.CHAT_WEBHOOK, json={"text": text}, timeout=15)
        response.raise_for_status()
    except Exception as e:
        log(f"알림 전송 실패: {e}")


def read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    return pd.read_excel(path, dtype=str, keep_default_na=False)


def filter_product_keyword(df: pd.DataFrame, keyword: str) -> pd.DataFrame:
    """제품명에 keyword 가 들어간 행만 남긴다 (위치 무관). keyword 가 비면 그대로 돌려준다.

    제품명 열이 없으면 전부 저장되는 사고를 막기 위해 중단한다.
    """
    if not keyword:
        return df
    cols = {str(c).strip(): c for c in df.columns}
    col = cols.get(C.ORDER_PRODUCT_HEADER)
    if col is None:
        raise SystemExit(f"엑셀에 '{C.ORDER_PRODUCT_HEADER}' 열이 없어 '{keyword}' 필터를 적용할 수 없습니다")
    keep = df[col].astype(str).str.contains(keyword, regex=False)
    return df[keep].reset_index(drop=True)


def to_sheet_rows(df: pd.DataFrame, header: list) -> list[list]:
    """시트 헤더(A~N) 순서에 맞춰 행을 만든다.

    A(수집일자)는 B(날짜)의 날짜 부분으로 채운다.
    엑셀에 없는 열은 빈 값으로 둔다.
    """
    cols = {str(c).strip(): c for c in df.columns}
    out = []
    for _, row in df.iterrows():
        rec = []
        for i, h in enumerate(header):
            h = str(h).strip()
            if i == 0:                       # A 수집일자
                rec.append("")               # 아래에서 B 기준으로 채움
            elif h in cols:
                rec.append(str(row[cols[h]]))
            else:
                rec.append("")
        rec[0] = sheets.parse_date(rec[sheets.COL_DATE])
        out.append(rec)
    return out


def check_columns(df: pd.DataFrame, header: list) -> list[str]:
    """시트 헤더 B~N 중 엑셀에 없는 열 이름을 반환."""
    cols = {str(c).strip() for c in df.columns}
    return [str(h).strip() for h in header[1:] if str(h).strip() and str(h).strip() not in cols]


def write_purchase_column(gc, ws, rows, prev) -> str:
    """상품분류규칙 탭 규칙으로 실구매옵션 열을 값으로 채운다. 알림 문구를 돌려준다.

    어떤 단계든 실패하면 그 열만 건너뛴다 (A~N 은 이미 기록됨, O열은 절대 안 씀).
    """
    label = C.PURCHASE_HEADER
    try:
        col = sheets.find_aux_column(ws, C.PURCHASE_HEADER)
        rule_ws = sheets.worksheet(gc, C.SHEET_ID, C.PURCHASE_RULE_TAB)
        rules = product_classifier.load_rules(rule_ws)
    except SystemExit as e:
        return f"⚠️ {label} 미기록 — 규칙 탭 없음: {e}"
    except Exception as e:
        return f"⚠️ {label} 미기록 — {e}"
    values = product_classifier.classify_rows(rules, rows)
    try:
        n = sheets.write_aux_column(ws, col, values, prev)
    except Exception as e:
        return f"⚠️ {label}({col}열) 기록 실패 — {e}"
    summary = product_classifier.summarize(rows, values)
    log(f"{label}({col}열) 기록 {n:,}행 / 미분류 {summary['unclassified']:,}행")
    return product_classifier.format_summary(summary, len(rules), f"{label}({col}열)")


def main() -> int:
    C.validate()
    start, end = C.period()
    run_day = datetime.now(C.KST).strftime("%Y%m%d")
    out_dir = Path(C.OUT_DIR)

    log(f"기간: {start} ~ {end}")
    log(f"대상 브랜드: {', '.join(C.BRANDS)}")
    log(f"DRY_RUN={C.DRY_RUN} / FILL_FORMULAS={C.FILL_FORMULAS}")
    log(f"제품명 필터: {C.ORDER_PRODUCT_KEYWORD + ' 포함' if C.ORDER_PRODUCT_KEYWORD else '(없음 — 전부 저장)'}")

    files = asyncio.run(fetch_all(C.BRANDS, start, end, out_dir))
    failed = [b for b in C.BRANDS if b not in files]
    if not files:
        notify(f"❌ cigro 리프레시 실패\n기간: {start} ~ {end}\n다운로드된 브랜드 없음")
        return 1

    # ── 수집 데이터 취합 ─────────────────────────────────────────
    frames, counts = [], {}
    for brand, path in files.items():
        raw = read_table(path)
        df = filter_product_keyword(raw, C.ORDER_PRODUCT_KEYWORD)
        counts[brand] = len(df)
        df.to_csv(out_dir / f"{brand}_{start}_{end}.csv",
                  index=False, encoding="utf-8-sig")
        frames.append(df)
        if C.ORDER_PRODUCT_KEYWORD:
            log(f"[{brand}] {len(raw):,}행 수집 -> '{C.ORDER_PRODUCT_KEYWORD}' 포함 {len(df):,}행 저장")
        else:
            log(f"[{brand}] {len(df):,}행 수집")

    total_new = sum(counts.values())
    if total_new == 0:
        notify(f"⚠️ cigro 리프레시 중단\n기간: {start} ~ {end}\n수집 0행 - 시트 미반영")
        return 1

    if C.DRY_RUN:
        log(f"[DRY_RUN] 총 {total_new:,}행 수집 - 시트 미반영")
        notify(f"🧪 cigro dry-run\n기간: {start} ~ {end}\n"
               + "\n".join(f"• {b}: {n:,}행" for b, n in counts.items()))
        return 0

    # ── 시트 병합 ────────────────────────────────────────────────
    gc = sheets.client()
    ws = sheets.worksheet(gc, C.SHEET_ID, C.SHEET_TAB)
    header, existing = sheets.read_existing(ws)
    prev = len(existing)
    log(f"기존 시트: {prev:,}행")

    missing = check_columns(frames[0], header)
    if missing:
        notify(f"❌ cigro 리프레시 중단\n엑셀에 없는 시트 열: {', '.join(missing)}\n"
               f"cigro 화면 구성이 바뀌었을 수 있습니다.")
        log(f"열 불일치 -> 중단: {missing}")
        return 1

    new_rows = []
    for df in frames:
        new_rows.extend(to_sheet_rows(df, header))

    merged, kept = sheets.merge(existing, new_rows, C.BRANDS, start, end)
    replaced = prev - kept

    # 급감 가드: 병합 결과가 기존보다 크게 줄면 쓰지 않는다
    if prev > 0 and len(merged) < prev * C.SHRINK_GUARD and not C.FORCE_WRITE:
        notify(f"⚠️ cigro 리프레시 중단 (급감 가드)\n"
               f"기존 {prev:,}행 -> 병합 후 {len(merged):,}행\n"
               f"정상이면 force_write=true 로 재실행")
        log(f"급감 가드 발동: {prev:,} -> {len(merged):,}")
        return 1

    written = sheets.write(ws, header, merged, prev, C.FILL_FORMULAS,
                           existing=existing)
    log(f"기록 완료: 보존 {kept:,}행 + 신규 {len(new_rows):,}행 = {len(merged):,}행 "
        f"(실제 기록 {written:,}행)")

    # ── 실구매옵션(X열) 값 기록 — 참고용, 수익표·정산 무관 ─────────────
    purchase_text = ""
    if C.PURCHASE_CLASSIFY:
        purchase_text = write_purchase_column(gc, ws, merged, prev)

    # ── 시트 수식 매핑 검증 ─────────────────────────────────────
    # O(상품분류)·P(매출구분)는 Google Sheets ARRAYFORMULA 결과다.
    # 재계산이 끝나기 전에 공란을 읽어 오탐하지 않도록 연속 안정화 뒤 검사한다.
    mapping_summary = "매핑 검증 미실행"
    mapping_detail = ""
    refreshed_brands = list(files)
    try:
        mapping_report = sheets.check_mappings(
            ws, merged, refreshed_brands, start, end)
        mapping_counts = mapping_report["counts"]
        if mapping_report["status"] == "unmapped":
            mapping_summary = (
                f"⚠️ 매핑 누락: 상품분류 {mapping_counts['상품분류']:,}행 / "
                f"매출구분 {mapping_counts['매출구분']:,}행"
            )
            mapping_detail = mapping_alert.format_webhook(
                mapping_report, C.SHEET_TAB, start, end, refreshed_brands)
            log(mapping_summary)
        else:
            mapping_summary = (
                f"매핑 정상: 상품분류/매출구분 "
                f"({mapping_report['scanned_rows']:,}행 검사)"
            )
            log(mapping_summary)
    except Exception as e:
        mapping_summary = f"⚠️ 매핑 검증 실패: {e}"
        log(mapping_summary)
        mapping_detail = (
            f"⚠️ cigro 매출 매핑 검증 실패\n"
            f"탭: {C.SHEET_TAB}\n기간: {start} ~ {end}\n"
            f"브랜드: {', '.join(refreshed_brands)}\n사유: {e}"
        )

    # ── 드라이브 원본 보관 ───────────────────────────────────────
    uploaded = []
    if C.DRIVE_FOLDER_ID:
        try:
            svc = drive.service()
            for brand, path in files.items():
                stem = f"{C.DRIVE_PREFIX}{brand}"
                if C.DRIVE_KEEP_HISTORY:
                    stem += f"_{run_day}"
                drive.upload(svc, C.DRIVE_FOLDER_ID, path, stem + path.suffix)
                uploaded.append(brand)
        except Exception as e:
            log(f"드라이브 업로드 실패: {e}")

    icon = "⚠️" if mapping_detail else "✅"
    lines = [f"{icon} cigro 매출 리프레시",
             f"기간: {start} ~ {end}"]
    lines += [f"• {b}: {n:,}행" for b, n in counts.items()]
    lines.append(f"시트: 보존 {kept:,} + 교체 {len(new_rows):,} = {len(merged):,}행 "
                 f"(직전 {prev:,}행, 제거 {replaced:,})")
    lines.append(mapping_detail or mapping_summary)
    if purchase_text:
        lines.append(purchase_text)
    if uploaded:
        lines.append(f"📁 드라이브 {len(uploaded)}건 보관")
    if failed:
        lines.append(f"❌ 수집 실패: {', '.join(failed)}")
    notify("\n".join(lines))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
