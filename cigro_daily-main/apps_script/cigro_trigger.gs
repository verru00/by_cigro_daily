/**
 * cigro 데일리 리프레시 트리거 (주문 + 광고 + 손익)
 *
 * Apps Script 가 타이밍을 잡고, 실제 실행은 GitHub Actions 가 한다.
 * (GitHub cron 은 지연·누락이 잦아 사용하지 않음)
 *
 * ── 최초 설정 ──────────────────────────────────────────────
 * 1) 프로젝트 설정 > 시간대: (GMT+09:00) 서울
 * 2) 프로젝트 설정 > 스크립트 속성에 아래 4개 등록
 *      GH_TOKEN                 GitHub PAT
 *      GH_OWNER                 레포 소유자 (조직명 또는 계정명)
 *      GH_REPO                  레포 이름 (예: cigro_daily)
 *      GOOGLE_CHAT_WEBHOOK_URL  실패 알림용 웹훅
 * 3) setupTriggers 실행 (1회) — 주문·광고·손익 트리거가 함께 설치된다
 *
 * PAT 권한: fine-grained = Actions(Read and write)
 *           classic      = repo + workflow
 */

// ── 설정 ────────────────────────────────────────────────────
var WORKFLOW_FILE     = 'cigro_refresh.yml';       // 주문
var WORKFLOW_ADS_FILE = 'cigro_ads_refresh.yml';   // 광고
var WORKFLOW_PNL_FILE = 'cigro_pnl_refresh.yml';   // 손익(핵심이익지표)
var GIT_REF           = 'main';

var RUN_HOUR   = 9;    // 주문: 매일 09시대 (Apps Script 시간 트리거는 ±15분 오차)
var RUN_MINUTE = 30;

// 광고를 주문(09:30)보다 20분 먼저 돌린다.
// ±15분 오차로 09:15~09:25 구간에서 겹칠 수 있으나 워크플로가 서로 달라
// GitHub 에서는 병렬 실행된다. 로그인 실패가 잦아지면 간격을 넓힌다.
var ADS_RUN_HOUR   = 9;
var ADS_RUN_MINUTE = 10;

// 손익은 두 달치를 한 번에 받아 오래 걸린다. 주문(09:30) 뒤로 뺀다.
// 브랜드가 계정 전역 설정이라 다른 작업과 실행이 겹치지 않는 편이 안전하다.
var PNL_RUN_HOUR   = 10;
var PNL_RUN_MINUTE = 00;

var VERIFY_AFTER_MIN = 25;   // 슈팅 후 몇 분 뒤에 실행 결과를 확인할지

// ── 공통 ────────────────────────────────────────────────────
function prop_(key) {
  var v = PropertiesService.getScriptProperties().getProperty(key);
  if (!v) throw new Error('스크립트 속성 누락: ' + key);
  return v;
}

function ghFetch_(url, options) {
  var opt = options || {};
  opt.muteHttpExceptions = true;
  opt.headers = {
    Authorization: 'Bearer ' + prop_('GH_TOKEN'),
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28'
  };
  return UrlFetchApp.fetch(url, opt);
}

function notify_(text) {
  var hook = PropertiesService.getScriptProperties().getProperty('GOOGLE_CHAT_WEBHOOK_URL');
  if (!hook) return;
  try {
    UrlFetchApp.fetch(hook, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({ text: text }),
      muteHttpExceptions: true
    });
  } catch (e) {
    Logger.log('알림 실패: ' + e);
  }
}

function repoBase_() {
  return 'https://api.github.com/repos/' + prop_('GH_OWNER') + '/' + prop_('GH_REPO');
}

// ── 메인: 워크플로 슈팅 ──────────────────────────────────────
function dispatchCigroRefresh() {
  dispatch_(WORKFLOW_FILE, '주문', { dry_run: 'false', force_write: 'false' },
            'verifyLastRun');
}

/** 테스트용 — 시트에 쓰지 않고 다운로드/검증만 수행 */
function dispatchCigroDryRun() {
  dispatch_(WORKFLOW_FILE, '주문', { dry_run: 'true', force_write: 'false' }, null);
}

/**
 * 광고 캠페인 리프레시.
 * 수집 기간은 파이썬이 KST 기준으로 계산한다 (전일 하루, 월요일만 이틀).
 */
function dispatchCigroAdsRefresh() {
  dispatch_(WORKFLOW_ADS_FILE, '광고',
            { dry_run: 'false', probe: 'false', force_write: 'false' },
            'verifyLastAdsRun');
}

/** 테스트용 — 광고 다운로드 + 열 확인만, 시트 미반영 */
function dispatchCigroAdsDryRun() {
  dispatch_(WORKFLOW_ADS_FILE, '광고',
            { dry_run: 'true', probe: 'false', force_write: 'false' }, null);
}

/** 셀렉터가 깨졌을 때 — 화면 덤프만 받아본다 (artifact 확인) */
function dispatchCigroAdsProbe() {
  dispatch_(WORKFLOW_ADS_FILE, '광고',
            { dry_run: 'true', probe: 'true', force_write: 'false' }, null);
}

/**
 * 손익(핵심이익지표) 리프레시.
 * 수집 구간은 파이썬이 KST 기준으로 계산한다 (전전월 1일 ~ 어제).
 */
function dispatchCigroPnlRefresh() {
  dispatch_(WORKFLOW_PNL_FILE, '손익',
            { dry_run: 'false', probe: 'false', force_write: 'false' },
            'verifyLastPnlRun');
}

