/**
 * LINE → ホームページ更新ボット 中継スクリプト（Google Apps Script）
 *
 * 役割:
 *  - LINE の Webhook を受け取り、シート「queue」に溜める（PC 側の poller.py が取りに来る）
 *  - 受け取った直後に「受け付けました」と返信する（返信は無料枠を消費しない）
 *  - 下書きのプレビューページを表示する（?preview=トークン）
 *
 * スクリプトプロパティ（プロジェクトの設定 → スクリプト プロパティ）:
 *  SECRET               : poller.py の config.json の gas_secret と同じ値
 *  CHANNEL_ACCESS_TOKEN : LINE チャネルアクセストークン（長期）
 *  ALLOWED_USER_IDS     : 操作を許可する LINE ユーザーID（カンマ区切り）
 *  GROUP_PREFIX         : グループで指示するときの先頭文字（省略時 "#HP"）
 *  GITHUB_TOKEN         : （クラウドで動かすとき）GitHub Actions を起動するためのトークン
 *  GITHUB_REPO          : （クラウドで動かすとき）"ユーザー名/リポジトリ名"
 */

const SHEET_QUEUE = 'queue';
const SHEET_PREVIEW = 'previews';
const SHEET_STATE = 'state';
const CHUNK = 45000; // セル1つの上限(5万文字)より少し小さく分割する
const KEEP_DAYS = 7;

function props_() {
  return PropertiesService.getScriptProperties();
}

function sheet_(name, headers) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.appendRow(headers);
  }
  return sh;
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function authorized_(e) {
  const secret = props_().getProperty('SECRET');
  return !!secret && e.parameter && e.parameter.key === secret;
}

function doGet(e) {
  if (e.parameter && e.parameter.preview) return showPreview_(e.parameter.preview);
  if (!authorized_(e)) return json_({ error: 'unauthorized' });
  if (e.parameter.action === 'fetch') return json_({ rows: fetch_() });
  if (e.parameter.action === 'state') return json_({ state: loadState_() });
  return json_({ ok: true });
}

function doPost(e) {
  if (!authorized_(e)) return json_({ error: 'unauthorized' });
  const body = JSON.parse((e.postData && e.postData.contents) || '{}');
  const action = e.parameter.action;
  if (action === 'ack') {
    ack_(body.ids || []);
    return json_({ ok: true });
  }
  if (action === 'preview') {
    savePreview_(body.token, body.title, body.html);
    return json_({ ok: true });
  }
  if (action === 'state') {
    saveState_(body);
    return json_({ ok: true });
  }
  // それ以外は LINE からの Webhook
  const queued = (body.events || []).map(handleEvent_).some(Boolean);
  if (queued) startCloud_();
  return json_({ ok: true });
}

// ---------- LINE イベント ----------

/** キューに入れたら true を返す。 */
function handleEvent_(ev) {
  if (ev.type !== 'message' && ev.type !== 'postback') return;
  const src = ev.source || {};
  const isGroup = src.type === 'group' || src.type === 'room';
  const msg = ev.message || {};

  // グループでは先頭が GROUP_PREFIX の文章だけを指示として扱う
  if (isGroup && ev.type === 'message' && msg.type === 'text') {
    const prefix = props_().getProperty('GROUP_PREFIX') || '#HP';
    if (msg.text.indexOf(prefix) !== 0) return;
  }

  const allowed = (props_().getProperty('ALLOWED_USER_IDS') || '')
    .split(',').map(function (s) { return s.trim(); }).filter(String);
  if (allowed.indexOf(src.userId) < 0) {
    if (!isGroup || msg.type === 'text' || ev.type === 'postback') {
      reply_(ev.replyToken, 'ホームページの更新は登録された方のみ利用できます。\nあなたのユーザーID:\n' + src.userId +
        '\n（管理者に登録を依頼してください）');
    }
    return;
  }

  if (ev.type === 'message' && ['text', 'image', 'video'].indexOf(msg.type) < 0) {
    if (!isGroup) reply_(ev.replyToken, '文章・写真・動画に対応しています。');
    return;
  }

  enqueue_(ev);

  if (ev.type === 'postback') {
    const data = String((ev.postback || {}).data || '');
    if (data.indexOf('approve:') === 0) {
      reply_(ev.replyToken, '公開の手続きを受け付けました。少しお待ちください。');
    } else if (data !== 'delete_abort') {
      reply_(ev.replyToken, '受け付けました。少しお待ちください。');
    }
  } else if (msg.type === 'text') {
    reply_(ev.replyToken, '受け付けました。ホームページの準備をしています。少しお待ちください。');
  } else if (!isGroup && (!msg.imageSet || msg.imageSet.index === 1)) {
    reply_(ev.replyToken, '写真・動画を受け取りました。続けて、記事にする内容を文章で送ってください。');
  }
  return true;
}

