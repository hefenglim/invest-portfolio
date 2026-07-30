/* =============================================================================
 * 外幣期初資金的成本基礎 — 規格 demo
 *
 * 本頁所有數字都是在你的瀏覽器裡「當場算出來的」，用的是 BigInt 定點運算
 * （CLAUDE.md 不變式 3：金額永不經過 JS float），資料則是<測試站>實例
 * 於 2026-07-30T00:37:55+08:00 的真實帳本列（唯讀取自 /api/cash/statement）。
 * 「現況」模式的每一個結果，都在頁尾與 /api/dashboard 的引擎輸出逐項比對。
 * ========================================================================== */

/* ---------------- 精確十進位（BigInt 定點，scale = 20） ---------------- */
var SC = 20, ONE = 100000000000000000000n;

function dec(s) {
  s = String(s).trim();
  var neg = s.charAt(0) === '-';
  if (neg || s.charAt(0) === '+') s = s.slice(1);
  var p = s.split('.'), i = p[0] || '0';
  var f = (p[1] || '') + '00000000000000000000';
  var v = BigInt(i) * ONE + BigInt(f.slice(0, SC));
  return neg ? -v : v;
}
function mul(a, b) { return a * b / ONE; }
function dv(a, b) { return a * ONE / b; }
/** 四捨五入（half away from zero，與 Decimal ROUND_HALF_UP 同義）到 dp 位。 */
function fixed(x, dp) {
  var neg = x < 0n, v = neg ? -x : x;
  var p = 10n ** BigInt(SC - dp), q = v / p, r = v % p;
  if (r * 2n >= p) q += 1n;
  var s = q.toString();
  while (s.length <= dp) s = '0' + s;
  var out = dp ? s.slice(0, s.length - dp) + '.' + s.slice(s.length - dp) : s;
  return (neg && q !== 0n ? '-' : '') + out;
}
/** 把引擎回傳的高精度 Decimal 字串收斂到 dp 位，全程字串／整數運算。 */
function rnd(str, dp) { return fixed(dec(str), dp); }

/* ---------------- 帳本：嘉信 Schwab 的 USD 池，22 列真實資料 ---------------- */
/* [日期, 類型, USD 金額, fx_in 的 TWD 對價 / 其他為代號] */
var ROWS = [
  ['2026-01-05', 'deposit', '100000', '期初資金'],
  ['2026-01-12', 'fx_in', '6875', '220000'],
  ['2026-01-15', 'buy', '-6750', 'AAPL'],
  ['2026-01-15', 'buy', '-4000.00', 'MSFT'],
  ['2026-01-15', 'buy', '-4000.00', 'MSFT'],
  ['2026-02-20', 'buy', '-2060.00', 'MSFT'],
  ['2026-03-01', 'buy', '-1250.00', 'TSLA'],
  ['2026-03-03', 'buy', '-2800', 'NVDA'],
  ['2026-03-20', 'sell', '1299.96', 'TSLA'],
  ['2026-04-01', 'buy', '-720.00', 'TSLA'],
  ['2026-05-15', 'sell', '1659.95', 'MSFT'],
  ['2026-07-02', 'fx_in', '2000', '64000'],
  ['2026-07-03', 'fx_in', '1000', '32000'],
  ['2026-07-03', 'fx_in', '1000', '32000'],
  ['2026-07-19', 'fx_in', '6330', '201000'],
  ['2026-07-19', 'fx_in', '3088.88', '100000'],
  ['2026-07-20', 'fx_in', '168', '5000'],
  ['2026-07-21', 'fx_in', '21474.11', '695000'],
  ['2026-07-21', 'fx_in', '15449.00', '500000'],
  ['2026-07-23', 'buy', '-6765', 'AAPL'],
  ['2026-07-23', 'buy', '-36600', 'TSLA'],
  ['2026-07-30', 'fx_in', '6188.00', '200000']
];
var LAST = '2026-07-30', LAST_F = dec('6188.00'), LAST_H = dec('200000');

