# 18 — 核心計算正確性(Bug-Free 強化)(P0 — 計算層的驗收憲法)

> 持倉、成本、費稅、報酬率、試算 — 這些數字直接驅動投資決策,正確性優先於一切功能。
> 本 spec 是 spec 17 的深化:定義**費率真值表、手算對照表(worked examples)、
> 會計恆等式、性質測試、Decimal 紀律**。計算核心的完成定義 = 本 spec 全部測試綠
> ＋分支覆蓋率 100%(`portfolio/`、`forex/`、`data_ingestion/fees.py`)。

## 18.0 費率真值表(SR 發現:四處來源互相矛盾 — 本表為唯一真值)

審查發現費率散落四處且不一致(後端 `config_seed.py` US/MY 為 placeholder、
`input-mock-data.js`、`detail.js feeTax()`、`ledger.js` mock 三處前端鏡像各寫各的)。
**定案:下表(取自使用者設計稿之前端值)為暫定真值,回填 `config_seed.FEE_RULES`;
四處前端鏡像接線後一律改讀 API,刪除 hardcode。**

| 帳戶 | 買進 | 賣出加收 | 進位 |
|---|---|---|---|
| tw_broker | 0.1425%×折扣1.0,最低 NT$20 | 證交稅 0.3%(ETF 0.1%、當沖 0.15%) | 整數 NT$,ROUND_HALF_UP |
| schwab | $0 佣金 | SEC fee 0.00278%(0.0000278)× 成交額 | 0.01 USD,ROUND_HALF_UP |
| moomoo_my_us | **平台費 USD 0.99/筆(固定)** | 同左+SEC fee 0.0000278 | 0.01 USD |
| moomoo_my_my | 佣金 0.08%,最低 RM3 + 清算費 0.03%(cap RM1000) | 印花稅 0.1%×成交額(買賣皆收,RM cap 依現行法規待確認) | 0.01 MYR |

⚠ **待使用者最終確認**(各券商實際費率以您的對帳單為準):SEC fee 年度費率、
馬股印花稅 cap、Moomoo 平台費是否買賣皆收。確認前以上表開發,測試以上表為錨。

### 18.0.1 `FeeRuleSet` 結構缺口(必須先修,否則真值表無法表達)

1. **新增 `flat_fee: Decimal = 0`(每筆固定費)** — Moomoo US 0.99/筆目前無欄位可放;
   US 分支公式改為 `fee = flat_fee + brokerage×notional (+ sec_fee×notional if SELL)`。
2. **US/MY 分支補套 `min_fee`**(現只有 TW 分支使用)— 馬股最低 RM3 需要它。
3. **`stamp_duty` 語義修正**:現行 `fee += stamp_duty`(當常數)→ 改 `stamp_duty_rate × notional`,
   cap 另設 `stamp_duty_cap: Decimal | None`。
4. **TW `min_fee` 守門**:`notional == 0` 不得收最低費(validate 層已擋 shares>0,
   仍須在 `compute_fees` 加 `if notional > 0` 防衛 — 與前端 input.js 行為一致)。

## 18.1 手算對照表(Worked Examples — 合約測試的精確期望值)

每一筆均為人工以真值表推導的精確 Decimal,寫入 `tests/contract/test_fee_worked_examples.py`,
**期望值 hardcode 在測試中,禁止由被測程式生成**:

| # | 情境 | 手算過程 | fee | tax | total |
|---|---|---|---|---|---|
| W1 | TW 買 1000×612.5 | 612500×0.001425=872.8125→873 | 873 | 0 | −613373 |
| W2 | TW 賣 200×598(非 ETF) | fee 170.43→170;tax 119600×0.003=358.8→359 | 170 | 359 | +119071 |
| W3 | TW 賣 2000×38.6(ETF) | fee 110.01→110;tax 77200×0.001=77.2→77 | 110 | 77 | +77013 |
| W4 | TW 買 100×38.6(觸最低費) | 3860×0.001425=5.5005→max(·,20)=20 | 20 | 0 | −3880 |
| W5 | TW 當沖賣 200×595 | tax 119000×0.0015=178.5→**179**(HALF_UP 邊界) | 170 | 179 | +118651 |
| W6 | Schwab 賣 5×200.50 | SEC 1002.50×0.0000278=0.0278695→0.03 | 0 | 0.03 | +1002.47 |
| W7 | Moomoo US 買 10×165.20 | flat 0.99 | 0.99 | 0 | −1652.99 |
| W8 | Moomoo MY 買 300×9.62 | 佣 2886×0.0008=2.3088→max(·,3)=3;清算 0.87;印花 2.886→2.89 | 3.87 | 2.89 | −2892.76 |
| W9 | Moomoo MY 大額觸 cap | notional 4,000,000:清算 1200→cap 1000(驗 cap 路徑) | — | — | — |

(W2/W3/W7 與 ledger mock 一致 ✓;W6 修正 ledger mock 的 0.04 → 0.03,以真值表公式為準;
W8 與 ledger mock 3.00/2.89 差異源於 min_fee — mock 未含佣金細節,以本表為準。)

### 成本與配息恆等(黃金資料集即驗,mock 數字已人工核對 ✓)
- 2330:adjusted_avg = 500 − 5000/1000 = **495**;payback = 5000/500000 = **0.0100**
- 0056:36.20 − 13500/10000 = **34.85**;payback = 13500/362000 = **0.0373**(4 位截斷顯示)
- realized(2330):119350 − 98000 = **21350**

