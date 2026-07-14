# 投資組合會計公式手冊（Accounting-Formula Manual）

> **版本**：`v1.0-draft`（2026-07-15）
> **程式碼基線**：`v0.1.18 + feat/p3-batch3`
> **仲裁狀態**：**待 owner 確認**（pending owner confirmation）。經 owner 正式簽署後，本文件即成為
> 站上任何「金額爭議」的**唯一仲裁標準**（arbitration standard）。
> **語言例外**：本文件採**繁體中文正文 + 英文技術識別字**（欄位／資料表／函式名），為一份 owner
> 面向的仲裁文件，係對 repo「工件一律英文」規則之**刻意且經標示的例外**。
> **工程來源**：`.claude/rules/` 下之英文規則檔（`domain-ledger.md`、`markets-and-fees.md`、
> `data-and-pricing.md` …）仍為本文件所編纂之**工程正本**；本文件與程式碼、規則檔三者若有出入，以
> 本文件標示之「已驗證」數字與其引用之程式碼為準，並回報衝突。
>
> **驗證基礎**：本文件所有帶數字之工作範例，均取自或核對於一組 **966 項對抗性對帳斷言**
> （adversarial reconciliation，`stress/oplog.jsonl` + `stress/assertions.jsonl`，對本程式碼
> **966/966 全數通過**）。每一數字範例均標注其 `scope` 驗證錨點。手冊作者未自行捏造任何數字。

---

## 目錄