/* 引擎輸出（/api/dashboard → fx.by_account.schwab，原字串未經加工） */
var ENG = {
  avg_rate: '32.23066903098312663915917751',
  current_spot: '32.320999',
  foreign_cash: '1587.90',
  foreign_stock_value: '79462.19545664206642066420664',
  unrealized_fx_cash: '143.4349578018932096791420319',
  unrealized_fx_stocks: '7177.817653611213001018425768',
  unrealized_fx_total: '7321.252611413106210697567800',
  cash_basis_incomplete: false,
  funds_view: '101587.90'          /* /api/cash → balances[schwab/USD].amount */
};
var SPOT = dec(ENG.current_spot), STOCK = dec(ENG.foreign_stock_value);
var OPEN_AMT = dec('100000'), OPEN_RATE = dec('31.3587');   /* 2026-01-05 收盤 USD/TWD */

/* ---------------- 匯率池重算：現況公式 vs 提案公式 ----------------
 * 現況 = portfolio_dash/forex/pools.py::foreign_cash_balance
 *        只認 換匯 + 外幣買賣 + 外幣現金股利，完全不讀 cash_movements。
 * 提案 = 同上，再加「具備取得匯率的外幣資金流」。
 */
function calc(withOpening, lastF) {
  var convF = 0n, convH = 0n, cash = 0n, dep = 0n;
  for (var i = 0; i < ROWS.length; i++) {
    var r = ROWS[i], d = dec(r[2]);
    if (r[1] === 'fx_in') {
      if (r[0] === LAST) {
        var f = (lastF === null || lastF === undefined) ? LAST_F : lastF;
        convF += f; convH += f * LAST_H / LAST_F; cash += f;   /* 維持該列的隱含匯率 */
      } else { convF += d; convH += dec(r[3]); cash += d; }
    } else if (r[1] === 'buy' || r[1] === 'sell') { cash += d; }
    else { dep += d; }
  }
  var pool = cash, funds = cash + dep;
  if (withOpening) { pool = funds; convF += OPEN_AMT; convH += mul(OPEN_AMT, OPEN_RATE); }
  var avg = dv(convH, convF), delta = SPOT - avg;
  var uc = mul(pool, delta), us = mul(STOCK, delta);
  return { avg: avg, delta: delta, pool: pool, funds: funds, gap: funds - pool,
           uc: uc, us: us, ut: uc + us };
}

/* =========================== 實驗一：匯率池重算 =========================== */
(function () {
  var mode = 0, slider = PD.$('#lab1-range'), out = PD.$('#lab1-sv');
  function render() {
    var f = dec(slider.value), c = calc(mode === 1, f);
    out.textContent = PD.group(fixed(f, 2));
    /* 4 dp（不是 app 面板的 2 dp）—— 本報告的重點之一就是這個匯率被拉高了 0.53，
       2 dp 會把差異藏起來。值仍走 app 自己的 fmt.num，只是指定小數位。 */
    PD.ro(PD.$('#lab1-ro-a'), fmt.num(fixed(c.avg, 4), 4), mode === 1 ? 'good' : 'bad');
    PD.ro(PD.$('#lab1-ro-b'), fmt.money(fixed(c.pool, 2), 'USD'),
          c.gap === 0n ? 'good' : 'bad');
    PD.ro(PD.$('#lab1-ro-c'), fmt.money(fixed(c.funds, 2), 'USD'), 'good');
    PD.ro(PD.$('#lab1-ro-d'), fmt.money(fixed(c.gap, 2), 'USD'),
          c.gap === 0n ? 'good' : 'bad');
    PD.ro(PD.$('#lab1-ro-e'), fmt.signed(fixed(c.ut, 2), 'TWD'), mode === 1 ? 'good' : 'bad');
    if (mode === 0) {
      PD.verdict(PD.$('#lab1-verdict'), 'bad',
        '<b>現況：匯率池少了 ' + fmt.money(fixed(c.gap, 2), 'USD') + ' USD。</b>' +
        '平均取得匯率被推到 ' + fmt.num(fixed(c.avg, 4), 4) + '（期初那筆較低的取得成本沒有進來），' +
        '所以連「未實現匯損益（股票）」都一起失真——錯的不只是現金那一行。');
    } else {
      PD.verdict(PD.$('#lab1-verdict'), 'good',
        '<b>提案：匯率池 = 資金視角 = ' + fmt.money(fixed(c.pool, 2), 'USD') + ' USD，誤差歸零。</b>' +
        '平均取得匯率降到 ' + fmt.num(fixed(c.avg, 4), 4) + '，未實現匯損益（合計）為 ' +
        fmt.signed(fixed(c.ut, 2), 'TWD') + ' TWD。兩個視角從此可以互相對帳。');
    }
  }
  slider.addEventListener('input', render);
  PD.modes(PD.$('#lab1-modes'), function (m) { mode = m; render(); });
})();