## 18.2 會計恆等式(每次回歸全量驗證,黃金庫＋隨機帳本)

對**任意**帳本狀態(黃金庫 + hypothesis 隨機生成),以下恆等式必須成立:

```
I1  market_value      = shares × market_price                 (原幣,逐持倉)
I2  unrealized_pnl    = market_value − adjusted_cost_total
I3  capital_gain      = market_value − original_cost_total
I4  adjusted_cost_total = original_cost_total − dividend_portion   (現金沖減模型)
I5  realized          = proceeds_net − adjusted_cost_removed       (逐已實現列)
I6  total_return(ccy) = realized(ccy) + unrealized(ccy)            (逐幣別,絕不跨幣)
I7  kpis.total_return = Σ ccy→reporting 換算(同一組 spot)
                      = realized_total + unrealized_total          (報告幣別自洽)
I8  Σ weight_i        = 1 ± 1e-9(排除缺價持倉;缺價者 weight=null)
I9  股數守恆:期初+Σ買−Σ賣 = 現持股;任意前綴不為負(OversellError)
I10 重放確定性:同一帳本 build 兩次 → 全輸出 bit-identical
I11 FX:implied_rate = from_amt/to_amt;池均價 = Σfrom/Σto;
      unrealized_fx_cash = foreign_cash × (spot − avg_rate)
I12 帳本不可變:任何 GET/試算呼叫前後,四帳本表內容雜湊不變
```

## 18.3 性質測試(hypothesis,計算核心全覆蓋)

```
P1 任意合法交易序列重放:I1–I9 全程成立(stateful test, RuleBasedStateMachine)
P2 買 n×p1 → 全賣 n×p2:realized = n×(p2−p1) − fee_buy − fee_sell − tax_sell(精確 Decimal)
P3 費用單調性:notional 增 → fee 不減(固定規則集)
P4 線性區可加性:無 min_fee/flat_fee 規則下,fee(a+b 股) = fee(a)+fee(b) ± 進位 1 單位
P5 序列化往返:Decimal → JSON str → Decimal 無損(所有 API model)
P6 XIRR 收斂:已知解析解案例(−100 → 一年 +110 ⇒ 0.10)誤差 < 1e-9;
   現金流順序打亂不影響結果;無解情境回 null 不丟例外
P7 配息沖減不變量:任意配息序列後 adjusted_avg ≤ original_avg;
   adjusted_avg 可為負(超額回本)時 payback_ratio > 1 — 行為明確不是 bug(顯示層處理)
```

## 18.4 Decimal 紀律(機械強制,非約定)

1. **進位政策 pinned**:全系統唯一進位 = `ROUND_HALF_UP`(fees.py 現行);
   TW 量化到 `1`、USD/MYR 到 `0.01`、匯率 4 位、權重/比率 4 位(僅顯示層)。
   任何新計算函式引用共用 `money.quantize_*` helper,禁止散落 `.quantize()`。
2. **float 禁令(AST 檢查)**:CI 腳本掃 `portfolio/ forex/ data_ingestion/` —
   金額路徑出現 `float(`、`round(`(內建)、float 字面量參與 Decimal 運算 → 紅。
   `Decimal(0.1)`(float 建構)→ 紅;必須 `Decimal("0.1")`。
3. **API 邊界**:request 數值收 string|number,進入即轉 `Decimal(str(x))`;
   response 一律字串(spec 08 §8.0)— 合約測試對每個金額欄位斷言 `isinstance(v, str)`。

## 18.5 三方一致性(同一引擎證明 — 杜絕「試算與實際不符」)

對同一組輸入(帳戶×標的×方向×股數×價格):

```
POST /api/whatif 的 fee/tax
  == POST /api/input/manual/preview 的 fee/tax
  == commit 後帳本列的 fee/tax(及 fee_snapshot)
  == POST /api/rebalance/preview 對應列的 fee/tax
```

實作要求:四者**呼叫同一個 `compute_fees`**;測試以 monkeypatch 計數器證明
同一函式被呼叫(不是四份各自實作恰好相等)。任何一處繞過共用引擎 = 架構違規。

## 18.6 覆蓋率與強度門檻

- 分支覆蓋率:`portfolio/`、`forex/`、`fees.py`、`cost_basis.py`、`returns.py` = **100%**
  (其餘模組 ≥90%);`pytest --cov --cov-branch --cov-fail-under` 進 `make all`。
- 變異測試(`mutmut`,選配但建議):對 `fees.py`/`cost_basis.py` 跑一輪,
  存活突變體 = 測試矩陣盲區,須補 case 直到關鍵運算子(+/−/×/比較/進位)全殺。

## 18.7 Claude Code 執行守則(疊加 spec 17 §17.7)

1. 任何 worked example 不平 → **先懷疑程式、後懷疑手算**;若確認手算錯,
   修正本表須在 commit message 附完整推導過程。
2. 恆等式失敗禁止以容差掩蓋:Decimal 比對一律 `==`(僅 XIRR 牛頓法允許 1e-9)。
3. 發現計算 bug → 先在 18.1 表補一行重現該 bug 的 worked example,再修 —
   每個 bug 永久留下回歸錨。
