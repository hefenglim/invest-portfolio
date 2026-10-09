/* ---- news-prompt settings — the demo IS the spec (2026-09-10) ----
   FX below is the real engine's output, captured on a disposable seeded instance
   (DB_PATH=<tmp>/probe.db, PD_DISABLE_SCHEDULER=1, port 8477) on 2026-09-10. Nothing in it
   was typed by hand. The lab's simulated server reimplements the PROPOSED semantics and is
   self-checked against these figures at the bottom of the file. */
var FX = {
 "get_initial": {
  "status": 200,
  "body_len": 434,
  "updated_at": "2026-07-06T00:00:00",
  "equals_official": true,
  "sha8": "fe55de9b"
 },
 "official": {
  "version": "v2",
  "len": 434,
  "sha8": "fe55de9b",
  "library_version": "official-v24 (2026-08-28)",
  "body": "你是財經新聞整理員。輸入是一篇新聞文章的正文（可能夾雜網頁雜訊）。\n請忠實整理成結構化資訊，只根據原文，不得杜撰或加入原文沒有的內容或數字。\n<rules>\n1. 一律使用繁體中文（台灣用語）。\n2. body_summary：2–4 句重點摘要，忠於原文、不評論、不加料；原文若含數字照原文引用，不得自行計算或推估。\n3. news_date：文章日期，格式 YYYY-MM-DD；原文無明確日期時，留給呼叫端提供的預設。\n4. related_stocks：文章提及的個股，回傳其代號（台股用數字代號如 2330、美股用英文代號如 AAPL）；沒有明確提及個股時回空陣列。\n5. title：若原文標題可辨識則沿用，否則以一句話擬定精簡標題。\n6. 若正文並非實質新聞內容（如程式碼、樣式表、導覽選單雜訊），body_summary 一律留空字串，不得描述或摘要這些雜訊。\n</rules>\n只回傳一個 JSON 物件，不要 Markdown 圍欄、不要額外散文。"
 },
 "put_empty": {
  "status": 200,
  "resp": {
   "body": "",
   "updated_at": "2026-09-10T09:58:53.858996+08:00"
  }
 },
 "get_after_empty": {
  "status": 200,
  "body_len": 0,
  "updated_at": "2026-09-10T09:58:53.858996+08:00"
 },
 "put_whitespace": {
  "status": 200,
  "body_len": 6
 },
 "put_custom": {
  "status": 200,
  "body_len": 29,
  "updated_at": "2026-09-10T09:58:53.921506+08:00",
  "echo_ok": true
 },
 "get_after_custom": {
  "status": 200,
  "body_len": 29,
  "equals_official": false
 },
 "reset": {
  "status": 200,
  "body_len": 434,
  "updated_at": "2026-09-10T09:58:54.000113+08:00",
  "equals_official": true
 },
 "put_missing_body": {
  "status": 400,
  "resp": {
   "error": {
    "code": "validation_error",
    "message": "請求內容不完整，請確認必填欄位都已填寫"
   }
  }
 },
 "prompt_templates": {
  "status": 200,
  "keys": [
   "library_version",
   "strategies",
   "system_prompt",
   "task_presets"
  ],
  "library_version": "official-v24 (2026-08-28)",
  "system_prompt_version": "v2"
 },
 "system_prompt_get": {
  "status": 200,
  "keys": [
   "body",
   "updated_at"
  ]
 },
 "auth_session": {
  "status": 200,
  "resp": {
   "mode": "guest"
  }
 }
};
var OFFICIAL = FX.official.body;
var FIELDS = ['title', 'news_date', 'body_summary', 'related_stocks'];