/* ==================== 實驗二：警示是症狀偵測還是成因偵測 ==================== */
(function () {
  var mode = 0, slider = PD.$('#lab2-range'), out = PD.$('#lab2-sv');
  var frame = PD.$('#lab2-frame'), cur = null;

  var CSS = '.p{padding:12px 14px;font-size:12.5px}' +
    '.h{font-weight:700;font-size:13px;margin-bottom:9px;color:#e6e9ee}' +
    '.r{display:flex;justify-content:space-between;gap:10px;padding:5px 0;' +
    'border-bottom:1px dashed #262e39;color:#aab3c0}' +
    '.r b{font-family:ui-monospace,Menlo,Consolas,monospace;font-weight:500;color:#e6e9ee}' +
    '.up{color:#e2564d}.dn{color:#3fa66a}' +
    '#w{margin-top:11px;padding:9px 11px;border-left:3px solid #c8952a;' +
    'background:#c8952a1f;color:#d8c48d;font-size:11.5px;line-height:1.65}';

  function panel(c, warn) {
    var h = '<div class="p"><div class="h">換匯損益 · 嘉信 Schwab　USD → TWD</div>' +
      row('平均取得匯率 → 現時匯率',
          fmt.rate(fixed(c.avg, 6)) + ' → ' + fmt.rate(ENG.current_spot), '') +
      row('外幣現金', fmt.money(fixed(c.pool, 2), 'USD') + ' USD', '', 'c') +
      row('外幣股票市值', fmt.money(ENG.foreign_stock_value, 'USD') + ' USD', '') +
      row('未實現匯損益（現金）', fmt.signed(fixed(c.uc, 2), 'TWD') + ' TWD',
          c.uc < 0n ? 'dn' : 'up') +
      row('未實現匯損益（股票）', fmt.signed(fixed(c.us, 2), 'TWD') + ' TWD',
          c.us < 0n ? 'dn' : 'up') +
      row('未實現匯損益（合計）', fmt.signed(fixed(c.ut, 2), 'TWD') + ' TWD',
          c.ut < 0n ? 'dn' : 'up');
    if (warn) h += '<div id="w">' + warn + '</div>';
    return h + '</div>';
  }
  function row(k, v, cls, id) {
    return '<div class="r"><span>' + k + '</span><b' + (id ? ' id="' + id + '"' : '') +
           (cls ? ' class="' + cls + '"' : '') + '>' + v + '</b></div>';
  }

  function render() {
    var f = dec(slider.value);
    out.textContent = PD.group(fixed(f, 2));
    cur = calc(false, f);
    var warn = null;
    if (mode === 0) {
      /* 現況：AccountFXResult.cash_basis_incomplete = foreign_cash < 0 —— 只看症狀 */
      if (cur.pool < 0n) {
        warn = '⚠ 外幣現金為負 — 此帳戶的外幣入金可能未完整登錄；未實現匯損益（現金）' +
               '是以該負值計算，歸因僅供參考。補登入金／換匯紀錄後此提示會自動消失。';
      }
    } else {
      /* 提案：看成因 —— 有外幣資金流未具取得成本（實作為 covered_ratio < 1；
         規格原本寫的 fx_basis_gap ≠ 0 在池子被買光時也會歸零，見 §04 的實作修正）。 */
      if (cur.gap !== 0n) {
        warn = '⚠ 匯率成本基礎不完整 — 本帳戶有 ' + fmt.money(fixed(cur.gap, 2), 'USD') +
               ' USD 的外幣資金流沒有取得成本，未納入匯率池；帳上實際餘額為 ' +
               fmt.money(fixed(cur.funds, 2), 'USD') + ' USD。上方的「平均取得匯率」' +
               '本身即以不完整的母體計算，因此<b>現金與股票兩條腿都受影響</b>，' +
               '不只是現金。請至資金管理補上取得成本。';
      }
    }
    PD.sandbox(frame, { id: 'lab2', css: CSS, html: panel(cur, warn), onData: onData,
      probe: '{warn:document.getElementById("w")?1:0,' +
             'cash:(document.getElementById("c")||{}).textContent||"",' +
             'rows:document.querySelectorAll(".r").length}' });
  }

  function onData(d) {
    var shown = d.warn === 1;
    PD.ro(PD.$('#lab2-ro-a'), d.cash + ' USD', cur.pool < 0n ? 'bad' : '');
    PD.ro(PD.$('#lab2-ro-b'), shown ? '有出現' : '沒有出現', shown ? 'good' : 'bad');
    PD.ro(PD.$('#lab2-ro-c'), fmt.money(fixed(cur.gap, 2), 'USD') + ' USD',
          cur.gap === 0n ? 'good' : 'bad');
    if (mode === 0) {
      PD.verdict(PD.$('#lab2-verdict'), shown ? '' : 'bad', shown
        ? '<b>現況：此刻池子是負的，所以警示有出現。</b>但把滑桿往右拉過 ' +
          '4,600.10 USD，池子轉正、警示立刻消失——而右邊那個「誤差」讀數' +
          '完全沒有變。警示追的是症狀，不是成因。'
        : '<b>現況：警示消失了，可是誤差仍然是 ' + fmt.money(fixed(cur.gap, 2), 'USD') +
          ' USD。</b>畫面上沒有任何提示，數字卻是錯的——這正是 2026-07-30 00:37 ' +
          '線上實例的真實狀態（那筆 +6,188 USD 換匯把池子推正了）。');
    } else {
      PD.verdict(PD.$('#lab2-verdict'), 'good', shown
        ? '<b>提案：不論池子正負，只要有「沒有取得匯率的外幣資金流」就一直警示，' +
          '而且直接說出缺口金額與正確餘額。</b>滑桿怎麼拉都不會靜默。'
        : '<b>提案：此情境下沒有缺口，因此不警示——這是正確的沉默。</b>');
    }
  }

  slider.addEventListener('input', render);
  PD.modes(PD.$('#lab2-modes'), function (m) { mode = m; render(); });
})();