1. [總則與精度規範](#1-總則與精度規範)
2. [帳戶／市場／幣別模型](#2-帳戶市場幣別模型)
3. [費用與交易稅公式](#3-費用與交易稅公式)
4. [成本基礎（加權平均）](#4-成本基礎加權平均)
5. [已實現／未實現損益](#5-已實現未實現損益)
6. [股息三模型](#6-股息三模型)
7. [總報酬與報酬率（含 XIRR）](#7-總報酬與報酬率含-xirr)
8. [換匯損益（FX P&L）](#8-換匯損益fx-pl)
9. [現金池與對帳單](#9-現金池與對帳單)
10. [更正、稽核與重算](#10-更正稽核與重算)
11. [再平衡試算](#11-再平衡試算)
12. [附錄](#12-附錄)

---

## 1. 總則與精度規範

### 1.1 仲裁條款（Arbitration Clause）

站上任一顯示金額若生爭議，**依本手冊對應章節之公式 + 其引用之四本永久帳本（ledgers）逐筆重算**（重算
／replay），重算結果即為裁定值。任何 UI 顯示、快取、或口頭記憶皆不得凌駕帳本重算。裁定程序見
[§12.4 如何仲裁](#124-如何仲裁一個爭議金額)。

### 1.2 核心不變式（Invariants — 違反即為 bug，非選擇）

| # | 不變式 | 出處 |
| --- | --- | --- |
| I1 | **金額永不使用 `float`**：價格、數量、費率、金額全程 `Decimal`。 | `shared/money.py` |
| I2 | **原始成本 `original_total` 永不被覆寫**；所有報表由帳本重建。 | `domain-ledger.md` |
| I3 | **報價數字來自財經 API，永不來自 LLM**。 | `data-and-pricing.md` |
| I4 | **股息只計入總報酬一次**（經成本調整，非另立收入行）。 | §6 |
| I5 | **換匯損益是報表幣別總報酬的「拆解」，永不加疊於上**。 | §8 |
| I6 | **費用／稅綁定「帳戶」，非「市場」**。 | §2、§3 |
| I7 | **均價一律 on read 計算，永不儲存四捨五入後的均價為權威值**。 | §4 |

### 1.3 精度模型（非可議）

**儲存精度（不得截斷）**

| 種類 | 儲存精度 | 寫入接縫（write seam）之上限（cap） | 實作 |
| --- | --- | --- | --- |
| 交易價格 `price` | 市場最細 tick（US/TW 2 dp、**MY 至 3 dp**） | **4 dp**，`ROUND_HALF_UP`，**只截不補**（cap-not-pad） | `data_ingestion/store.py::_cap_price`（`_PRICE_DP=4`） |
| 行情價 `prices.close`（OHLC） | 同上 | **4 dp** 同上（唯一價格寫入接縫） | `pricing/store.py::_cap_dp`（`_PRICE_DP=4`） |
| 匯率 `fx_rates.rate` | 高精度（4–6 dp；匯率非金額，2-dp 規則不適用） | **6 dp**，`ROUND_HALF_UP`，只截不補 | `pricing/store.py::_cap_dp`（`_FX_DP=6`） |
| 均價（average cost） | **不存**；存 `total_cost` + `shares`，on read 相除（見 §4） | — | `portfolio/cost_basis.py` |

> **「只截不補」**：乾淨值（如 `130`、`9.50`）位元組完全不變地存入；只有浮點雜訊尾
> （如 `305.364990234375`）被截到 4 dp。此為**移除表示雜訊，非丟失資訊**。

**金額精度（per-currency minor unit，於結算／顯示時套用）**

| 幣別 | minor unit | 小數位 | 定義 |
| --- | --- | --- | --- |
| `TWD` | 整數新台幣 | **0 dp** | 費／稅四捨五入至整數 NT$ |
| `USD` | cent | **2 dp** | — |
| `MYR` | sen | **2 dp** | — |

實作：`shared/money.py::MINOR_UNITS = {TWD:0, USD:2, MYR:2}`。

**量化（quantization）發生的唯一時機**：**結算／顯示**，透過
`shared/money.py::quantize_amount(value, currency, ROUND_HALF_UP)`。價格與匯率**不**在此量化（維持全
精度）。所有貨幣換算一律走單一 helper `shared/fx.py::convert(amount, rate)`（`rate` 定義為「1 單位來源
幣 = rate 單位目標幣」），禁止任何模組散落自行乘率。

**持久化格式**：`Decimal` 以**canonical 定點字串（TEXT）**存（`money.py::to_db` / `from_db`），拒絕
`float` 與非有限值（NaN／Inf），保證 `from_db(to_db(x)) == x` 無損往返。

### 1.4 重算原則（Rebuild / 重算）

四本**永久真實來源**：`opening_inventory`（期初庫存）、`transactions`（交易）、`dividends`（股利）、
`fx_conversions`（換匯）。**所有**衍生數字（持倉、成本、已實現／未實現、報酬、換匯損益、現金餘額）皆
於讀取時由這四本**按日期順序重播（replay）**算出，不以「算好的結果」作為真實來源（除非量測顯示需
快取）。裁定時一律以重算為準。

> **實作位置**：`shared/money.py`、`shared/fx.py`、`data_ingestion/store.py`、`pricing/store.py`、
> `portfolio/cost_basis.py`。
> **依據**：`.claude/rules/data-and-pricing.md`（Money & numeric precision model）、`CLAUDE.md`（Core invariants）。

---

## 2. 帳戶／市場／幣別模型

三個正交維度：**市場（market，在哪交易）· 帳戶（account，哪家券商持有）· 幣別（currency，標的報價
幣）**。**同一市場可橫跨多個帳戶且規則各異**，故費／稅／股利規則綁定**帳戶**（invariant I6）。

| `account_id` | 名稱 | 市場 | 交割幣 `settlement_ccy` | 資金幣 `funding_ccy` | 股利模型 `dividend_model` | 費規則集 `fee_rule_set` |
| --- | --- | --- | --- | --- | --- | --- |
| `tw_broker` | TW Broker | TW | TWD | TWD | `cash_cost_reduction`（現金→降成本） | `tw` |
| `schwab` | Charles Schwab | US | USD | **TWD** | `drip_us`（DRIP，30% 預扣） | `schwab` |
| `moomoo_my_us` | Moomoo MY (US) | US | USD | **MYR** | `drip_us`（DRIP，30% 預扣） | `moomoo_us` |
| `moomoo_my_my` | Moomoo MY (MY) | MY | MYR | MYR | `cash`（單層淨額） | `moomoo_my` |

要點：

- **US 市場橫跨 `schwab` 與 `moomoo_my_us`，成本結構不同** → 正是費規則綁帳戶的理由。
- Moomoo MY 為**一個券商帳戶**，同時持有 USD 交割之美股（經 MYR→USD 換匯供資）與 MYR 交割之馬股，故拆為
  兩個 `account_id`（`moomoo_my_us` / `moomoo_my_my`）。
- 交易列帶 `account_id` + `symbol`；`instruments` 表知道該 symbol 的 `market` 與 `quote_ccy`。
- 換匯 pool 之**本位幣（home）= 帳戶的 `funding_ccy`**：Schwab USD pool 錨定 **TWD**，Moomoo USD pool
  錨定 **MYR**（見 §8）。

> **實作位置**：`data_ingestion/config_seed.py::DEFAULT_ACCOUNTS`、`shared/models/assets.py`（`Account` /
> `Instrument`，含 `is_etf`）。
> **依據**：`.claude/rules/domain-ledger.md`（Accounts）、`.claude/rules/markets-and-fees.md`。

---

## 3. 費用與交易稅公式

**單一實作**：`data_ingestion/fees.py::compute_fees(rules, side, quantity, price, *, is_etf, daytrade)`。
`notional = quantity × price`。回傳 `FeeResult{fee, tax, snapshot}`，其中 **`snapshot` 為當筆使用之費率快照**，
逐筆存於 `transactions.fee_rule_snapshot`，使規則日後變動仍能重現歷史（invariant I2 之延伸）。

種子費率見 `config_seed.py::FEE_RULES`（標注「pending real-statement confirmation」之項見 §3.5）。

### 3.1 TW（`tw_broker` → 規則集 `tw`，`market = TW`，`round_integer = True`）

$$\text{fee} = \operatorname{round}_{\mathbb{Z}}\Big(\max\big(\text{brokerage}\times\text{discount}\times\text{notional},\ \text{min\_fee}\big)\Big),\quad \text{買賣皆有}$$

$$\text{tax} = \operatorname{round}_{\mathbb{Z}}\big(\text{rate}\times\text{notional}\big),\quad \text{僅賣方}$$

其中賣方稅率依序判定：

$$\text{rate} = \begin{cases} \text{tax\_daytrade} = 0.0015 & \text{當沖 } daytrade=\text{True}\\ \text{tax\_etf} = 0.001 & is\_etf=\text{True}\\ \text{tax\_normal} = 0.003 & \text{現股（預設）}\end{cases}$$

種子值：`brokerage = 0.001425`、`discount = 1`、`min_fee = 20`（NT$）。`round_integer=True` → 費與稅皆
`ROUND_HALF_UP` 至**整數 NT$**。買方 `tax = 0`。

- **`is_etf` 來源**：標的 **registry**（`instruments.is_etf`，唯一真實來源，**永不由 sector 推導**）。
- **`daytrade`**：**逐筆旗標**，寫入並**持久化於 `transactions.daytrade`**，使重算能重現當沖稅率（見 §10）。

**已驗證範例**

| 情境 | notional | fee | tax | 驗證錨點（`scope`） |
| --- | ---: | ---: | ---: | --- |
| 2330 買 1,000@600 | 600,000 | `max(855, 20)=` **855** | 0 | `fee_engine.fee/tax tw_broker/2330 buy 1000@600 id=1` |
| 2330 賣 300@700（現股） | 210,000 | round(299.25)=**299** | round(0.003×210,000)=**630** | `fee_engine.fee/tax tw_broker/2330 sell 300@700 id=18` |
| 0050 賣 50@140（**ETF**） | 7,000 | max(9.975,20)=**20** | round(0.001×7,000)=**7** | `fee_engine.fee/tax tw_broker/0050 sell 50@140 id=21` |
| 0050 買 10@130（**min 生效**） | 1,300 | max(1.8525,20)=**20** | 0 | — |

> 當沖比較：同一 0050 賣 50@140 若 `daytrade=True`，tax = round(0.0015×7,000)= **11**（本手冊以
> `compute_fees` 純函式重現；壓測情境未觸發當沖）。

### 3.2 US — Schwab（規則集 `schwab`，`market = US`）

$$\text{fee} = \underbrace{\text{flat\_fee}}_{=0} + \underbrace{\text{brokerage}}_{=0}\times\text{notional} + \big[\,\text{side}=\text{SELL}\,\big]\cdot \text{sec\_fee}\times\text{notional}$$

$$\text{tax} = 0.00 \quad(\text{美股無交易稅})$$

種子值：`sec_fee = 0.0000278`（賣方 SEC/TAF 監理費率）。`fee` 量化至 **2 dp**（`ROUND_HALF_UP`）。

**已驗證範例**

| 情境 | fee | tax | 驗證錨點 |
| --- | ---: | ---: | --- |
| AAPL 買 100@180 | **0.00** | 0.00 | `fee_engine.fee/tax schwab/AAPL buy 100@180 id=7` |
| TSLA 賣 20@260（sec fee） | 0.0000278×5,200 = 0.14456 → **0.14** | 0.00 | `fee_engine.fee/tax schwab/TSLA sell 20@260 id=23` |

### 3.3 US — Moomoo（規則集 `moomoo_us`，`market = US`）

同 §3.2，另加**每筆固定平台費** `flat_fee = 0.99`（USD）：

$$\text{fee} = 0.99 + \big[\,\text{SELL}\,\big]\cdot 0.0000278\times\text{notional}$$

**已驗證範例**

| 情境 | fee | 驗證錨點 |
| --- | ---: | --- |
| NVDA 買 30@500 | 0.99 → **0.99** | （oplog `op20` total = −15,000.99） |
| NVDA 賣 25@600 | 0.99 + 0.0000278×15,000 = 0.99+0.417 → **1.41** | `fee_engine.fee moomoo_my_us/NVDA sell 25@600 id=27` |

### 3.4 MY（`moomoo_my_my` → 規則集 `moomoo_my`，`market = MY`）

$$\text{brokerage} = \max\big(\text{brokerage\_rate}\times\text{notional},\ \text{min\_fee}\big)$$

$$\text{clearing} = \min\big(\text{clearing\_rate}\times\text{notional},\ \text{clearing\_cap}\big)$$

$$\boxed{\text{fee} = \text{brokerage} + \text{clearing} + \text{sst}}\qquad \boxed{\text{tax} = \text{stamp\_duty\_rate}\times\text{notional}\ (\le \text{stamp\_duty\_cap})}$$

種子值：`brokerage_rate = 0.0008`、`min_fee = 3`、`clearing_rate = 0.0003`、`clearing_cap = 1,000`
（RM）、`stamp_duty_rate = 0.001`、`stamp_duty_cap = None`（**目前未設上限**，見 §3.5）、`sst = 0`。
`fee`、`tax` 皆量化至 **2 dp**。

> **重要記帳約定**：本 app 將**印花稅（stamp duty）記於 `tax` 欄**，**brokerage + clearing + SST 記於
> `fee` 欄**。這是 MY 特有的欄位對應，裁定 MY 交易成本時務必分辨。

**已驗證範例**

| 情境 | brokerage | clearing | fee | tax（印花） | 驗證錨點 |
| --- | ---: | ---: | ---: | ---: | --- |
| 1155 買 1,000@9.50 | max(7.60,3)=7.60 | 0.0003×9,500=2.85 | **10.45** | 0.001×9,500=**9.50** | `fee_engine.fee/tax moomoo_my_my/1155 buy 1000@9.50 id=15` |
| 1155 賣 400@11.00 | max(3.52,3)=3.52 | 0.0003×4,400=1.32 | **4.84** | 0.001×4,400=**4.40** | `fee_engine.fee/tax moomoo_my_my/1155 sell 400@11.00 id=26` |

### 3.5 覆寫（overrides）與待確認費率

- **手動覆寫**：使用者於輸入／編輯時可顯式改寫 `fee` / `tax`；此時系統以覆寫值為準，並在 `snapshot`
  標記 `override: true`（見 §10 之 `_recompute_edit_fees`）。
- **待真實對帳單確認**（`config_seed.py` 註記）：US `sec_fee`、Moomoo 平台費買／賣別、MY `stamp_duty_cap`。
  這些是 schema 已支援、種子值暫定之項；費率變更屬 config 變更，須記於 `CHANGELOG.md`。

> **實作位置**：`data_ingestion/fees.py`、`data_ingestion/config_seed.py::FEE_RULES`。
> **依據**：`.claude/rules/markets-and-fees.md`。
> **驗證錨點**：上表各 `fee_engine.*`（壓測 phase1，`fee_engine.fee`／`fee_engine.tax` 各 36 項全通過）。

---

## 4. 成本基礎（加權平均）

**方法**：**加權平均成本法**（weighted-average），全市場適用。以標的**報價幣**追蹤（TW→TWD、US→USD 含
Moomoo、MY→MYR）。每個部位（`account_id` × `symbol`）維護兩個總額：

| 欄位 | 定義 | 是否可被覆寫 |
| --- | --- | --- |
| `original_total`（原始成本總額） | **all-in**：買入 `quantity×price + fees + tax` 累加 | **永不覆寫**（I2） |
| `adjusted_total`（調整後成本總額） | `original_total − 累計現金股利淨額`（見 §6） | 隨股利／賣出變動；**可 ≤ 0**，永不設地板（floor） |

**均價一律 on read 相除**（I7，避免多批次累計捨入誤差）：

$$\text{original\_avg} = \frac{\text{original\_total}}{\text{shares}}\qquad \text{adjusted\_avg} = \frac{\text{adjusted\_total}}{\text{shares}}$$

### 4.1 逐事件重播（chronological replay）

`cost_basis.py::build_book` 將四本帳按 **(日期, 同日優先序)** 排序後逐筆重播。**同日優先序**：

$$\text{opening}(0) \prec \text{buy}(1) \prec \text{sell}(2) \prec \text{dividend}(3)$$

- **買入**：`cost = quantity×price + fees + tax`；`shares += quantity`；`original_total += cost`；
  `adjusted_total += cost`。
- **賣出（比例移除）**：令 `frac = quantity / shares`（賣出前的 shares），則

$$\text{original\_removed} = \text{original\_total}\times\text{frac},\quad \text{adjusted\_removed} = \text{adjusted\_total}\times\text{frac}$$

  移除後 `shares -= quantity`、`original_total -= original_removed`、`adjusted_total -= adjusted_removed`。
- **全賣後再買（restart）**：當 `shares` 歸零，部位總額同步歸零；之後再買即以新批次重新累積（新的加權平均
  自然從零起算）。

### 4.2 已驗證工作範例 — `tw_broker/0050`

此例展示：all-in 成本、**依交易日排序**（賣出早於某買入）、比例移除、現金股利降 `adjusted_total`。帳本：

| 日期 | 事件 | 明細 |
| --- | --- | --- |
| 2026-01-12 | 買 | 10 @ 130，fee 20 → cost 1,320 |
| 2026-02-01 | 買 | 100 @ 132，fee 20 → cost 13,220 |
| 2026-04-10 | **賣** | 50 @ 140，fee 20、tax 7 |
| 2026-05-10 | 買 | 50 @ 138，fee 20 → cost 6,920 |
| 2026-06-12 | 股利 | CASH，net 800 |

逐步（**注意 2026-04-10 賣出排在 2026-05-10 買入之前**）：

1. 買 10：shares 10、total 1,320
2. 買 100：shares 110、total 14,540
3. 賣 50：`frac = 50/110`；`removed = 14,540 × 50/110 = 6,609.0909…`；剩 shares 60、total 7,930.9090…
4. 買 50：shares 110、`original_total = 7,930.9090… + 6,920 = 14,850.9090…`
5. 股利 net 800：`adjusted_total = 14,850.9090… − 800 = 14,050.9090…`

最終持倉（與 `build_book` 輸出逐位一致）：

| 量 | 值 |
| --- | ---: |
| `shares` | 110 |
| `original_total` | 14,850.909090909… |
| `adjusted_total` | 14,050.909090909… |
| `original_avg` | 135.008264462… |
| `adjusted_avg` | 127.735537190… |
| `dividend_portion`（= original − adjusted） | 800.000… |
| `payback_ratio`（見 §6.4） | 0.053868756… |

> **驗證錨點**：`holding.original_total / holding.adjusted_total / holding.original_avg /
> holding.adjusted_avg / holding.dividend_portion / holding.shares`，`scope = tw_broker|0050`（phase1
> 最終快照）。

> **實作位置**：`portfolio/cost_basis.py::build_book`、`_Position`；持倉結果 `portfolio/results.py::Holding`。
> **依據**：`.claude/rules/domain-ledger.md`（Cost basis）。

---

## 5. 已實現／未實現損益

### 5.1 已實現損益（Realized P&L）

於每筆**賣出**產生一列 `RealizedRow`（`cost_basis.py`）：

$$\text{proceeds\_net} = \text{quantity}\times\text{price} - \text{fees} - \text{tax}$$

$$\boxed{\text{realized} = \text{proceeds\_net} - \text{adjusted\_removed}}$$

即：**淨賣出價款（扣費扣稅後）− 賣出比例對應之 `adjusted_avg × shares_sold`**。已實現以**調整後成本**衡量
（股利已折入成本，故不另立股利收入行 → invariant I4，避免重複計算）。跨幣別以
`RealizedPnL.by_currency` 分幣彙總。

**已驗證範例**

| 賣出 | proceeds_net | adjusted_removed | realized | 驗證錨點 |
| --- | ---: | ---: | ---: | --- |
| `tw_broker/0050` 2026-04-10（50@140） | 6,973 | 6,609.0909… | **363.9090…** | `realized.realized tw_broker/0050@2026-04-10` |
| `schwab/TSLA` 2026-04-20（20@260） | 5,199.86 | 5,000.00 | **199.86** | `realized.realized schwab/TSLA@2026-04-20` |

（分幣彙總已驗證：`realized_by_ccy = {TWD: 46,492.2877…, USD: 3,576.8159…, MYR: 835.9766…}`。）

### 5.2 未實現損益（Unrealized P&L）與資本利得

`portfolio/pnl.py::value_holdings` 以現價 `price` 填市值欄：

$$\text{market\_value} = \text{price}\times\text{shares}$$

$$\boxed{\text{unrealized\_pnl} = (\text{price} - \text{adjusted\_avg})\times\text{shares}}$$

$$\text{capital\_gain} = (\text{price} - \text{original\_avg})\times\text{shares}\quad(\text{相對原始成本；供「資本利得 vs 股利」拆分})$$

**已驗證範例 — `schwab/TSLA`**：`shares = 10`、`adjusted_avg = 240.00`、現價 250 →
`unrealized_pnl = (250 − 240)×10 = 100.00`；`market_value = 2,500`。
驗證錨點：`holding.unrealized_pnl / holding.market_value schwab|TSLA`。

### 5.3 缺價與賣超之退化語意

- **缺現價**：`price is None` → `market_value / unrealized_pnl / capital_gain` 全設 `None`、
  `price_stale = True`；**永不臆造價格**。所有以 `market_value is not None` 為閘的彙總會自動排除它。
- **賣超（oversell，賣出量 > 持有量）**：屬**輸入錯誤 vs 放空**之辨識，語意為**「阻擋待確認」**
  （blocked-pending-ack）：
  - 驗證路徑（`allow_oversell=False`）：`build_book` 拋 `OversellError`，API 回 **422
    `oversell_unacknowledged`**（`需確認賣超`）。
  - 使用者 `ack_oversell=True` 後：儀表板路徑（`allow_oversell=True`）**優雅退化**——部位淨為負股、
    丟棄其（已無定義的）成本基礎、**不產生已實現列**，該持倉標記 `oversold`（**待釐清**）。此非放空會計。
  - 修復方式：補登遺漏的期初庫存／買入。

> **驗證錨點**：`guard.oversell_blocks`（oplog `op42` 回 422 → `op43` 帶 ack 成功 → `op44` 刪除）。
> **實作位置**：`portfolio/cost_basis.py`（`OversellError`、`RealizedRow`）、`portfolio/pnl.py::value_holdings`、
> `api/routers/input_center.py::manual_commit`。
> **依據**：`.claude/rules/domain-ledger.md`（P&L and returns；Data integrity）。

---

## 6. 股息三模型

實作：`data_ingestion/dividend_model.py::apply_dividend_model`（衍生 withholding／net／reinvest_shares）
+ `cost_basis.py::build_book` 之股利分支。同日優先序中股利排最後（見 §4.1）。
`CASH_DIVIDEND_TYPES = {CASH, NET}`（TW 現金 + MY 單層淨額共用同一「降成本」定義）。

### 6.1 TW 現金（`CASH`，`tw_broker`）— 降成本

記**淨收金額**；折入調整後成本，**不另立收入行**：

$$\text{adjusted\_total} \mathrel{-}= \text{net}\qquad(\text{net 於 TW 現金 = gross}，\text{無預扣})$$

**已驗證範例**：`tw_broker/0050` 股利 net 800（2026-06-12，在最後買入之後、且此後無賣出）→ 全數作用於
最終 110 股 → `dividend_portion = 800.00`、`adjusted_total = 14,050.909…`（見 §4.2）。

### 6.2 US DRIP（`DRIP`，`schwab` / `moomoo_my_us`）— 30% 預扣、$0 成本再投資

$$\text{withholding} = \text{gross}\times 0.30\qquad \text{net} = \text{gross} - \text{withholding}$$

$$\text{reinvest\_shares} = \frac{\text{net}}{\text{reinvest\_price}}\quad(\text{reinvest\_price = 登錄之再投資價})$$

再投資股數以 **$0 成本**加入部位：`shares += reinvest_shares`；**`adjusted_total` 不變**（DRIP **不**降調整
後成本）→ 均價因加入零成本股而自然下降。DRIP 於現金流上**中性**（見 §7、§9）。

**已驗證範例 — `schwab/MSFT` 股利 id=1**：`gross 100 → withholding 30.00 → net 70.00`，
`reinvest_price 350 → reinvest_shares = 70/350 = 0.20` 股，`$0` 成本加入。故 MSFT `dividend_portion = 0.00`
（調整後成本未被股利改變），`shares` 增加 0.20。
驗證錨點：`ledger.div.gross/net`（`schwab|MSFT`）、`holding.dividend_portion schwab|MSFT = 0.00`、
`holding.shares schwab|MSFT`。

`US_WITHHOLDING = 0.30` 適用 Schwab 與 Moomoo 兩美股帳戶（W-8BEN）。

### 6.3 MY 現金（`NET`，`moomoo_my_my`）— 單層淨額降成本

馬來西亞單層制（single-tier）：記**淨收金額**，與 TW 現金同走降成本：`adjusted_total −= net`。
驗證錨點：`ledger.div.net moomoo_my_my|1155`；`holding.dividend_portion moomoo_my_my/1155 = 306.25`
（注意：因該部位在股利後仍有賣出，`dividend_portion` 會隨賣出**比例移除**，故不等於累計股利總額——
交叉參見 §4.1 比例移除、§5.1）。

### 6.4 配股（`STOCK`）與顯示用回本進度

- **配股（stock dividend，配股）**：`shares +=`（無現金、無成本變動）；`withholding = net = 0`。
- **股利只計入總報酬一次**（invariant I4）：TW/MY 現金經降成本、US DRIP 經 $0 成本股——皆各只一次；
  **無獨立股利行**（舊有重複計算陷阱）。
- **顯示用（display-only）回本進度／股利回收率**：

$$\text{payback\_ratio} = \frac{\text{cumulative cash dividends}}{\text{original\_total}} = \frac{\text{dividend\_portion}}{\text{original\_total}}$$

  （`cost_basis.py`：`dividend_portion = original_total − adjusted_total`。此為顯示指標，不進報酬分子。）

> **實作位置**：`data_ingestion/dividend_model.py`、`portfolio/cost_basis.py`（股利分支、`CASH_DIVIDEND_TYPES`、
> DRIP 需 `reinvest_shares` 否則 fail-loud）。
> **依據**：`.claude/rules/domain-ledger.md`（Dividend models；P&L and returns）、`.claude/rules/markets-and-fees.md`（30% 預扣）。

---

## 7. 總報酬與報酬率（含 XIRR）

### 7.1 總報酬與累計報酬率

實作：`portfolio/returns.py::total_return`。

$$\text{total\_return}_{ccy} = \text{realized}_{ccy} + \text{unrealized}_{ccy}\quad(\text{兩者皆相對「調整後成本」，含已平倉部位之已實現})$$

$$\text{reporting\_total\_return} = \sum_{ccy}\operatorname{convert}\big(\text{total\_return}_{ccy},\ \text{spot}(ccy\to\text{reporting})\big)$$

$$\text{rate}_{ccy} = \frac{\text{total\_return}_{ccy}}{\text{gross\_invested}_{ccy}}\quad(\text{分母 = 累計原始投入成本，非年化})$$

> **退化註記**：某 `ccy` 的 `gross_invested = 0` 時 `rate = None`；若某持倉現價缺失（stale），其 unrealized
> 被排除於分子，但成本仍留在分母 → 簡易 rate **會低估**報酬。故 rate 為次要瞥視指標，**XIRR 才是嚴謹指標**。

**已驗證彙總（reporting = TWD，spot USD/TWD = 32.5、MYR/TWD = 7.2）**

| KPI | 值（TWD） | 驗證錨點 |
| --- | ---: | --- |
| `realized_total` | 168,757.84 | `kpi.realized_total TWD` |
| `unrealized_total` | 345,995.01 | `kpi.unrealized_total TWD` |
| `total_return`（= 已實現 + 未實現） | **514,752.85** | `kpi.total_return TWD` |
| `total_market_value` | 3,887,889.28 | `kpi.total_market_value TWD` |

（交叉核對：168,757.84 + 345,995.01 = 514,752.85 ✓。）

### 7.2 XIRR（年化、資金加權、FX-aware — 決策主指標）

實作：`portfolio/returns.py::xirr_reporting`（求解器 `pyxirr.xirr`）。**單一報表幣別**；**每筆流量以其
交易日 FX 換算**，終值以**當前 spot** 換算。現金流符號：

| 流量 | 符號 | 金額（報表幣，換算後） |
| --- | :---: | --- |
| 買入 buy | **−** | `−(quantity×price + fees + tax)`，日期 = `trade_date` |
| 賣出 sell | **+** | `+(quantity×price − fees − tax)`，日期 = `trade_date` |
| 現金股利（TW `CASH` / MY `NET`） | **+** | `+net`，日期 = 股利日 |
| **DRIP / STOCK** | **中性** | 不計入（非外部現金流；再投資非 − 流出、股利非 + 流入） |
| 期初庫存 opening | **−** | `−original_cost_total`，日期 = **`build_date`**（使期初資本被計入） |
| 期末市值 | **+** | `Σ price×shares`（各持倉），日期 = `as_of` |

**退化（all-or-nothing）**：任一持有 symbol 缺現價 → 無法形成終值 → 回 `None`（不部分退化）；無號變
（例如全為流出，無 sign change）或非有限結果亦回 `None`。

**流量建構範例（`schwab/TSLA`，USD 單幣，各 total 均有錨點）**

| 日期 | 事件 | 流量（USD） | 錨點 |
| --- | --- | ---: | --- |
| 2026-04-01 | 買 20@250 | −5,000.00 | `ledger.tx.total …TSLA id=22` |
| 2026-04-20 | 賣 20@260 | +5,199.86 | `ledger.tx.total …TSLA id=23` |
| 2026-05-01 | 買 10@240 | −2,400.00 | `ledger.tx.total …TSLA id=24` |
| `as_of` | 期末 10 股 @250 | +2,500.00 | `holding.market_value schwab|TSLA` |

XIRR 即對上述 `(dates, amounts)` 序列求使 NPV=0 之年化率 r。

> **仲裁註記（誠實揭露）**：966 項壓測**未涵蓋 XIRR 純量**（無 `xirr.*` 斷言）。XIRR 之**現金流建構規則**
> 以 `returns.py::xirr_reporting` 為裁定準據（上表逐項可由已驗證的 `ledger.tx.total` 與 `holding.market_value`
> 重建）；其**數值收斂結果**由 `pyxirr` 決定。若日後需為 XIRR 純量提供錨點，應新增對應斷言。

> **實作位置**：`portfolio/returns.py`（`total_return`、`xirr_reporting`）、`portfolio/results.py`
> （`ReturnSummary`、`CurrencyReturn`）。
> **依據**：`.claude/rules/domain-ledger.md`（Total return；XIRR cashflow signs）、`.claude/rules/data-and-pricing.md`（Returns & FX P&L）。

---

## 8. 換匯損益（FX P&L）

**專用帳本** `fx_conversions` 記錄**每一筆實際換匯**：`date, account_id, from_ccy, from_amount, to_ccy,
to_amount` → 隱含匯率 `implied_rate = from_amount / to_amount`（**本位幣 per 1 單位外幣**；例 `id=1` TWD
320,000→USD 10,000 → 320,000/10,000 = **32**，錨點 `ledger.fx.implied id=1`）。每個外幣 pool（per account）帶一個
**本位幣（home = 帳戶 `funding_ccy`）成本基礎 = 加權平均取得匯率**。Schwab USD pool 錨定 **TWD**；Moomoo
USD pool 錨定 **MYR**。

### 8.1 加權平均取得匯率（home per foreign）

實作：`forex/pools.py::average_acquisition_rate`。僅計 `home → foreign` 方向之換匯：

$$\text{avg\_rate} = \frac{\sum \text{from\_amount}\ (\text{home})}{\sum \text{to\_amount}\ (\text{foreign})}\quad(\text{無此類換匯則 None})$$

**已驗證範例**

| 帳戶 | home→foreign 換匯 | avg_rate | 錨點 |
| --- | --- | ---: | --- |
| `schwab` | TWD 320,000→USD 10,000（32.0）；TWD 2,310,000→USD 70,000（33.0） | (320,000+2,310,000)/(10,000+70,000) = **32.875** | `fx.avg_rate schwab` |
| `moomoo_my_us` | MYR 44,000→USD 10,000（4.4）；MYR 46,000→USD 10,000（4.6） | 90,000/20,000 = **4.5** | `fx.avg_rate moomoo_my_us` |

### 8.2 已實現換匯損益（回換 foreign→home 時）

實作：`forex/fx_pnl.py::realized_fx_rows`。對每筆 `foreign → home` 回換：

$$\text{realized\_fx} = \text{home\_received} - \text{foreign\_sold}\times\text{avg\_rate}$$

（刻意**不**走 `shared.fx.convert`，因 `avg_rate` 是**衍生 pool 匯率**，非市場 spot。）`avg_rate = None`（無成本
基礎）→ 回 `None`；有基礎但無回換 → 0。
**已驗證範例**：壓測期間無 USD→TWD／USD→MYR 回換（`op47` 之 USD→TWD 遭 `negative_cash` 阻擋）→
`realized_fx = 0`。錨點：`fx.realized schwab = 0`、`fx.realized moomoo_my_us = 0`、`fx.reporting_realized rollup = 0`。

### 8.3 未實現換匯損益（剩餘外幣曝險 mark-to-spot）

實作：`forex/fx_pnl.py::compute_account_fx`。令 `spot = 當前 foreign→home` 匯率：

$$\text{unreal\_stocks} = \text{foreign\_stock\_value}\times(\text{spot} - \text{avg\_rate})$$

$$\text{unreal\_cash} = \text{foreign\_cash}\times(\text{spot} - \text{avg\_rate})$$

其中 `foreign_cash` 為 **FX 曝險視角**之外幣餘額（由換匯 + 外幣買賣 + 外幣現金股利重建；**與 §9 營運現金池
不同**，見 `forex/pools.py` 檔頭 C9 說明）。`avg_rate is None` 或 `spot is None` → unrealized = `None`。

**已驗證範例 — `schwab`（reporting = home = TWD）**

- 外幣曝險：USD 股票市值 61,723.21 + FX 視角 USD 現金 23,159.29 = **84,882.50 USD**
- `avg_rate = 32.875`、`spot(USD→TWD) = 32.5` → `spot − avg = −0.375`
- 未實現 FX = 84,882.50 × (−0.375) = **−31,830.94 TWD**（買入 USD 均價 32.875，今 32.5 → 台幣升值／USD 貶
  → 換匯損失）

`moomoo_my_us`：`avg_rate = 4.5`、`spot(USD→MYR) = 4.5` → 差 0 → 未實現 FX = 0。
故 reporting（TWD）rollup 未實現 FX = **−31,830.938…**。錨點：`fx.reporting_unrealized rollup`。

### 8.4 CRITICAL — 換匯損益是「拆解」，永不加疊（invariant I5）

報表幣別總報酬 / XIRR **已內含** FX（流量按交易日匯率換算、終值按當前匯率）。**換匯損益是該數字的
attribution 拆解（資產損益 vs 換匯損益），絕不是另外加在總報酬之上的一筆額外收益**。任何把
`reporting_unrealized_fx`（如上例 −31,830.94）再加到 `total_return`（§7）之上的做法，都是**重複計算**，屬 bug。

> **實作位置**：`forex/pools.py`（`average_acquisition_rate`、`foreign_cash_balance`）、`forex/fx_pnl.py`
> （`compute_account_fx`、`compute_fx_summary`）、`forex/results.py`。
> **依據**：`.claude/rules/domain-ledger.md`（FX / currency-exchange ledger；CRITICAL — no double count）。

---

## 9. 現金池與對帳單

實作：`portfolio/cash.py`（純計算）+ `api/routers/cash.py`（門與護欄）。**每個 (account, currency) 一個
營運現金池**。此為**營運現金追蹤**，**不餵任何報酬指標**（XIRR 仍純以交易流量計，見 `cash.py` 檔頭）。

### 9.1 每種流量的借貸（`cash_balances` / `pool_lines`）

| 流量 | 對 (account, ccy) 池之 delta |
| --- | --- |
| 存入 deposit / 期初資金 opening（cash movement） | **+ amount**（credit） |
| 提出 withdraw | **− amount**（debit） |
| 換匯 fx（兩腿） | `from_ccy`：**− from_amount**；`to_ccy`：**+ to_amount** |
| 買入 buy | **− (quantity×price + fees + tax)**（all-in debit，記於 `quote_ccy` 池） |
| 賣出 sell | **+ (quantity×price − fees − tax)**（淨額 credit） |
| 現金股利（`CASH` / `NET`） | **+ net**（credit） |
| **DRIP / STOCK** | **0**（股票事件，不動現金） |

> **期初庫存 `opening_inventory` 刻意不動現金池**（其資金早於追蹤起點）。若要現金池從第一天起平衡，
> 需另記一筆 `deposit` 或 `opening`（**期初資金**）現金移動。注意：`opening_inventory`（庫存）與 cash
> movement 的 `opening`（期初資金）是**兩個不同概念**。

`symbol` 未註冊之列會被跳過（與儀表板同一退化規則），不使現金視圖崩潰。

### 9.2 對帳單（running-balance statement）與同日排序

實作：`pool_lines` → `_ordered` → `running_statement` / `running_min`。**同日排序：credit 先於 debit**
（`key = (date, 0 if delta≥0 else 1)`），使同日入金能覆蓋同日支出，不虛假地瞬間為負。
`running_statement` 回傳每列 + 其後的**逐列 running balance**；`running_min` 回傳**期間內最小 running
balance**（空池為 0）。

**已驗證期末餘額（reporting = TWD）**

| 池 | 期末餘額 | 錨點 |
| --- | ---: | --- |
| `tw_broker` / TWD | 1,085,715 | `cash.balance` / `cash.statement.terminal tw_broker|TWD` |
| `schwab` / USD | 23,159.29 | `cash.balance schwab|USD` |
| `schwab` / TWD | 370,000 | `cash.balance schwab|TWD` |
| `moomoo_my_us` / USD | 894.63 | `cash.balance moomoo_my_us|USD` |
| `moomoo_my_us` / MYR | 30,000 | `cash.balance moomoo_my_us|MYR` |
| `moomoo_my_my` / MYR | 94,360.58 | `cash.balance moomoo_my_my|MYR` |

（`cash.balance` 與 `cash.statement.terminal` 兩組錨點期末一致，證明彙總視圖與逐列對帳單收斂於同一值。）

### 9.3 負池語意與護欄（date-aware guard）

**負池通常代表漏記入金或換匯**。護欄分兩層：

- **現金門（deposit/withdraw、fx.convert）之硬護欄**：以 **`running_min`（date-aware，含未來回填）** 檢查
  「會使該池在**某時點**降至負」；若 `running_min < 0` 且未 `ack_negative` → **422 `negative_cash`**
  （`此筆會使 … 現金於某時點降至 … — 通常代表漏記入金或換匯;確認無誤可強制寫入`）。編輯／刪除須使
  **所有受影響池**（舊 + 新 account/ccy）皆不為負。
- **交易門之軟警告（soft）**：`api/routers/input_center.py::_cash_overdraft_issue` — **僅當**帳戶已啟用現金
  追蹤（≥1 筆 cash movement）**且**該筆買入會使該標的現金池 < 0 時，附一則**警告 issue（永不硬阻擋）**。
  未追蹤現金的帳戶不會觸發。

**已驗證範例**：`op47`（schwab USD 5,000 → TWD 162,000）遭拒，訊息含 `降至 -24000.00`（date-aware 檢出
某時點會透支）。錨點：oplog `op47` status 422 `negative_cash`。

> **實作位置**：`portfolio/cash.py`（`cash_balances`、`pool_lines`、`running_min`、`running_statement`）、
> `api/routers/cash.py`（`_pool_min`、`_negative_response`、`add_movement`／`add_fx` 護欄）、
> `api/routers/input_center.py::_cash_overdraft_issue`。
> **依據**：`.claude/rules/data-and-pricing.md`（cash pools；audit C3/C5/C9）。

---

## 10. 更正、稽核與重算

**「精神上僅追加」（append-only in spirit）**：更正是**顯式**的 PUT/DELETE 使用者動作，**永不靜默變更**。
每筆寫入前，先將**「更正後的整本帳」重播過 `build_book`**（replay），**只阻擋此更正所「新引入」的問題**。

### 10.1 重播護欄（replay guard，`ledgers.py::_replay_block`）

比較**現狀帳本 vs 更正後帳本**，二分：

| 阻擋碼 | 觸發 | 性質 | 回應 |
| --- | --- | --- | --- |
| `orphan`（孤兒） | 更正使某股利／期初紀錄**失去對應持倉**（該股利日之前無買入／期初） | **硬**（不可 ack 繞過） | 422 `orphan_correction` |
| `oversell`（賣超） | 更正**新造成或惡化**某部位賣超（更負） | **軟**（`ack_oversell` 可繞過） | 422 `oversell` |

**關鍵 scoping**：`introduced_orphans = orphans(post) − orphans(pre)`；賣超則逐 key 比較
`post_over[key] < pre_over[key]` 或新出現。**既有、無關的** orphan／oversell **不會**污染一筆無關更正
（audit H3/H8）。若更正後帳本**根本無法重建**（例如 DRIP 被剝除 `reinvest_shares`），且此問題係本更正引入
→ 硬阻擋。

### 10.2 費用／稅自動重算（`_recompute_edit_fees`，audit M6）

交易編輯時，若**核心欄位**（account／symbol／side／quantity／price／date／**daytrade**）改變**且**使用者未
顯式改寫 fee/tax（`fee_overridden` / `tax_overridden` 皆 False）→ **以新帳戶規則集重算 fee/tax 並重生
snapshot**；顯式改寫則保留為 override（snapshot 標 `override: true`）。

- **`daytrade` 保存**：wire 上 `daytrade = None` 表**保留 DB 既存旗標**（MED-1）；改變 daytrade 屬核心變更
  （左右 TW 賣方稅率），會餵入 `compute_fees` 使重算重現當沖率，而非默默退回現股。
- **溢位保護**：過大 notional 於 quantize 接縫拋 `FeeComputationError` → 400（audit M4），不 500。

### 10.3 稽核軌跡（audit trail，`store.py`，audit M9）

任何 update／delete **在變更前**將**變更前值（before-values）**寫入 `ledger_audit`
（`table_name, row_id, action, before_json, at`）。以 `list_ledger_audit` 查詢（新到舊）。
`original_cost` 不可侵犯（I2）——更正產生新的權威狀態，但歷史前值恆可稽核回溯。

### 10.4 模式（modes）

- **試算（試算）**：計算、**不寫入**。
- **報告／更新／績效**：完整報表 + 即時抓價。
- **重算（重算）**：由四本帳本**完全重建**所有統計（見 §1.4）。

### 10.5 已驗證更正範例

| op | 動作 | 結果 |
| --- | --- | --- |
| `op44` | 刪交易 id=28（先前 acked 賣超之 0050 賣 200） | `ok`（賣超列消失，帳本恢復） |
| `op45` | 編輯 id=3（2330 買 500，price 640→645，顯式 fee=460、tax=0） | `ok`，回傳 `fee=460, tax=0`（override 生效） |
| `op46` | 刪交易 id=16（1155 買 500@10.20） | `ok`（1155 成本基礎相應重算） |

> **實作位置**：`api/routers/ledgers.py`（`_replay_block`、`_orphan_keys`、`_oversold_shares`、
> `_recompute_edit_fees`、`edit_transaction`／`remove_*`）、`data_ingestion/store.py`（`_write_audit`、
> `update_transaction`／`delete_*`、`_cap_price`、`daytrade` 持久化）。
> **依據**：`.claude/rules/domain-ledger.md`（Data integrity）、`.claude/rules/engineering-process.md`（append-only 精神）。

---

## 11. 再平衡試算

實作：`strategy/rebalance.py::compute_rebalance`。**純試算（compute-only），永不寫任何帳本**——僅投影「要達到
這些權重需下哪些單」。使用與儀表板**相同**的 spot 匯率（`RateResolver`）與估值（`build_dashboard`）。

### 11.1 Owner ruling（2026-07-13）— Option 1 合併跨帳戶引擎

> **裁定日期註記**：程式碼 docstring 記為 **2026-07-13**；發版紀錄（MEMORY / v0.1.18）記為 **2026-07-14
> 定案並出貨**。兩者指同一裁定（Option 1）。仲裁時以「symbol-level 目標套用於合併部位」之語意為準。

一個 symbol 的**目標權重套用於其跨「所有帳戶」的合併部位**（Option 1；Option 2 之 per-account 目標已否決）。
對每個目標 symbol：

1. **聚合**該 symbol 在每個有現價帳戶的 `shares` + 報表幣市值；`delta = target_weight × portfolio_total −
   combined_MV`。
2. **路由**執行單至具體帳戶（費／稅綁帳戶 — invariant I6）：
   - **買入 BUY**：單一 leg，路由至**持股最多**之帳戶（tie-break：`account_id` 升序）。
   - **賣出 SELL**：**貪婪（greedy），持股最多者優先**，每 leg 以該帳戶持股為上界，直到 delta 補足 → 故
     **目標 0 會清光每個帳戶**，且**超額賣出永不超過實際持股**。
3. **整股捨入**（per leg，依該 leg 的市場）：TW → 股（整數，非整千即零股旗標）、**MY → 100 單位 board lot**、
   US → 1 股。捨入實作 `_round_shares`（MY 以 `round(raw/100)×100`）。
4. 每 leg 之 fee/tax 以**該帳戶規則集**經真實費引擎 `compute_fees` 計（見 §3）。

### 11.2 權重與彙總公式

$$\text{current\_weight} = \frac{\operatorname{convert}(\text{combined\_MV}_{quote},\ \text{rate})}{\text{portfolio\_total}}$$

$$\text{delta\_reporting} = \text{target\_ratio}\times\text{portfolio\_total} - \text{current\_MV}_{reporting},\quad \text{side} = \begin{cases}\text{BUY} & \delta>0\\\text{SELL} & \delta<0\end{cases}$$

$$\text{raw\_shares} = \frac{|\delta_{reporting}| / \text{rate}}{\text{price}}$$

$$\text{new\_weight} = \frac{\operatorname{convert}(\text{new\_combined\_shares}\times\text{price},\ \text{rate})}{\text{portfolio\_total}}\quad(\text{分母為「原」總市值，非重算後})$$

### 11.3 誠實退化

- 目標 symbol **無現價**（未知、未持且未定價、或列於 `freshness.missing_prices`、或現價 ≤ 0）→ **排除**，
  列入 `excluded`；**永不臆造價格**、亦不除以零。
- v1 **只作用於 `targets` 內的 symbol**；未列之持倉不動、不出現於輸出。
- `summary.over_allocated`：Σ(送出目標) > 1 時**僅旗標**（不硬阻擋）。`summary.excluded_with_target`：帶已存
  目標權重卻不會成列（未持／未定價）之 symbol，浮出以免 UI 靜默丟棄。
- Money 全程 `Decimal`；router 再序列化為 wire 字串。

> **實作位置**：`strategy/rebalance.py`（`compute_rebalance`、`_priced_constituents`、`_round_shares`、`_Leg`）、
> `strategy/target_weights.py`（存取目標權重）。
> **依據**：`.claude/rules/domain-ledger.md`（invariant #5 費綁帳戶）、`CLAUDE.md`（rebalance ruling）。
> **驗證錨點**：壓測 phase1 未涵蓋再平衡試算純量（該引擎為 compute-only，不寫帳本）；本節公式以程式碼為準，
> 其 leg 費用經 §3 之 `fee_engine.*` 錨點間接驗證。

---

## 12. 附錄

### 12.1 工作範例索引（每例附驗證錨點）

| # | 範例 | 章節 | 驗證錨點（`scope`） |
| --- | --- | --- | --- |
| E1 | TW 費／稅（2330 買 1,000@600 → fee 855） | §3.1 | `fee_engine.fee tw_broker/2330 buy 1000@600 id=1` |
| E2 | TW 現股賣稅（2330 賣 300@700 → tax 630） | §3.1 | `fee_engine.tax tw_broker/2330 sell 300@700 id=18` |
| E3 | TW ETF 賣稅（0050 賣 50@140 → tax 7） | §3.1 | `fee_engine.tax tw_broker/0050 sell 50@140 id=21` |
| E4 | US Schwab 賣（TSLA 20@260 → fee 0.14） | §3.2 | `fee_engine.fee schwab/TSLA sell 20@260 id=23` |
| E5 | US Moomoo 賣（NVDA 25@600 → fee 1.41） | §3.3 | `fee_engine.fee moomoo_my_us/NVDA sell 25@600 id=27` |
| E6 | MY 費 + 印花（1155 買 1,000@9.50 → fee 10.45／tax 9.50） | §3.4 | `fee_engine.fee/tax moomoo_my_my/1155 buy 1000@9.50 id=15` |
| E7 | 加權平均成本（0050 完整重播 → orig 14,850.91／adj 14,050.91） | §4.2 | `holding.* tw_broker|0050` |
| E8 | 已實現（0050 賣 → 363.9091） | §5.1 | `realized.realized tw_broker/0050@2026-04-10` |
| E9 | 未實現（TSLA → 100.00） | §5.2 | `holding.unrealized_pnl schwab|TSLA` |
| E10 | DRIP（MSFT gross 100 → 0.20 股 $0 成本，div_portion 0） | §6.2 | `holding.dividend_portion schwab|MSFT = 0.00` |
| E11 | TW 現金股利降成本（0050 net 800 → div_portion 800） | §6.1 | `holding.dividend_portion tw_broker|0050 = 800` |
| E12 | 總報酬（TWD 514,752.85） | §7.1 | `kpi.total_return TWD` |
| E13 | FX 加權均率（schwab 32.875／moomoo 4.5） | §8.1 | `fx.avg_rate schwab / moomoo_my_us` |
| E14 | 未實現換匯（rollup −31,830.94 TWD） | §8.3 | `fx.reporting_unrealized rollup` |
| E15 | 現金池期末（tw_broker TWD 1,085,715） | §9.2 | `cash.balance tw_broker|TWD` |
| E16 | 負池護欄（op47 → 422，降至 −24,000） | §9.3 | oplog `op47` `negative_cash` |
| E17 | 賣超阻擋 + ack + 刪除 | §5.3／§10.5 | `guard.oversell_blocks`；oplog `op42–44` |

### 12.2 詞彙表（中文 ↔ 英文欄位）

| 中文 | 英文識別字 | 定義所在 |
| --- | --- | --- |
| 原始成本總額 | `original_total` / `original_cost_total` | §4 |
| 調整後成本總額 | `adjusted_total` / `adjusted_cost_total` | §4 |
| 原始均價 | `original_avg` | §4 |
| 調整後均價 | `adjusted_avg` | §4 |
| 淨賣出價款 | `proceeds_net` | §5.1 |
| 已實現損益 | `realized` / `RealizedRow` | §5.1 |
| 未實現損益 | `unrealized_pnl` | §5.2 |
| 資本利得 | `capital_gain` | §5.2 |
| 股利折入部分 | `dividend_portion` | §6.4 |
| 回本進度／股利回收率 | `payback_ratio` | §6.4 |
| 加權平均取得匯率 | `avg_rate` / `average_acquisition_rate` | §8.1 |
| 已實現換匯損益 | `realized_fx` | §8.2 |
| 未實現換匯損益 | `unrealized_fx_stocks` / `unrealized_fx_cash` | §8.3 |
| 費率快照 | `fee_rule_snapshot` / `snapshot` | §3 |
| 當沖旗標 | `daytrade` | §3.1／§10.2 |
| 稽核前值 | `ledger_audit.before_json` | §10.3 |
| 期初庫存 | `opening_inventory` | §2／§9.1 |
| 期初資金（現金移動） | cash movement `opening` | §9.1 |

### 12.3 版本歷史

| 版本 | 日期 | 說明 |
| --- | --- | --- |
| `v1.0-draft` | 2026-07-15 | 首版草稿。基線 `v0.1.18 + feat/p3-batch3`。經 966 項對抗性對帳（966/966 通過）核對。**待 owner 確認為仲裁標準**。 |

### 12.4 如何仲裁一個爭議金額

給定一個「站上顯示為 X，但認為應為 Y」的金額：

1. **定位金額類型** → 對應章節：費／稅 §3；持倉成本／均價 §4；已實現 §5.1；未實現／資本利得 §5.2；
   股利 §6；總報酬／報酬率 §7.1；XIRR §7.2；換匯損益 §8；現金餘額 §9；再平衡 §11。
2. **取出相關帳本列**（四本永久帳本）：
   - 費／稅、成本、已實現、未實現 → `transactions`（該 account×symbol，**依 `trade_date` 排序**）+
     `dividends` + `opening_inventory`。
   - 換匯損益 → 該帳戶之 `fx_conversions` + `fx_rates`（當前 spot）。
   - 現金 → `cash_movements` + `fx_conversions` + 該池之 `transactions` + 現金股利。
3. **依該章節公式逐步重算**（重算）。務必套用：**同日優先序** open≺buy≺sell≺dividend（§4.1）、賣出**比例
   移除**、股利模型（§6）、精度規範（§1.3，儲存全精度、僅結算／顯示量化）。
4. **比對**：重算值 = 裁定值。若與程式碼輸出不符 → 為程式碼 bug（提報）；若與本手冊公式不符 → 為手冊
   缺陷（提報並更新）。
5. **稽核佐證**：若該列曾被更正，查 `ledger_audit`（§10.3）取變更前值還原歷史。
6. **換匯爭議專屬檢查**：確認爭議者**未把換匯損益加疊於總報酬之上**（§8.4，invariant I5 — 最常見的重複
   計算來源）。

---

_本手冊為 `portfolio-dash` 之會計公式仲裁草稿。所有工件（程式碼、規則檔、CHANGELOG）維持英文；本仲裁
文件之繁中正文為經標示之刻意例外，待 owner 簽署後生效。_