/** GitHub Actions のボットを起動する（GITHUB_TOKEN が未設定なら何もしない＝PC で動かす）。 */
function startCloud_() {
  const token = props_().getProperty('GITHUB_TOKEN');
  const repo = props_().getProperty('GITHUB_REPO');
  if (!token || !repo) return;
  UrlFetchApp.fetch('https://api.github.com/repos/' + repo + '/dispatches', {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json' },
    payload: JSON.stringify({ event_type: 'line' }),
    muteHttpExceptions: true,
  });
}

function reply_(replyToken, text) {
  const token = props_().getProperty('CHANNEL_ACCESS_TOKEN');
  if (!token || !replyToken) return;
  UrlFetchApp.fetch('https://api.line.me/v2/bot/message/reply', {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + token },
    payload: JSON.stringify({ replyToken: replyToken, messages: [{ type: 'text', text: text }] }),
    muteHttpExceptions: true,
  });
}

// ---------- キュー ----------

function enqueue_(ev) {
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    sheet_(SHEET_QUEUE, ['id', 'received', 'event', 'done'])
      .appendRow([Utilities.getUuid(), new Date(), JSON.stringify(ev), '']);
  } finally {
    lock.releaseLock();
  }
}

function fetch_() {
  const sh = sheet_(SHEET_QUEUE, ['id', 'received', 'event', 'done']);
  const values = sh.getDataRange().getValues();
  const rows = [];
  for (let i = 1; i < values.length && rows.length < 100; i++) {
    if (values[i][3] === '') rows.push({ id: values[i][0], event: JSON.parse(values[i][2]) });
  }
  return rows;
}

function ack_(ids) {
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const sh = sheet_(SHEET_QUEUE, ['id', 'received', 'event', 'done']);
    const values = sh.getDataRange().getValues();
    const now = new Date();
    const limit = now.getTime() - KEEP_DAYS * 86400000;
    for (let i = values.length - 1; i >= 1; i--) {
      if (ids.indexOf(values[i][0]) >= 0) {
        sh.getRange(i + 1, 4).setValue(now);
      } else if (values[i][3] !== '' && new Date(values[i][3]).getTime() < limit) {
        sh.deleteRow(i + 1);
      }
    }
  } finally {
    lock.releaseLock();
  }
}

// ---------- 処理状態（poller.py の state） ----------

function loadState_() {
  const sh = sheet_(SHEET_STATE, ['json...']);
  if (sh.getLastRow() < 2) return null;
  const text = sh.getRange(2, 1, 1, sh.getLastColumn()).getValues()[0].join('');
  return text ? JSON.parse(text) : null;
}

function saveState_(state) {
  const text = JSON.stringify(state);
  const chunks = [];
  for (let i = 0; i < text.length; i += CHUNK) chunks.push(text.substring(i, i + CHUNK));
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const sh = sheet_(SHEET_STATE, ['json...']);
    if (sh.getMaxColumns() < chunks.length) sh.insertColumnsAfter(sh.getMaxColumns(), chunks.length - sh.getMaxColumns());
    sh.getRange(2, 1, 1, sh.getMaxColumns()).clearContent();
    const range = sh.getRange(2, 1, 1, chunks.length);
    range.setNumberFormat('@'); // 数字や日付に変換されないよう文字列として保存
    range.setValues([chunks]);
  } finally {
    lock.releaseLock();
  }
}

// ---------- プレビュー ----------

function savePreview_(token, title, html) {
  const chunks = [];
  for (let i = 0; i < html.length; i += CHUNK) chunks.push(html.substring(i, i + CHUNK));
  sheet_(SHEET_PREVIEW, ['token', 'created', 'title', 'html...'])
    .appendRow([token, new Date(), title].concat(chunks));
}

function showPreview_(token) {
  const sh = sheet_(SHEET_PREVIEW, ['token', 'created', 'title', 'html...']);
  const cell = sh.getRange('A:A').createTextFinder(token).matchEntireCell(true).findNext();
  if (!cell) return HtmlService.createHtmlOutput('<p>プレビューが見つかりません。</p>');
  const row = sh.getRange(cell.getRow(), 1, 1, sh.getLastColumn()).getValues()[0];
  return HtmlService.createHtmlOutput(row.slice(3).join(''))
    .setTitle(String(row[2]))
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}