(function () {
  var frame = PD.$('#lab1-frame'), mode = 0;

  /* The app's own rules for the pieces the demo shows, copied from web/styles.css +
     web/settings.css so the iframe resolves the SAME cascade (panel head wraps at narrow
     widths because .panel-head{flex-wrap:wrap} — AUDIT M1/M2 2026-07-26). */
  var CSS =
    ':root{--bg:#0c1015;--panel:#131922;--panel-2:#18202b;--border:#232e3c;--border-soft:#1b2430;' +
    '--text:#e6ebf2;--text-2:#9aa6b5;--text-3:#5e6b7c;--accent:#58a6dd;--accent-soft:rgba(88,166,221,.10);' +
    '--amber:#d9a13f;--amber-soft:rgba(217,161,63,.14);--ok:#4fae6c;--ok-soft:rgba(79,174,108,.14);' +
    '--up:#f0544f;--radius:6px;--radius-sm:4px;--pad:16px}' +
    'body{background:var(--bg);color:var(--text);padding:12px;display:flex;flex-direction:column;gap:12px}' +
    '.panel{background:var(--panel);border:1px solid var(--border-soft);border-radius:var(--radius)}' +
    '.panel-head{display:flex;align-items:baseline;gap:12px;row-gap:6px;padding:12px var(--pad) 0;flex-wrap:wrap}' +
    '.panel-title{margin:0;font-size:13px;font-weight:700;letter-spacing:.04em;color:var(--text)}' +
    '.panel-sub{font-size:11px;color:var(--text-3)}' +
    '.panel-head .spacer{flex:1}' +
    '.btn{display:inline-flex;align-items:center;gap:6px;padding:4px 13px;border-radius:var(--radius-sm);' +
    'border:1px solid var(--border);background:var(--panel-2);color:var(--text-2);font-size:12px;line-height:18px;cursor:pointer}' +
    '.btn:hover{border-color:var(--text-3);color:var(--text)}' +
    '.btn-primary{color:var(--accent);border-color:rgba(88,166,221,.45);background:var(--accent-soft);font-weight:700}' +
    '.card{padding:12px var(--pad) 14px;display:flex;flex-direction:column;gap:8px}' +
    'textarea.input{background:var(--panel-2);border:1px solid var(--border);border-radius:var(--radius-sm);' +
    'color:var(--text);font-size:12px;padding:7px 10px;outline:none;min-height:120px;width:100%;resize:vertical;' +
    'font-family:ui-monospace,Menlo,Consolas,monospace;line-height:1.5}' +
    '.hint{font-size:11px;color:var(--text-3)}' +
    '.save-bar{display:flex;align-items:center;gap:12px;padding:10px var(--pad);border-top:1px solid var(--border-soft)}' +
    '.save-bar .note{font-size:11px;color:var(--text-3);flex:1}' +
    '.badge{display:inline-block;font-size:10.5px;padding:1px 7px;border-radius:9px;border:1px solid var(--border);' +
    'color:var(--text-2);background:var(--panel-2);margin-left:6px;vertical-align:1px}' +
    '.badge.official{color:var(--ok);border-color:rgba(79,174,108,.5);background:var(--ok-soft)}' +
    '.badge.custom{color:var(--amber);border-color:rgba(217,161,63,.5);background:var(--amber-soft)}' +
    '.schema{font-size:11px;color:var(--amber);background:var(--amber-soft);border:1px solid rgba(217,161,63,.35);' +
    'border-radius:var(--radius-sm);padding:6px 9px;display:none}' +
    '.schema.on{display:block}' +
    '.toast{position:fixed;right:12px;bottom:12px;max-width:80%;font-size:12px;padding:8px 12px;border-radius:6px;' +
    'border:1px solid var(--border);background:var(--panel-2);color:var(--text);display:none}' +
    '.toast.on{display:block}.toast.ok{border-color:rgba(79,174,108,.5);color:var(--ok)}' +
    '.toast.fail{border-color:rgba(240,84,79,.5);color:var(--up)}' +
    '.gap{border:1px dashed var(--border);border-radius:var(--radius);padding:10px var(--pad);font-size:11.5px;color:var(--text-3)}' +
    '.gap code{color:var(--text-2)}' +
    '.tpl{display:flex;gap:8px;flex-wrap:wrap;padding:0 var(--pad) 12px}' +
    '.tpl span{background:var(--panel-2);border:1px solid var(--border);border-radius:4px;padding:3px 8px;font-size:11px;color:var(--text-2)}';

  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

  /* Panels that exist TODAY on settings.html#prompts (view-prompts): 系統提示詞 + 策略提示詞. */
  function existingPanels() {
    return '<section class="panel"><div class="panel-head"><h2 class="panel-title">系統提示詞</h2>' +
      '<span class="panel-sub">更新 2026-07-06・套用於所有策略提示詞之前</span><span class="spacer"></span>' +
      '<button class="btn" type="button">重置回官方版</button></div>' +
      '<div class="card"><textarea class="input" spellcheck="false" readonly>你是資深投資組合分析師，服務一位同時持有台股、美股、馬股的個人長期投資者。…（略）</textarea>' +
      '<span class="hint">定義角色、語言與全域規則；所有 AI 呼叫（輸入解析、洞察批次）皆先套用此提示詞。</span></div>' +
      '<div class="save-bar"><span class="note">「儲存設定」寫入系統提示詞；策略卡各自的「儲存」即時寫入資料庫</span>' +
      '<button class="btn btn-primary" type="button">儲存設定</button></div></section>' +
      '<section class="panel"><div class="panel-head"><h2 class="panel-title">策略提示詞</h2>' +
      '<span class="panel-sub">啟用中的策略會在洞察批次依序執行，各產生一張洞察卡片</span><span class="spacer"></span>' +
      '<button class="btn" type="button">從官方模板庫新增</button><button class="btn btn-primary" type="button">＋新增策略</button></div>' +
      '<div class="tpl"><span>台股 · 產業輪動</span><span>美股 · 財報前瞻</span><span>組合 · 風險體檢</span></div></section>';
  }

  /* The PROPOSED third panel. ids are the ones the implementation will use, so the wiring
     probe in the acceptance table has something concrete to grade. */
  function newsPanel() {
    return '<section class="panel" id="news-prompt-panel"><div class="panel-head">' +
      '<h2 class="panel-title">新聞整理提示詞<span class="badge" id="news-prompt-badge"></span></h2>' +
      '<span class="panel-sub" id="news-prompt-meta"></span><span class="spacer"></span>' +
      '<button class="btn" id="news-reset" type="button" title="以官方模板庫最新版覆蓋目前內容">重置回官方版</button></div>' +
      '<div class="card"><textarea class="input" id="news-prompt" spellcheck="false"></textarea>' +
      '<div class="schema" id="news-prompt-schema"></div>' +
      '<span class="hint">新聞管線把每篇文章正文交給 AI 整理成 <code>{title, news_date, body_summary, related_stocks}</code>；' +
      '這是那一次呼叫的系統提示詞。下次執行（每日 06:00 排程或新聞庫的「手動抓取」）生效。</span></div>' +
      '<div class="save-bar"><span class="note" id="news-save-note">修改後按「儲存」；空白內容不會被接受——要回到預設請用「重置回官方版」</span>' +
      '<button class="btn btn-primary" id="news-save" type="button">儲存</button></div></section>';
  }

  /* The in-iframe script: the PROPOSED server semantics, offline. Every rule here is one
     the acceptance table names, and the self-checks at the bottom of this file hold the
     same rules against the engine's captured output. */
  function newsScript() {
    return '<scr' + 'ipt>(function(){' +
      'var OFFICIAL=' + JSON.stringify(OFFICIAL) + ';' +
      'var FIELDS=' + JSON.stringify(FIELDS) + ';' +
      'var st={body:OFFICIAL,updated_at:' + JSON.stringify(FX.get_initial.updated_at) + '};' +
      'var $=function(s){return document.querySelector(s)};' +
      'function ping(){dispatchEvent(new Event("resize"))}' +
      'function fmtDate(s){return String(s).slice(0,10)}' +
      'function render(){' +
      '  var ta=$("#news-prompt"); if(document.activeElement!==ta) ta.value=st.body;' +
      '  var off=(st.body===OFFICIAL);' +
      '  var b=$("#news-prompt-badge"); b.textContent=off?"與官方版相同":"已自訂"; b.className="badge "+(off?"official":"custom");' +
      '  $("#news-prompt-meta").textContent="更新 "+fmtDate(st.updated_at)+"・官方 " + ' + JSON.stringify(FX.official.version) + ' + "・每日 06:00 新聞管線與手動抓取共用";' +
      '  schema(ta.value); ping();}' +
      'function schema(text){var miss=FIELDS.filter(function(k){return text.indexOf(k)<0});' +
      '  var el=$("#news-prompt-schema"); document.body.dataset.missing=miss.join(",");' +
      '  if(miss.length){el.textContent="提示詞未提及欄位 "+miss.join("、")+" — 整理員回傳的 JSON 需要這四個鍵，缺了文章會整理失敗（不擋儲存，只提醒）";el.className="schema on"}' +
      '  else{el.className="schema";el.textContent=""}}' +
      'function toast(kind,msg){var t=$("#t"); t.textContent=msg; t.className="toast on "+kind; clearTimeout(t._h); t._h=setTimeout(function(){t.className="toast"},2600)}' +
      'function save(body){' +
      '  if(!body.trim()){document.body.dataset.last="PUT → 422 news_prompt_empty"; toast("fail","新聞整理提示詞不可為空白；要恢復預設請按「重置回官方版」"); return}' +
      '  st.body=body; st.updated_at=new Date().toISOString(); document.body.dataset.last="PUT → 200"; toast("ok","已儲存：下次新聞管線執行生效"); render()}' +
      'function reset(){st.body=OFFICIAL; st.updated_at=new Date().toISOString(); document.body.dataset.last="POST reset → 200"; toast("ok","已重置：新聞整理提示詞已回到官方 " + ' + JSON.stringify(FX.official.version) + '); render()}' +
      '$("#news-save").addEventListener("click",function(){save($("#news-prompt").value)});' +
      '$("#news-reset").addEventListener("click",function(){reset()});' +
      '$("#news-prompt").addEventListener("input",function(e){schema(e.target.value); var off=(e.target.value===OFFICIAL); var b=$("#news-prompt-badge"); b.textContent=off?"與官方版相同":"未儲存的修改"; b.className="badge "+(off?"official":"custom"); ping()});' +
      'document.body.dataset.last="GET → 200（種子）"; render();' +
      '})()</scr' + 'ipt>';
  }

  var probe = '{panels:document.querySelectorAll(".panel").length,' +
    'news:!!document.getElementById("news-prompt"),' +
    'badge:((document.getElementById("news-prompt-badge")||{}).textContent)||"—",' +
    'len:((document.getElementById("news-prompt")||{value:""}).value||"").length,' +
    'last:document.body.dataset.last||"—",' +
    'missing:document.body.dataset.missing||"",' +
    'sw:document.scrollingElement.scrollWidth,cw:document.scrollingElement.clientWidth}';

  function html(m) {
    if (m === 0) {
      return existingPanels() +
        '<div class="gap">新聞整理提示詞：<b>沒有面板</b>。唯一的修改入口是 <code>curl -X PUT /api/news-prompt</code>；' +
        '而且 <code>{"body": ""}</code> 會被接受（實測 HTTP 200，之後 GET 回 0 字）。</div>';
    }
    return '<section class="panel"><div class="panel-head"><h2 class="panel-title">系統提示詞</h2>' +
      '<span class="panel-sub">更新 2026-07-06・套用於所有策略提示詞之前</span><span class="spacer"></span>' +
      '<button class="btn" type="button">重置回官方版</button></div><div class="tpl"><span>（同現況，略）</span></div></section>' +
      newsPanel() +
      '<section class="panel"><div class="panel-head"><h2 class="panel-title">策略提示詞</h2>' +
      '<span class="panel-sub">啟用中的策略會在洞察批次依序執行，各產生一張洞察卡片</span><span class="spacer"></span>' +
      '<button class="btn" type="button">從官方模板庫新增</button><button class="btn btn-primary" type="button">＋新增策略</button></div>' +
      '<div class="tpl"><span>（同現況，略）</span></div></section>' +
      '<div class="toast" id="t"></div>' + newsScript();
  }

  function render(m) {
    mode = m;
    PD.sandbox(frame, { id: 'lab1', css: CSS, html: html(m), probe: probe, onData: onData });
  }
  function onData(d) {
    var over = d.sw > d.cw + 1;
    PD.ro(PD.$('#lab1-ro-a'), d.news ? '有（#news-prompt）' : '無', d.news ? 'good' : 'bad');
    PD.ro(PD.$('#lab1-ro-b'), d.news ? d.badge : '無徽章（畫面看不到是不是官方版）', d.news ? (d.badge === '與官方版相同' ? 'good' : '') : 'bad');
    PD.ro(PD.$('#lab1-ro-c'), d.news ? (d.len + ' 字') : '434 字（伺服器端，畫面看不到）', d.news ? 'good' : '');
    PD.ro(PD.$('#lab1-ro-d'), d.news ? d.last : 'PUT "" → 200（實測）', d.news ? (d.last.indexOf('422') >= 0 ? 'good' : '') : 'bad');
    PD.ro(PD.$('#lab1-ro-e'), d.sw + ' px（可用 ' + d.cw + '）', over ? 'bad' : 'good');
    if (mode === 0) {
      PD.verdict(PD.$('#lab1-verdict'), 'bad',
        '<b>現況：提示詞存在、可被夜間管線讀取，卻沒有任何畫面能看見或修改它。</b>' +
        '路由自 2026-07-06 就在（GET／PUT／reset 都有契約測試與操作記錄標籤），只是從未接上 UI；' +
        '同時空白內容會被原樣存進去。');
    } else {
      PD.verdict(PD.$('#lab1-verdict'), over ? 'bad' : 'good',
        '<b>提案：第三個面板，與系統提示詞同一套文法。</b>徽章即時說明目前是官方版還是自訂版；' +
        '把內容清空再按「儲存」會被擋下（422），要回預設只能走「重置回官方版」；刪掉欄位名稱會出現琥珀色提醒但不擋。' +
        (over ? ' <b>但這個寬度撐破了版面。</b>' : ' 拖到 320px 也不撐破——面板頭沿用既有的 flex-wrap 規則。'));
    }
  }
  PD.modes(PD.$('#lab1-modes'), render);
  PD.sizer(PD.$('#lab1-range'), frame, PD.$('#lab1-w'));
})();