/** 테스트용 — 손익 다운로드 + 열 확인만, 시트 미반영 */
function dispatchCigroPnlDryRun() {
  dispatch_(WORKFLOW_PNL_FILE, '손익',
            { dry_run: 'true', probe: 'false', force_write: 'false' }, null);
}

/** 셀렉터가 깨졌을 때 — 화면 덤프만 받아본다 */
function dispatchCigroPnlProbe() {
  dispatch_(WORKFLOW_PNL_FILE, '손익',
            { dry_run: 'true', probe: 'true', force_write: 'false' }, null);
}

function dispatch_(workflowFile, label, inputs, verifyHandler) {
  var url = repoBase_() + '/actions/workflows/' + workflowFile + '/dispatches';
  var res = ghFetch_(url, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({ ref: GIT_REF, inputs: inputs })
  });

  var code = res.getResponseCode();
  if (code === 204) {
    Logger.log(label + ' 슈팅 성공 (inputs=' + JSON.stringify(inputs) + ')');
    if (verifyHandler) scheduleVerify_(verifyHandler);
    return;
  }

  var msg = '❌ cigro ' + label + ' 리프레시 슈팅 실패\n'
          + 'HTTP ' + code + '\n'
          + res.getContentText().slice(0, 400);
  Logger.log(msg);
  notify_(msg);
  throw new Error('workflow_dispatch 실패: HTTP ' + code);
}

// ── 실행 결과 검증 ───────────────────────────────────────────
/**
 * 워크플로 자체가 죽으면(의존성 설치 실패, 타임아웃 등) 파이썬 알림이
 * 아예 안 나간다. 그 무음 실패를 잡기 위해 일정 시간 뒤 결과를 확인한다.
 */
function scheduleVerify_(handler) {
  clearTriggersFor_(handler);
  var when = new Date(Date.now() + VERIFY_AFTER_MIN * 60 * 1000);
  ScriptApp.newTrigger(handler).timeBased().at(when).create();
}

function verifyLastRun() {
  clearTriggersFor_('verifyLastRun');
  verifyWorkflow_(WORKFLOW_FILE, '주문');
}

function verifyLastAdsRun() {
  clearTriggersFor_('verifyLastAdsRun');
  verifyWorkflow_(WORKFLOW_ADS_FILE, '광고');
}

function verifyLastPnlRun() {
  clearTriggersFor_('verifyLastPnlRun');
  verifyWorkflow_(WORKFLOW_PNL_FILE, '손익');
}

function verifyWorkflow_(workflowFile, label) {
  var url = repoBase_() + '/actions/workflows/' + workflowFile
          + '/runs?event=workflow_dispatch&per_page=1';
  var res = ghFetch_(url, { method: 'get' });

  if (res.getResponseCode() !== 200) {
    notify_('⚠️ cigro ' + label + ' 실행 결과 조회 실패 (HTTP '
            + res.getResponseCode() + ')');
    return;
  }

  var runs = JSON.parse(res.getContentText()).workflow_runs || [];
  if (!runs.length) {
    notify_('⚠️ cigro ' + label + ' 실행 기록을 찾지 못했습니다.');
    return;
  }

  var run = runs[0];

  if (run.status !== 'completed') {
    notify_('⏳ cigro ' + label + ' 리프레시가 ' + VERIFY_AFTER_MIN
            + '분 뒤에도 진행 중입니다.\n'
            + '상태: ' + run.status + '\n' + run.html_url);
    return;
  }

  if (run.conclusion === 'success') {
    Logger.log(label + ' 정상 완료 — 상세 알림은 워크플로가 발송');
    return;
  }

  // failure / cancelled / timed_out
  notify_('❌ cigro ' + label + ' 리프레시 실패\n'
          + '결과: ' + run.conclusion + '\n'
          + '시작: ' + run.run_started_at + '\n'
          + run.html_url);
}

// ── 트리거 설치 / 제거 ───────────────────────────────────────
var HANDLERS = ['dispatchCigroRefresh', 'verifyLastRun',
                'dispatchCigroAdsRefresh', 'verifyLastAdsRun',
                'dispatchCigroPnlRefresh', 'verifyLastPnlRun'];

function setupTriggers() {
  deleteTriggers();

  ScriptApp.newTrigger('dispatchCigroRefresh')
    .timeBased().atHour(RUN_HOUR).nearMinute(RUN_MINUTE).everyDays(1).create();

  ScriptApp.newTrigger('dispatchCigroAdsRefresh')
    .timeBased().atHour(ADS_RUN_HOUR).nearMinute(ADS_RUN_MINUTE).everyDays(1).create();

  ScriptApp.newTrigger('dispatchCigroPnlRefresh')
    .timeBased().atHour(PNL_RUN_HOUR).nearMinute(PNL_RUN_MINUTE).everyDays(1).create();

  Logger.log('트리거 설치 완료 (KST)\n'
           + '  주문: 매일 ' + RUN_HOUR + ':' + RUN_MINUTE + ' 전후\n'
           + '  광고: 매일 ' + ADS_RUN_HOUR + ':' + ADS_RUN_MINUTE + ' 전후\n'
           + '  손익: 매일 ' + PNL_RUN_HOUR + ':' + PNL_RUN_MINUTE + ' 전후');
}

function clearTriggersFor_(handler) {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === handler) ScriptApp.deleteTrigger(t);
  });
}

function deleteTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (HANDLERS.indexOf(t.getHandlerFunction()) >= 0) ScriptApp.deleteTrigger(t);
  });
  Logger.log('기존 트리거 제거 완료');
}

function listTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    Logger.log(t.getHandlerFunction() + ' / ' + t.getEventType());
  });
}