/* ============ 實驗二（F2）：相減法 vs 比例法，流出從哪個桶扣 ============
 * 情境：期初那 100,000 USD 沒有填成本。有基礎的取得額只有換匯的 63,572.99，
 * 家幣成本 2,049,000 TWD。相減法在餘額跌破 100,000 時會給出負基數——
 * 也就是這次修法要根除的那個反號假數字，從另一個入口長回來。
 */
(function () {
  var mode = 0, slider = PD.$('#lab4-range'), out = PD.$('#lab4-sv');
  var A_WITH = dec('63572.99'), H_WITH = dec('2049000'), A_NONE = dec('100000');
  var AVG = dv(H_WITH, A_WITH), DELTA = SPOT - AVG;
  var RATIO = dv(A_WITH, A_WITH + A_NONE);

  function render() {
    var B = dec(slider.value);
    out.textContent = PD.group(fixed(B, 2));
    var wb = (mode === 0) ? (B - A_NONE) : mul(B, RATIO);
    var uc = mul(wb, DELTA), neg = wb < 0n;
    PD.ro(PD.$('#lab4-ro-a'), fmt.money(fixed(B, 2), 'USD') + ' USD', 'good');
    PD.ro(PD.$('#lab4-ro-b'), fmt.money(fixed(wb, 2), 'USD') + ' USD', neg ? 'bad' : 'good');
    PD.ro(PD.$('#lab4-ro-c'), fmt.signed(fixed(uc, 2), 'TWD') + ' TWD', neg ? 'bad' : 'good');
    PD.ro(PD.$('#lab4-ro-d'), neg ? '是 — 反號假數字回來了' : '否', neg ? 'bad' : 'good');
    if (mode === 0) {
      PD.verdict(PD.$('#lab4-verdict'), neg ? 'bad' : '', neg
        ? '<b>相減法給出負基數 ' + fmt.money(fixed(wb, 2), 'USD') + ' USD。</b>' +
          '帳上明明有 ' + fmt.money(fixed(B, 2), 'USD') + ' USD，卻用一個負數去乘匯差——' +
          '這正是本次修法要消滅的那個錯誤，只是換了個入口。'
        : '<b>此刻餘額高於無基礎金額，相減法碰巧是正的。</b>把滑桿拉到 100,000 以下，' +
          '它立刻翻負——一個公式的正確性取決於餘額大小，而不是取決於帳務邏輯，就是錯的公式。');
    } else {
      PD.verdict(PD.$('#lab4-verdict'), 'good',
        '<b>比例法：覆蓋率 ' + fmt.num(fixed(mul(RATIO, dec('100')), 2), 2) +
        '%，基數 = 餘額 × 覆蓋率 = ' + fmt.money(fixed(wb, 2), 'USD') + ' USD。</b>' +
        '現金是可替代物，流出按比例扣，任何餘額下都不會為負。' +
        '若期初那筆補上成本，覆蓋率變 100%，這條路徑就完全不會啟動。');
    }
  }
  slider.addEventListener('input', render);
  PD.modes(PD.$('#lab4-modes'), function (m) { mode = m; render(); });
})();

