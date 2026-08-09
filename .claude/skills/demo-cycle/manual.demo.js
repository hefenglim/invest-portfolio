/* manual.demo.js — the live parts of the /demo-cycle manual.
 *
 * Everything here either runs a real browser mechanism in a sandbox, or reproduces a
 * shipped tool's real logic and then proves that reproduction against output the tool
 * actually printed on this machine (see the self-check strip at the bottom of the page).
 */
(function () {
  'use strict';
  var $ = PD.$;

  /* ================= 01 · which phase should I run ================= */

  var PICK = [
    { v: 'good', t: '<b>跑 <code>propose</code>。</b>先把提案做成可點的 demo，你確認之後才進實作——'
        + '這一步就是為了避免文字規格的認知誤差。',
      c: 'new_report.py --kind spec --slug <你的功能> --title "<標題> — 規格 demo"\n'
        + '# 產物：docs/spec/<date>-<slug>.html  →  你在 demo 上確認  →  驗收表成為規格' },
    { v: 'good', t: '<b>跑 <code>audit</code>。</b>先起一個可丟棄實例，三支探針各跑一輪，'
        + '再把量到的東西寫成分級報告。金額那一塊交給 <code>/stress-audit</code>。',
      c: '# 1) 起可丟棄實例（見 03 節）\n'
        + '# 2) 量測\nprobe_live.py layout <url> --pages "/,/ledger.html" --widths 390,1257 --out DIR\n'
        + 'probe_live.py wiring <url> --pages "/ledger.html" --click\n'
        + '# 3) /stress-audit  →  引用它的 fail=0\n'
        + '# 4) new_report.py --kind audit --slug full-site --title "..."' },
    { v: '', t: '<b>先量那一個點，不要開全站稽核。</b>用 <code>css</code> 問「哪條規則真的勝出」、'
        + '用 <code>wiring --click</code> 問「這顆按鈕到底有沒有反應」。'
        + '確認是真問題之後，再決定要不要做成 <code>fix</code> 報告。',
      c: 'probe_live.py css    <url> --selector "<出問題的選擇器>" --prop <屬性> --width 1257\n'
        + 'probe_live.py wiring <url> --pages "<那一頁>" --click\n'
        + '# 量到了再寫報告；量不到就先別下結論' },
    { v: 'good', t: '<b>跑 <code>fix</code>。</b>每一項都要附證據：gate 輸出、前後量測、可操作的對照 demo。'
        + '沒有證據的項目一律標未完成。',
      c: 'new_report.py --kind fix --slug audit-remediation --title "稽核修正報告"\n'
        + '# 證據來源：pytest / mypy --strict / ruff / stress-audit / probe_live 前後對照\n'
        + '# 收尾：/ship-version' },
    { v: '', t: '<b>跑 <code>rootcause</code>。</b>把「原推測 vs 實測」並排，中間放一個能親手切換的 demo。'
        + '同時寫進 CHANGELOG；如果它是一「類」錯誤而不只是個案，才寫 LESSONS_LEARNED。',
      c: 'new_report.py --kind rootcause --slug divergence --title "根因分歧 — 前後機制演示"\n'
        + '# 沒有分歧就不要產這份報告，改在 fix 報告裡寫「無分歧」' }
  ];
  PD.modes($('#pick-modes'), function (m) {
    var p = PICK[m];
    PD.verdict($('#pick-verdict'), p.v, p.t);
    var pre = $('#pick-cmd');
    pre.className = '';
    pre.textContent = p.c;
  });

  /* ================= 04 · the cascade is only knowable by measuring ================= */

  var CARDS = [['總市值', '5,933,032'], ['總報酬', '+3,941,317'],
               ['年化報酬 (XIRR)', '資料不足'], ['損益（TWD）', '+3,941,317'],
               ['換匯損益（歸因拆分）', '+1,862']];
  var cssMode = 0;

  function cssCss(m) {
    var s = '.band{display:grid;grid-template-columns:repeat(8,1fr);gap:8px;padding:9px}'
      + '.band.v2{grid-template-columns:1.15fr 1.3fr 1.05fr 1.25fr 1.25fr}'
      + '.c{background:#161b22;border:1px solid #262e39;border-radius:7px;padding:8px 9px;'
      + 'overflow:hidden}'
      + '.c .l{font-size:10.5px;color:#78838f;white-space:nowrap}'
      + '.c .v{font-size:15px;font-weight:700;white-space:nowrap;margin-top:3px;'
      + 'font-family:ui-monospace,Consolas,monospace}';
    if (m === 1) s += '.band>*{min-width:0}';          // 原報告的建議：加了也沒用
    // 斷點：只有模式 2 把 .v2 寫進去，特異性才追得上無條件的 .band.v2
    s += '@media (max-width:620px){' + (m === 2 ? '.band,.band.v2' : '.band')
      + '{grid-template-columns:repeat(2,1fr)}}';
    return s;
  }
  var CSS_PROBE = '{cols:getComputedStyle(document.getElementById("bd")).gridTemplateColumns,'
    + 'card:Math.round(document.getElementById("bd").children[0].getBoundingClientRect().width),'
    + 'clip:Array.from(document.getElementById("bd").children)'
    + '.filter(function(x){return Array.from(x.children).some('
    + 'function(y){return y.scrollWidth>y.clientWidth+1})}).length}';

  function cssRender(m) {
    cssMode = m;
    PD.sandbox($('#css-frame'), {
      id: 'css', css: cssCss(m), probe: CSS_PROBE, onData: cssData,
      html: '<div class="band v2" id="bd">' + CARDS.map(function (c) {
        return '<div class="c"><div class="l">' + c[0] + '</div><div class="v">' + c[1] + '</div></div>';
      }).join('') + '</div>'
    });
  }
  function cssData(d) {
    var n = d.cols.split(/\s+/).length;
    PD.ro($('#css-ro-cols'), n + ' 欄 · ' + d.cols.slice(0, 34), n > 2 ? 'bad' : 'good');
    PD.ro($('#css-ro-card'), d.card + ' px', d.card < 150 ? 'bad' : 'good');
    PD.ro($('#css-ro-clip'), d.clip + ' 張', d.clip ? 'bad' : 'good');
    var narrow = document.getElementById('css-range').value <= 620;
    if (cssMode === 2) {
      PD.verdict($('#css-verdict'), d.clip ? 'bad' : 'good', d.clip
        ? '<b>還是壞的？</b>把滑桿拉到 620 以下再看——斷點只在那之後才參賽。'
        : '<b>斷點補上 <code>.band.v2</code> 之後，特異性追平（0,2,0），靠出現順序勝出 →'
          + ' 真的變成 2 欄。</b>');
    } else if (!narrow) {
      PD.verdict($('#css-verdict'), '', '<b>目前寬度在斷點之上，三種寫法本來就一樣。</b>'
        + '把滑桿拉到 <b>620px 以下</b>，差別才會出現。');
    } else {
      PD.verdict($('#css-verdict'), 'bad', cssMode === 1
        ? '<b><code>min-width: 0</code> 確實生效了，但欄數完全沒變。</b>'
          + '要修的機制（斷點的特異性）<b>從頭到尾沒被觸碰</b>——'
          + '這就是「diff 看起來完全正確，卻毫無作用」的長相。'
        : '<b>斷點形同不存在：</b><code>@media { .band }</code> 是 (0,1,0)，'
          + '而無條件的 <code>.band.v2</code> 是 (0,2,0)。'
          + '<b>媒體查詢本身不貢獻特異性</b>，所以它被壓過，卡片擠成 '
          + d.card + 'px、' + d.clip + ' 張的內容被裁掉。');
    }
  }
  PD.modes($('#css-modes'), cssRender);
  PD.sizer($('#css-range'), $('#css-frame'), $('#css-w'));
  document.getElementById('css-range').addEventListener('input', function () {
    cssData.last && cssData.last();
  });

  /* ================= 05 · the wiring verdict ladder ================= */

  /* The shipped rule from probe_live.py::cmd_wiring, reproduced. The self-checks at the
     bottom of this page hold it against verdicts the tool actually printed. */
  function classify(c) {
    var strong = [], weak = [];
    if (c.inline) strong.push('inline onclick');
    if (c.link) strong.push('真連結 / form submit');
    if (c.idState === 'yes') strong.push('id 字面出現在 JS');
    if (c.dataDispatch) strong.push('data-* 被 dispatch');
    if (c.clsRef) {
      if (c.clsFreq <= 2) strong.push('class 被引用，且只有 ' + c.clsFreq + ' 個控制項穿它');
      else weak.push('class 被引用，但 ' + c.clsFreq + ' 個控制項共用 → 證明不了這一顆');
    }
    return { verdict: strong.length ? 'wired' : (weak.length ? 'weak' : 'SUSPECT'),
             evidence: strong.concat(weak) };
  }

  function wRead() {
    return {
      inline: $('#w-inline').value === '1',
      link: $('#w-link').value === '1',
      idState: $('#w-id').value,
      dataDispatch: $('#w-data').value === '1',
      clsRef: $('#w-cls').value === '1',
      clsFreq: parseInt($('#w-freq').value, 10) || 0,
      click: $('#w-click').value
    };
  }
  function wRender() {
    var c = wRead(), r = classify(c);
    var tone = r.verdict === 'wired' ? 'good' : (r.verdict === 'SUSPECT' ? 'bad' : '');
    PD.ro($('#w-ro-v'), r.verdict, tone);
    PD.ro($('#w-ro-e'), r.evidence.length ? r.evidence.length + ' 項' : '無', tone);
    var html = '<b>靜態判定：' + r.verdict + '</b><br>'
      + (r.evidence.length ? '證據：' + r.evidence.map(PD.esc).join('；') : '沒有任何程式碼指向它。');
    if (r.verdict === 'wired') {
      html += '<br>→ 不必再點。<code>--click</code> 只處理 weak / SUSPECT。';
    } else if (c.click === 'skip') {
      html += '<br>→ <b>這只是線索，不是判決。</b>加 <code>--click</code> 讓它在乾淨頁面上被點一次。';
    } else if (c.click === 'yes') {
      html += '<br>→ <b>點下去有反應，結案：它是活的。</b>'
        + '靜態抓不到通常是因為選擇器是組合出來的（<code>$(\'#tab-\' + t)</code>）。';
    } else {
      html += '<br>→ <b class="bad">DEAD? 點下去什麼也沒發生。</b>'
        + '報告前先確認它<b>不是本來就啟用的分頁／開關</b>——那種情況點了本來就不會變。';
    }
    PD.verdict($('#w-verdict'), c.click === 'no' && r.verdict !== 'wired' ? 'bad' : tone, html);
  }
  ['#w-inline', '#w-link', '#w-id', '#w-data', '#w-cls', '#w-freq', '#w-click']
    .forEach(function (s) {
      $(s).addEventListener('input', wRender);
      $(s).addEventListener('change', wRender);
    });
  wRender();

  /* ================= 06 · overflow vs a table scrolling legitimately ================= */

  var ROWS = [['2330', '台積電', '1,000', '612.50', '612,500'],
              ['2303', '聯電', '5,000', '48.35', '241,750'],
              ['AAPL', 'Apple Inc.', '30', '212.40', '6,372.00']];
  function layCss(m) {
    return '.tw{margin:8px}' + (m === 1 ? '.tw{overflow-x:auto}' : '')
      + 'table{border-collapse:collapse;min-width:560px;font-size:11px}'
      + 'th,td{border-bottom:1px solid #262e39;padding:5px 8px;white-space:nowrap;text-align:left}'
      + 'th{color:#78838f;font-weight:600}';
  }
  var LAY_PROBE = '{sw:document.scrollingElement.scrollWidth,'
    + 'cw:document.scrollingElement.clientWidth,'
    + 'off:Array.from(document.querySelectorAll("body *")).filter(function(x){'
    + 'var r=x.getBoundingClientRect();if(!r.width&&!r.height)return false;'
    + 'for(var p=x.parentElement;p;p=p.parentElement){var o=getComputedStyle(p).overflowX;'
    + 'if(o==="auto"||o==="scroll")return false}'
    + 'return r.right>document.scrollingElement.clientWidth+1}).length,'
    + 'leg:Array.from(document.querySelectorAll("*")).filter(function(x){'
    + 'var o=getComputedStyle(x).overflowX;'
    + 'return (o==="auto"||o==="scroll")&&x.scrollWidth>x.clientWidth+1}).length}';

  var layMode = 0;
  function layRender(m) {
    layMode = m;
    PD.sandbox($('#lay-frame'), {
      id: 'lay', css: layCss(m), probe: LAY_PROBE, onData: layData,
      html: '<div class="tw"><table><thead><tr><th>代號</th><th>名稱</th><th>股數</th>'
        + '<th>均價</th><th>成本</th></tr></thead><tbody>'
        + ROWS.map(function (r) {
          return '<tr>' + r.map(function (c) { return '<td>' + c + '</td>'; }).join('') + '</tr>';
        }).join('') + '</tbody></table></div>'
    });
  }
  function layData(d) {
    var over = d.sw > d.cw + 1;
    PD.ro($('#lay-ro-sw'), d.sw + ' px（可用 ' + d.cw + '）', over ? 'bad' : 'good');
    PD.ro($('#lay-ro-off'), d.off + ' 個', d.off ? 'bad' : 'good');
    PD.ro($('#lay-ro-leg'), d.leg + ' 個', 'good');
    PD.verdict($('#lay-verdict'), over ? 'bad' : 'good', over
      ? '<b>整份文件被撐到 ' + d.sw + 'px，超出 ' + (d.sw - d.cw) + 'px。</b>'
        + '工具會把這 ' + d.off + ' 個元素列進 <code>offenders</code>——這是真問題。'
      : '<b>表格一樣比視窗寬，但頁面沒有被撐開。</b>'
        + '捲動發生在 <code>.tw</code> 裡（工具數到 ' + d.leg + ' 個合法捲動容器），'
        + '<code>offenders</code> 是 0——<b>這不是缺陷，工具不會報它</b>。');
  }
  PD.modes($('#lay-modes'), layRender);
  PD.sizer($('#lay-range'), $('#lay-frame'), $('#lay-w'));

  /* ================= 07 · the lying verdict ================= */

  var vMode = 0, vLast = null;
  PD.sandbox($('#v-frame'), {
    id: 'vtrap',
    css: '.sub{padding:10px;color:#78838f;font-size:12px;'
       + 'white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    html: '<div class="sub"><span id="s">近 12 個月實收・歷年分佈・年度預估・除息日曆（已計入報酬）</span></div>',
    probe: '{clip:Array.from(document.querySelectorAll("*"))'
         + '.filter(function(x){return x.scrollWidth>x.clientWidth+1}).length}',
    onData: function (d) { vLast = d; vRender(); }
  });
  function vRender() {
    if (!vLast) return;
    var broken = vLast.clip > 0;
    PD.ro($('#v-ro-clip'), vLast.clip + ' 個', broken ? 'bad' : 'good');
    PD.ro($('#v-ro-mode'), '現況（壞）', 'bad');
    if (vMode === 0) {
      // 錯誤寫法：verdict 只看量測
      PD.verdict($('#v-verdict'), broken ? 'bad' : 'good', broken
        ? '<b>現況：內容被裁切。</b>（這次剛好說對了）'
        : '<b class="bad">「修法後：任何寬度都不再裁切或撐破。」</b><br>'
          + '——demo 還停在<b>現況（壞）</b>模式，什麼都沒修，'
          + 'verdict 卻宣布修好了。只因為這個寬度剛好沒觸發缺陷。');
    } else {
      PD.verdict($('#v-verdict'), broken ? 'bad' : '', broken
        ? '<b>現況：在這個寬度下被裁切。</b>'
        : '<b>現況：這個寬度剛好沒觸發。</b>把滑桿往左拉，缺陷就會出現——'
          + '這正是為什麼 demo 要能調寬度，而不是一張截圖。');
    }
  }
  PD.modes($('#v-modes'), function (m) { vMode = m; vRender(); });
  PD.sizer($('#v-range'), $('#v-frame'), $('#v-w'));

  /* ================= self-checks =================
     The manual teaches two rule engines. Both are re-implementations, so both must be
     held against what the SHIPPED tools actually printed on this machine (2026-07-27,
     recorded verbatim in the session that built the skill). If any of these go red, the
     manual is teaching something the tool no longer does. */

  var CASES = [
    { n: 'fixture #btn-alive', c: { idState: 'yes', clsRef: true, clsFreq: 4 }, want: 'wired' },
    { n: 'fixture #btn-dead（植入的死按鈕）', c: { idState: 'no', clsRef: true, clsFreq: 4 }, want: 'weak' },
    { n: 'fixture 只有 class 的死按鈕', c: { idState: 'none', clsRef: true, clsFreq: 4 }, want: 'weak' },
    { n: 'fixture data-action=refresh-all', c: { idState: 'none', dataDispatch: true, clsRef: true, clsFreq: 4 }, want: 'wired' },
    { n: 'fixture <a href="other.html">', c: { idState: 'none', link: true }, want: 'wired' },
    { n: '真站 /ledger.html #tab-csv（組合選擇器）', c: { idState: 'no' }, want: 'SUSPECT' },
    { n: '真站 /data-center.html .btn-refresh', c: { idState: 'none', clsRef: true, clsFreq: 4 }, want: 'weak' }
  ];
  CASES.forEach(function (t) {
    PD.checkEq('wiring 判定器 · ' + t.n, classify(t.c).verdict, t.want,
               'probe_live.py wiring 在本機的實際輸出');
  });

  /* The specificity numbers the css section teaches, against the tool's printed values. */
  function spec(sel) {
    var s = sel.replace(/\((?:[^()]|\([^()]*\))*\)/g, ' ');
    return [(s.match(/#[\w-]+/g) || []).length,
            (s.match(/\.[\w-]+/g) || []).length + (s.match(/\[[^\]]+\]/g) || []).length
              + (s.match(/:(?!:)[\w-]+/g) || []).length,
            (s.match(/(^|[\s>+~])[a-zA-Z][\w-]*/g) || []).length
              + (s.match(/::[\w-]+/g) || []).length].join(',');
  }
  PD.checkEq('特異性 · .kpi-band', spec('.kpi-band'), '0,1,0', 'probe_live css 印出 (0,1,0)');
  PD.checkEq('特異性 · .kpi-band.v2', spec('.kpi-band.v2'), '0,2,0', 'probe_live css 印出 (0,2,0)');
  PD.checkEq('特異性 · .topbar', spec('.topbar'), '0,1,0', 'probe_live css 印出 (0,1,0)');

  /* Money formatting never goes through Number (CLAUDE.md invariant 3). */
  PD.checkEq('Decimal 字串千分位（不經 float）', PD.group('5933032.50'), '5,933,032.50',
             'web/format.js 的精確字串格式化語意');
  PD.checkEq('負數也不經 float', PD.group('-1399.07'), '-1,399.07', '同上');

  PD.checkPanel($('#pd-selfcheck'));
})();