/* ---- self-checks: the demo's offline rules vs the REAL engine's captured output ---- */
(function () {
  var seededIsOfficial = (OFFICIAL === FX.official.body) && FX.get_initial.equals_official;
  PD.checkEq('內嵌的官方本文長度 = 引擎 GET /api/news-prompt 的種子本文長度', OFFICIAL.length, FX.get_initial.body_len,
    '官方 ' + FX.official.version + '，sha256 ' + FX.official.sha8);
  PD.checkEq('種子狀態的「與官方版相同」判定 = 引擎 equals_official', String(seededIsOfficial), String(FX.get_initial.equals_official));
  PD.checkEq('自訂本文儲存後不再等於官方版（引擎）', String(FX.get_after_custom.equals_official), 'false',
    'PUT 自訂 29 字 → GET 回 ' + FX.get_after_custom.body_len + ' 字');
  PD.checkEq('重置回官方版 → 引擎回到官方本文', String(FX.reset.equals_official && FX.reset.body_len === OFFICIAL.length), 'true',
    'POST /api/news-prompt/reset → ' + FX.reset.body_len + ' 字');
  PD.checkEq('現況：PUT {"body": ""} 被接受（缺口，實測）', FX.put_empty.status, 200,
    '之後 GET 回 ' + FX.get_after_empty.body_len + ' 字；空白 6 字也是 ' + FX.put_whitespace.status);
  PD.checkEq('現況：PUT 缺 body 欄位 → 400 validation_error', FX.put_missing_body.status, 400,
    (FX.put_missing_body.resp.error || {}).message || '');
  PD.checkEq('/api/prompt-templates 目前沒有 news_prompt 區塊', String(FX.prompt_templates.keys.indexOf('news_prompt') < 0), 'true',
    'keys: ' + FX.prompt_templates.keys.join(', ') + '（' + FX.prompt_templates.library_version + '）');
  PD.checkEq('demo 的欄位提醒規則：官方本文提及全部四個欄位', String(FIELDS.every(function (k) { return OFFICIAL.indexOf(k) >= 0; })), 'true');
  PD.checkPanel(PD.$('#pd-selfcheck'));
})();