/* ==================== 實驗四：登錄時的取得成本與降級路徑 ==================== */
(function () {
  var mode = 0;
  var ACCT = { schwab: { name: '嘉信 Schwab', home: 'TWD' },
               moomoo_my: { name: 'Moomoo MY', home: 'MYR' },
               tw_broker: { name: '台股券商', home: 'TWD' } };
  /* 取自兩個實例的 fx_rates（唯讀查詢）。prod 目前只有 2026-07-01 之後的資料。 */
  var RATES = {
    'USD/TWD': { '2026-01-05': '31.358700', '2026-02-02': '31.583000',
                 '2026-03-02': '31.375', '2026-04-01': '31.937901',
                 '2026-05-01': '31.569000', '2026-06-01': '31.412901',
                 '2026-07-01': '31.834400', '2026-07-23': '32.320999' },
    'USD/MYR': { '2026-01-05': '4.051500', '2026-02-02': '3.939500',
                 '2026-03-02': '3.888800', '2026-04-01': '4.046500',
                 '2026-05-01': '3.967500', '2026-06-01': '3.968000',
                 '2026-07-01': '4.082200', '2026-07-23': '4.087000' }
  };
  var PROD_FROM = '2026-07-01';   /* 實測：prod fx_rates 最早 2026-07-01（共 21 天） */

  var els = ['acct', 'ccy', 'date', 'src', 'amt'].map(function (k) {
    return PD.$('#lab3-' + k);
  });

  function render() {
    var acct = ACCT[els[0].value], ccy = els[1].value, day = els[2].value;
    var prodOnly = els[3].value === 'prod', amt = els[4].value || '0';
    var foreign = ccy !== acct.home;
    var pair = ccy + '/' + acct.home;
    var have = foreign && RATES[pair] && RATES[pair][day];
    if (prodOnly && day < PROD_FROM) have = null;

    var field, rate, into;
    if (mode === 0) {
      field = '不存在';
      rate = '—（現況沒有這個欄位）';
      into = foreign ? '0（外幣資金流完全不進池）' : '不適用（本幣）';
    } else if (!foreign) {
      field = '不出現（本幣入金）';
      rate = '不適用';
      into = '不適用——本幣資金流本來就不是匯率曝險';
    } else {
      field = '出現';
      rate = have ? ('參考值 ' + fmt.rate(have) + '（' + day + ' 收盤，可改）')
                  : '查無 ' + day + ' 的 ' + pair + ' 匯率 → 欄位留白，請手動輸入';
      /* F1：帳本存的是家幣金額，不是匯率；顯示用的匯率一律讀取時再除回來。 */
      into = have
        ? fmt.money(fixed(mul(dec(amt), dec(have)), 2), acct.home) + ' ' + acct.home +
          '（= ' + PD.group(amt) + ' × ' + fmt.rate(have) + '，存金額不存匯率）'
        : '若留白：計入餘額，依覆蓋率按比例排除於兩條腿之外，並標記缺口';
    }
    PD.ro(PD.$('#lab3-ro-a'), foreign ? '是（' + ccy + ' ≠ 資金幣別 ' + acct.home + '）'
                                      : '否（' + ccy + ' = 資金幣別）', foreign ? '' : 'good');
    PD.ro(PD.$('#lab3-ro-b'), field, mode === 0 && foreign ? 'bad' : 'good');
    PD.ro(PD.$('#lab3-ro-c'), rate, (mode === 1 && foreign && !have) ? 'bad' : '');
    PD.ro(PD.$('#lab3-ro-d'), into, mode === 0 && foreign ? 'bad' : 'good');

    if (mode === 0) {
      PD.verdict(PD.$('#lab3-verdict'), foreign ? 'bad' : '', foreign
        ? '<b>現況：登錄一筆外幣入金，系統不會問你取得匯率，也沒有地方能記。</b>' +
          '這筆錢因此永遠進不了匯率池——不是使用者忘了填，是根本沒有欄位可填。'
        : '<b>現況：本幣入金沒有問題。</b>把幣別改成該帳戶的外幣，缺口才會出現。');
    } else {
      PD.verdict(PD.$('#lab3-verdict'), have || !foreign ? 'good' : '', !foreign
        ? '<b>提案：本幣入金維持原樣，不多問一個欄位。</b>'
        : (have
          ? '<b>提案：欄位只在幣別 ≠ 資金幣別時出現，並預帶當日匯率，你可以改。</b>' +
            '確認後這筆錢就帶著成本基礎進入匯率池。'
          : '<b>提案：查無當日匯率時不猜、不填 0，欄位留白讓你手動輸入。</b>' +
            '留白也可以送出——金額照樣計入餘額，只是標記為「無成本基礎」並排除於' +
            '匯損益之外。正式站的 fx_rates 目前只回溯到 ' + PROD_FROM +
            '，所以這條降級路徑是必要的，不是防禦性設計。'));
    }
  }
  els.forEach(function (e) { e.addEventListener('input', render); });
  PD.modes(PD.$('#lab3-modes'), function (m) { mode = m; render(); });
})();

/* ======================= 自我校驗：離線重算 vs 真實引擎 ======================= */
var CUR = calc(false, null), PROP = calc(true, null), SHOT = calc(false, 0n);

PD.checkEq('現況・匯率池外幣現金', fixed(CUR.pool, 2), rnd(ENG.foreign_cash, 2),
           '/api/dashboard → fx.by_account.schwab.foreign_cash');
PD.checkEq('現況・平均取得匯率（8 dp）', fixed(CUR.avg, 8), rnd(ENG.avg_rate, 8),
           '引擎原值 ' + ENG.avg_rate);
PD.checkEq('現況・未實現匯損益（現金）', fixed(CUR.uc, 2), rnd(ENG.unrealized_fx_cash, 2),
           '引擎原值 ' + ENG.unrealized_fx_cash);
PD.checkEq('現況・未實現匯損益（股票）', fixed(CUR.us, 2), rnd(ENG.unrealized_fx_stocks, 2),
           '引擎原值 ' + ENG.unrealized_fx_stocks);
PD.checkEq('現況・未實現匯損益（合計）', fixed(CUR.ut, 2), rnd(ENG.unrealized_fx_total, 2),
           '引擎原值 ' + ENG.unrealized_fx_total);
PD.checkEq('提案・匯率池 = 資金視角餘額', fixed(PROP.pool, 2), rnd(ENG.funds_view, 2),
           '跨端點對帳：/api/cash → balances[schwab/USD].amount。兩個視角相等，' +
           '正是本提案要達成的結果');
PD.checkEq('截圖當下（扣掉 2026-07-30 那筆換匯）的匯率池', fixed(SHOT.pool, 2), '-4600.10',
           '引擎值來自 owner 於 2026-07-30 00:04 的畫面截圖，並由 /api/cash/statement ' +
           '的帳本列重算複驗');
PD.check('現況旗標 cash_basis_incomplete 與池子正負一致',
         (CUR.pool < 0n) === ENG.cash_basis_incomplete,
         '池子 ' + fixed(CUR.pool, 2) + ' → 旗標應為 ' + (CUR.pool < 0n) +
         '，引擎回報 ' + ENG.cash_basis_incomplete + '（＝警示此刻不會顯示）');
PD.checkEq('金額格式化走 app 自己的 web/format.js', fmt.money('101587.90', 'USD'),
           '101,587.90', 'Decimal 字串進、字串出，全程未經 JS float');

/* F2 的兩個性質檢查（提案規則的性質，非與引擎比對——提案尚未實作） */
(function () {
  var A_WITH = dec('63572.99'), A_NONE = dec('100000'), B = dec('50000');
  var ratio = dv(A_WITH, A_WITH + A_NONE);
  PD.check('F2・比例法在餘額 50,000（低於無基礎額）仍不為負', mul(B, ratio) >= 0n,
           '比例法基數 = ' + fixed(mul(B, ratio), 2) + ' USD');
  PD.check('F2・相減法在同一情境確實翻負（所以不採用）', (B - A_NONE) < 0n,
           '相減法基數 = ' + fixed(B - A_NONE, 2) + ' USD —— 反號假數字');
})();

PD.checkPanel(PD.$('#pd-selfcheck'));
