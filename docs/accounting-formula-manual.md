# 投資組合會計公式手冊（Accounting-Formula Manual）

> **版本**：`v1.8`（2026-08-13）
> **程式碼基線**：`v0.1.28 + feat/corporate-actions`（公司行動 SPLIT／EXCHANGE／SPINOFF；含
> 2026-08-13 之**現金收支七種 kind／兩軸表**與**美股現金股利 P1b**）
> **仲裁狀態**：**已由 owner 正式簽署（2026-07-15）**，自版本 **v0.1.19** 起正式生效為站上任何
> 「金額爭議」的**唯一仲裁標準**（arbitration standard）。
> **語言例外**：本文件採**繁體中文正文 + 英文技術識別字**（欄位／資料表／函式名），為一份 owner
> 面向的仲裁文件，係對 repo「工件一律英文」規則之**刻意且經標示的例外**。**本繁中文件為仲裁正本
> （arbitration authority）**；另備一份英文鏡像 `docs/accounting-formula-manual.en.md` 供 AI／agent
> 高效讀取，**每當本繁中文件變更，須於同一 change set 內同步重生該英文鏡像**。
> **工程來源**：`.claude/rules/` 下之英文規則檔（`domain-ledger.md`、`markets-and-fees.md`、
> `data-and-pricing.md` …）仍為本文件所編纂之**工程正本**；本文件與程式碼、規則檔三者若有出入，以
> 本文件標示之「已驗證」數字與其引用之程式碼為準，並回報衝突。
>
> **驗證基礎**：本文件所有帶數字之工作範例，均取自或核對於**常駐壓測實跑**——一組對抗性對帳斷言
> （adversarial reconciliation，`scripts/stress_audit/evidence/oplog.jsonl` ＋
> `scripts/stress_audit/evidence/assertions.jsonl`）。**v1.8 之當前實跑**：phase-1
> **122 ops、3,799/3,799 全數通過、0 fail**；phase 2（線上 demo）**1,192 斷言、0 fail**。每一數字範例均標注
> 其 `scope` 驗證錨點；場景依賴之終值另標其 phase（`phase1:final`、`phase1:corp_applied` 等）。手冊作者未
> 自行捏造任何數字。**注意**：壓測場景會逐版演進（v1.6 為 77 ops／1,806 斷言；公司行動場景 `CA*` 於本版
> 加入），故每次改版須就當前實跑重新對帳（見 §12.3）。

---

## 目錄

1. [總則與精度規範](#1-總則與精度規範)
2. [帳戶／市場／幣別模型](#2-帳戶市場幣別模型)
3. [費用與交易稅公式](#3-費用與交易稅公式)
4. [成本基礎（加權平均、宣告式賣空、**公司行動**）](#4-成本基礎加權平均)
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

站上任一顯示金額若生爭議，**依本手冊對應章節之公式 + 其引用之五本永久帳本（ledgers）逐筆重算**（重算
／replay），重算結果即為裁定值。任何 UI 顯示、快取、或口頭記憶皆不得凌駕帳本重算。裁定程序見
[§12.4 如何仲裁](#124-如何仲裁一個爭議金額)。

### 1.2 核心不變式（Invariants — 違反即為 bug，非選擇）

| # | 不變式 | 出處 |
| --- | --- | --- |
| I1 | **金額永不使用 `float`**：價格、數量、費率、金額全程 `Decimal`。 | `shared/money.py` |
| I2 | **原始成本 `original_total` 永不被覆寫**；所有報表由帳本重建。 | `domain-ledger.md` |
| I3 | **報價數字來自財經 API，永不來自 LLM**。 | `data-and-pricing.md` |
| I4 | **股息只計入總報酬一次**（經成本調整，非另立收入行）。 | §6 |
| I5 | **換匯損益是報表幣別總報酬的「拆解」，永不加疊於上**。⚠ 但「已內含 FX」只對 **XIRR** 與**趨勢的 `總市值 − 累計淨投入`** 成立，**對 `total_return` 不成立**——後者把匯率乘在**損益**上、乘不到**本金**上（AI-D41，2026-08-24；三數字關係見 §7.1a）。 | §7.1a、§8 |
| I6 | **費用／稅／股利規則綁定 (帳戶, 市場) 配對**，非僅「市場」；單一市場帳戶退化為舊敘述（等同綁帳戶）。帳戶列的標量欄位（`fee_rule_set`／`dividend_model`／`settlement_ccy`）為已載明之 fallback。 | §2、§3 |
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

五本**永久真實來源**：`opening_inventory`（期初庫存）、`transactions`（交易）、`dividends`（股利）、
`fx_conversions`（換匯）、**`corporate_actions`（公司行動，§4.4）**。**所有**衍生數字（持倉、成本、
已實現／未實現、報酬、換匯損益、現金餘額）皆於讀取時由這五本**按日期順序重播（replay）**算出，不以
「算好的結果」作為真實來源（除非量測顯示需快取）。裁定時一律以重算為準。

> **`corporate_actions` 於 2026-08 加入為第五本**（公司行動 spec §3）。它與其他四本同為**帳本列**：
> 公司行動**永不**編輯既有交易列，也**永不**是套用在算好結果上的調整；它是一列被重播讀入的資料，故
> 「`original_cost` 永不被覆寫」（I2）仍然成立——重播累加器上的 `original_total` 被 EXCHANGE 歸零、被
> SPINOFF 縮放，但下一次重算仍由**未改變的同一批帳本列**重建出同樣的值。**遺漏此本帳本重算，會得到
> 一個「看起來完全正常、卻按行動前股數計價」的錯誤金額**——這正是裁定時最危險的失敗形態。
> 帳本目錄之唯一宣告處：`shared/ledger_registry.py::LEDGER_TABLES`。

> **實作位置**：`shared/money.py`、`shared/fx.py`、`data_ingestion/store.py`、`pricing/store.py`、
> `portfolio/cost_basis.py`。
> **依據**：`.claude/rules/data-and-pricing.md`（Money & numeric precision model）、`CLAUDE.md`（Core invariants）。

---

## 2. 帳戶／市場／幣別模型

三個正交維度：**市場（market，在哪交易）· 帳戶（account，哪家券商持有）· 幣別（currency，標的報價
幣）**。**同一市場可橫跨多個帳戶、同一帳戶亦可橫跨多個市場且規則各異**，故費／稅／股利規則綁定
**(帳戶, 市場) 配對**（invariant I6）。

| `account_id` | 名稱 | 市場 | 交割幣 `settlement_ccy` | 資金幣 `funding_ccy` | 股利模型 `dividend_model` | 費規則集 `fee_rule_set` |
| --- | --- | --- | --- | --- | --- | --- |
| `tw_broker` | TW Broker | TW | TWD | TWD | `cash_cost_reduction`（現金→降成本） | `tw` |
| `schwab` | Charles Schwab | US | USD | **TWD** | `drip_us`（DRIP，30% 預扣） | `schwab` |
| `moomoo_my` | Moomoo MY | **US + MY** | USD（美股 leg）／MYR（馬股 leg） | **MYR** | US=`drip_us`（DRIP，30% 預扣）／MY=`cash`（單層淨額） | US=`moomoo_us`／MY=`moomoo_my`（依 (帳戶, 市場) 綁定） |

> **Batch B 合併（2026-07-21）**：合併前的**兩個 per-market Moomoo 帳戶（一 US-settled、一 MY-settled；
> 其 legacy account ids 見 `data_ingestion/moomoo_merge.py`）已合併為單一雙市場帳戶 `moomoo_my`**。每市場之
> 規則以顯式 binding 記於 `account_market_rules`（US → (`moomoo_us`, `drip_us`)、
> MY → (`moomoo_my`, `cash`)）；帳戶列的**標量欄位**（`settlement_ccy=USD`／`fee_rule_set=moomoo_us`／
> `dividend_model=drip_us`）pin US 對，作為**無 binding 之單一市場帳戶的 fallback**（`tw_broker`／`schwab`
> 即走此 fallback，等同舊「綁帳戶」敘述）。

要點：

- **US 市場橫跨 `schwab` 與 `moomoo_my`（後者之 US market leg），成本結構不同** → 正是費／稅／股利規則綁
  **(帳戶, 市場)** 配對（非僅市場）的理由。
- **Moomoo MY 為一個券商帳戶（`moomoo_my`），橫跨兩個市場**：US market leg 持 USD 交割之美股（經 MYR→USD
  換匯供資），MY market leg 持 MYR 交割之馬股。二市場之費／稅／股利規則各異，故綁 (帳戶, 市場)。
  **MYR 現金池於兩個市場 leg 之間共用單一 `(moomoo_my, MYR)` 操作池**（見 §9）；USD 曝險則為 `moomoo_my`
  之 USD FX pool、錨定 MYR（見 §8）。
- 交易列帶 `account_id` + `symbol`；`instruments` 表知道該 symbol 的 `market` 與 `quote_ccy`（市場由 symbol
  界定，故合併後費／稅工作範例之 `scope` 錨點以 `moomoo_my/<symbol>` 記述，市場由 symbol 帶出）。
- 換匯 pool 之**本位幣（home）= 帳戶的 `funding_ccy`**：Schwab USD pool 錨定 **TWD**，`moomoo_my` 之 USD pool
  錨定 **MYR**（見 §8）。

> **實作位置**：`data_ingestion/config_seed.py::DEFAULT_ACCOUNTS`（含 `MarketBinding` 每市場綁定）、
> `data_ingestion/moomoo_merge.py`（Batch B 一次性合併，2026-07-21）、表 `account_market_rules`、
> `shared/models/assets.py`（`Account` / `Instrument`，含 `is_etf`）。
> **依據**：`.claude/rules/domain-ledger.md`（Accounts）、`.claude/rules/markets-and-fees.md`。

---

## 3. 費用與交易稅公式（fee-engine **v2**，2026-07-15）

**單一實作**：`data_ingestion/fees.py::compute_fees(rules, side, quantity, price, *, is_etf, daytrade, stamp_fx)`。
`notional = quantity × price`。回傳 `FeeResult{fee, tax, snapshot}`，其中 **`snapshot` 為當筆使用之費率與
各費用成分之快照**（含 `engine="v2"`），逐筆存於 `transactions.fee_rule_snapshot`，使規則日後變動仍能重現
歷史（invariant I2 之延伸）。

**費率來源**：owner 完整費表 `docs/reference/broker-fee-schedules-2026-07.md`（權威來源），由
`config_seed.py::FEE_RULES` 以 **config** 承載；**每年會調整之費率（US SEC/TAF、佣金、印花）一律置於 config，
切勿寫死於函式中**（reference §肆.1）。

**捨入（per rule set）**：
- **TW（`rounding="floor"`）**：fee 與 tax 皆以**無條件捨去（ROUND_DOWN）至整數 NT$**（財政部 FE-D3，
  角以下免收）；min NT$20 於 floor **之後**比較。
- **US／MY（`rounding="half_up"`）**：**逐一費用成分**量化至 2 dp（ROUND_HALF_UP）後相加（成分別捨入為
  已載明之假設，待對帳單驗證）。

**費制並存（per-row regime clause）**：fee-engine-v2 為**逐列費制**——舊列保留其 v1 快照、以舊費制裁定；
新列以 v2 快照、v2 費制裁定。歷史列**永不重算**（見 §12.4 費用爭議註記）。stamp_fx（FE-D2）由呼叫端解析、
傳入純函式 `compute_fees`（`fees.py` 保持純淨、不觸 `conn`）。

### 3.1 TW（`tw_broker` → 規則集 `tw`，`market = TW`，`rounding = "floor"`）

$$\text{fee} = \max\Big(\big\lfloor\text{brokerage}\times\text{discount}\times\text{notional}\big\rfloor,\ \text{min\_fee}\Big),\quad \text{買賣皆有}$$

$$\text{tax} = \big\lfloor\text{rate}\times\text{notional}\big\rfloor,\quad \text{僅賣方}$$

其中賣方稅率依序判定：

$$\text{rate} = \begin{cases} \text{tax\_daytrade} = 0.0015 & \text{當沖 } daytrade=\text{True}\\ \text{tax\_etf} = 0.001 & is\_etf=\text{True}\\ \text{tax\_normal} = 0.003 & \text{現股（預設）}\end{cases}$$

種子值：`brokerage = 0.001425`、`discount = 1`（先收後退：交割收足原價，折讓次月退回，見 §3.6）、
`min_fee = 20`（NT$）、`rebate_rate = 0.77`（FORECAST-ONLY，`compute_fees` 永不使用）。`rounding="floor"` →
費與稅皆**無條件捨去（ROUND_DOWN）至整數 NT$**（FE-D3）；**min NT$20 於 floor 之後**比較（群益 142.5→floor
142；5.5→floor 5→min 20）。買方 `tax = 0`。

- **`is_etf` 來源**：標的 **registry**（`instruments.is_etf`，唯一真實來源，**永不由 sector 推導**）。
- **`daytrade`**：**逐筆旗標**，寫入並**持久化於 `transactions.daytrade`**，使重算能重現當沖稅率（見 §10）。

**已驗證範例**（驗證錨點：fee-engine v2 壓測 phase1，2026-07-15，`fee_engine.*` 80/80 通過）

| 情境 | notional | fee | tax | 驗證錨點（`scope`） |
| --- | ---: | ---: | ---: | --- |
| 2330 買 1,000@600 | 600,000 | `max(⌊855.0⌋, 20)=` **855** | 0 | `fee_engine.fee/tax tw_broker/2330 buy 1000@600` |
| 2330 賣 300@700（現股） | 210,000 | ⌊299.25⌋=**299** | ⌊0.003×210,000⌋=**630** | `fee_engine.fee/tax tw_broker/2330 sell 300@700` |
| 0050 買 1,000@1.15（**min 生效**） | 1,150 | ⌊1.6…⌋=1→**20** | 0 | 對照 群益 min 案例 |
| 2330 賣 100@725（**當沖**） | 72,500 | ⌊103.3…⌋=**103** | ⌊0.0015×72,500⌋=**108** | `fee_engine.fee/tax tw_broker/2330 sell 100@725 [daytrade]` |

> 捨入方向對照（**v2 vs v1**）：0050 賣 50@140 若 `daytrade=True`，tax = ⌊0.0015×7,000⌋ = ⌊10.5⌋ = **10**
> （v1 之 ROUND_HALF_UP 為 11）——此即 FE-D3 由 四捨五入 改為 無條件捨去之效果。

### 3.2 US — Schwab（規則集 `schwab`，`market = US`）

上市股票網路下單 **佣金 $0**；**僅賣方**加收 SEC + TAF 監理費（每年動態調整，置於 config）：

$$\text{fee} = \big[\,\text{SELL}\,\big]\cdot\Big(\underbrace{\max(\text{sec\_rate}\times\text{notional},\ 0.01)}_{\text{SEC}} + \underbrace{\min\big(\max(\text{taf\_per\_share}\times\text{shares},\ 0.01),\ 9.79\big)}_{\text{TAF}}\Big)$$

$$\text{tax} = 0.00 \quad(\text{美股無交易稅})$$

種子值：`sec_rate = 0.0000206`（min $0.01）、`taf_per_share = 0.000195`（min $0.01、cap **$9.79**）。
`broker_assisted_surcharge = 25.00` 為 config（**預設關閉**，無下單管道旗標，永不套用）。各成分量化至
**分（2 dp，ROUND_HALF_UP）**後相加。

**已驗證範例**

| 情境 | fee | tax | 驗證錨點 |
| --- | ---: | ---: | --- |
| AAPL 買 100@180 | **0.00**（買方無費） | 0.00 | 單元 `test_schwab_buy_zero` |
| 賣 100@300（notional 30,000） | SEC ⌈0.618⌉→0.62 + TAF 0.02 = **0.64** | 0.00 | 單元 `test_schwab_sell_sec_taf` |
| 賣 100,000@10（**TAF cap**） | SEC 20.60 + TAF **9.79** = **30.39** | 0.00 | 單元 `test_schwab_sell_taf_cap` |

### 3.3 US — Moomoo（規則集 `moomoo_us`，`market = US`）

$$\text{fee} = \underbrace{\max(\text{comm\_rate}\times n,\ 0.01)}_{\text{佣金}} + \underbrace{0.99}_{\text{平台}} + \underbrace{\min(0.003\times\text{shares},\ 0.01\times n)}_{\text{交收}} + \underbrace{0.000003\times\text{shares}}_{\text{CAT}} + \big[\text{SELL}\big]\cdot(\text{SEC}+\text{TAF})$$

其中 SEC／TAF 同 §3.2；$n=\text{notional}$（USD）。各成分量化至分後相加。

**大馬印花稅（tax，FE-D2）**：US 交易之印花以 MYR 計、以 USD 記帳：

$$\text{stamp\_myr} = \min\!\Big(\big\lceil (n\times\text{fx}) / 1000\big\rceil\times 1,\ \text{cap}\Big),\quad \text{cap}=\begin{cases}200 & \text{ETF}\\ 1000 & \text{正股}\end{cases}$$

$$\text{tax} = \text{round}_{2}\big(\text{stamp\_myr} / \text{fx}\big),\quad \text{fx}=\text{交易日 USD/MYR（on-or-before）}$$

`fx` 由呼叫端（manual/CSV/edit/rebalance/whatif）解析後傳入；**無匯率 → stamp 0** + soft issue
「無 USD/MYR 匯率,印花稅未計」。snapshot 記錄 `stamp_fx_rate` 與 `stamp_myr`。種子值：`commission_rate =
0.0003`（min 0.01）、`platform_fee = 0.99`、`settlement_per_share = 0.003`（cap 1%×n）、`cat_per_share =
0.000003`。

**已驗證範例（fx = 4.3；壓測 phase1 之 on-or-before USD/MYR）**

| 情境 | fee 拆解 | fee | tax（印花，換算 USD） | 驗證錨點 |
| --- | --- | ---: | ---: | --- |
| NVDA 買 30@500 | 4.50+0.99+0.09+0.00 | **5.58** | ⌈64,500/1000⌉=65 → 65/4.3=**15.12** | `fee_engine.fee/tax moomoo_my/NVDA buy 30@500` |
| NVDA 賣 25@600 | 4.50+0.99+0.08+0.00+SEC0.31+TAF0.01 | **5.89** | 65/4.3=**15.12** | `fee_engine.fee/tax moomoo_my/NVDA sell 25@600` |
| 買 1,000@0.10（**交收 cap**） | 0.03+0.99+min(3.00,1.00)+0.00 | **2.02** | — | 單元 `test_moomoo_us_settlement_cap` |

### 3.4 MY（帳戶 `moomoo_my` 之 MY market leg → 規則集 `moomoo_my`，`market = MY`，native MYR）

$$\text{comm} = \max(0.0003\times n,\ 0.01),\quad \text{clearing} = \min(0.0003\times n,\ 1000)$$

$$\text{sst} = 0.08\times(\text{comm}+\text{platform}+\text{clearing}),\quad \text{platform}=3.00$$

$$\boxed{\text{fee} = \text{comm} + \text{platform} + \text{clearing} + \text{sst}}\qquad \boxed{\text{tax} = \min\!\big(\lceil n/1000\rceil\times 1,\ \text{cap}\big)}$$

印花上限 `cap`：**正股 RM1,000**；**ETF 免徵（cap = 0 → tax 0）**；REITs/權證 RM200（**未建模 REIT 旗標**，
以 ETF 旗標為準——限制已載明）。各成分量化至 **分（2 dp）**；SST 以量化後之 comm/platform/clearing 為基（已
載明之假設）。

> **重要記帳約定**：本 app 將**印花稅記於 `tax` 欄**，**comm + platform + clearing + SST 記於 `fee` 欄**。

**已驗證範例**

| 情境 | fee 拆解 | fee | tax（印花） | 驗證錨點 |
| --- | --- | ---: | ---: | --- |
| 1155 買 1,000@9.50 | 2.85+3.00+2.85+0.70 | **9.40** | ⌈9,500/1000⌉=10 → **10.00** | `fee_engine.fee/tax moomoo_my/1155 buy 1000@9.50` |
| 1155 賣 400@11.00 | 1.32+3.00+1.32+0.45 | **6.09** | ⌈4,400/1000⌉=5 → **5.00** | `fee_engine.fee/tax moomoo_my/1155 sell 400@11.00` |
| **0800EA 買 1,000@1.15（ETF）** | 0.35+3.00+0.35+0.30 | **4.00** | **0.00（ETF 免徵）** | `fee_engine.fee/tax moomoo_my/0800EA buy 1000@1.15 [etf]` |

### 3.5 覆寫（overrides）、費制並存與費率規制（fee-engine v2 已上線）

- **手動覆寫**：使用者於輸入／編輯時可顯式改寫 `fee` / `tax`；此時系統以覆寫值為準，並在 `snapshot`
  標記 `override: true`（見 §10 之 `_recompute_edit_fees`）。
- **費率可調整（FU-D1，overlay）**：各規則集的費率／稅率／捨入方式可於「設定→帳戶與費率」調整，
  由一層 DB overlay（`data_ingestion/fee_overrides.py`，表 `fee_rule_overrides`）疊加於 v2 種子預設之上；
  **有效規則集＝v2 預設 ⊕ overlay**，於每個金額計算點 conn-aware 解析（`get_fee_rule_set(name, conn)`；
  `conn=None` 恆回種子預設，供 oracle／單元測試）。調整**僅影響未來交易**——歷史列仍以其
  `fee_rule_snapshot`（本節 §3、§10.2）為最終裁定，永不重算。重設語意：清空該欄位（null＝還原單一欄位）
  或刪除整列 overlay（每規則集／全部重設）即回種子預設。
- **fee-engine v2 已依 owner 完整費表實作（2026-07-15）**：`config_seed.py::FEE_RULES` 已載入
  `docs/reference/broker-fee-schedules-2026-07.md` 之完整費表；§3.1–§3.4 記述的即為 v2 引擎實際計算。先前 v1
  與費表之「已知分歧」（US `sec_fee` 0.0000278→0.0000206、TAF/CAT/平台/交收費、MY 結構、TW 捨入）**已於 v2
  全數收斂**。
- **費制並存（per-row regime）**：v2 為**逐列費制**。舊列以其 `fee_rule_snapshot` 之 v1 費率與捨入裁定；新列
  帶 `engine="v2"` 快照、以 v2 裁定。歷史列**永不重算**——`fee_rule_snapshot`（§3、§10.2）為最終裁定依據。
- **config 優先於寫死**：每年會調整之費率（SEC/TAF、佣金、印花）一律置於 `FEE_RULES`（config）；費率變更屬
  config 變更，須記於 `CHANGELOG.md`。
- **限制（已載明）**：REIT-specific 印花上限未建模（無 REIT 旗標，以 ETF 旗標為準）；MY/US 各費用成分之
  逐一分捨入為假設，待實際對帳單驗證；選擇權／債券／期貨／碎股不在範圍（app 僅整股股票/ETF）。

> **實作位置**：`data_ingestion/fees.py`、`data_ingestion/config_seed.py::FEE_RULES`、
> `data_ingestion/fx_lookup.py`（stamp FX 解析）；完整費表 `docs/reference/broker-fee-schedules-2026-07.md`。
> **依據**：`.claude/rules/markets-and-fees.md`。
> **驗證錨點**：§3.1–§3.4 各 `fee_engine.*`（壓測 phase1 2026-07-15，`fee_engine.fee`／`fee_engine.tax` 共
> **80/80 通過**）；邊緣案例（TAF/交收 cap、缺匯率降級）以單元測試守護。

### 3.6 折讓款預估（群益先收後退；FORECAST-ONLY，非金額之記錄）

群益 2.3 折採「交割當下收足原價 `0.1425%`、次月退回 77% 差額」。退款**永不進成本／損益／`compute_fees`**
（FE-D1）：`compute_fees` 恆以原價入帳（§3.1，`discount=1`）。系統僅**預估**退款供資訊參考：

$$\text{預估退款}_{\text{單筆}} = \big\lfloor \text{fee} \times \text{rebate\_rate} \big\rfloor,\quad \text{rebate\_rate}=0.77\ (\text{遇小數無條件捨去})$$

實作：`fees.py::forecast_tw_rebate(fee, rebate_rate)`（純函式）。**群益完整走查**：買 142 → ⌊142×0.77⌋=
**109**；賣 156 → ⌊156×0.77⌋=**120**；當月合計 229。實際退款到帳（次月）時，由 owner 於收件匣**確認**，記為
現金異動 `kind='rebate'`（折讓款），金額可編輯（預填估值；**實際值為準，估值永不為記錄**）。此預估／確認流程
（inbox、hint、cash movement）為 **Wave B** 範疇；本 §3.6 僅定義純數學公式。歸類見 §12.5（class B）。

> **驗證錨點**：`forecast_tw_rebate` 之 109/120 由單元 `test_gunyi_rebate_forecast_floor`（及 `test_fees`）
> 守護；為 FORECAST 值，**非金額之記錄**，不列入壓測純量對帳。

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

`cost_basis.py::build_book` 將五本帳按 **(日期, 同日優先序)** 排序後逐筆重播。**同日優先序**（唯一宣告
處 `shared/ledger_events.py::EventPriority`）：

$$\text{OPENING}(0) \prec \textbf{CORPORATE\_ACTION}(10) \prec \text{BUY}(20) \prec \text{SELL}(30) \prec \text{DIVIDEND}(40)$$

> **序號自 2026-08 由 0/1/2/3 改為 0/10/20/30/40**，公司行動插入於期初與買進之間（§4.4.3 說明「為何是
> 這個位置」）。間隔 10 使日後插入新事件型別**不需改動任何既有值**；序號為**具名列舉**而非散落的字面
> 常數，因為更新兩處而漏掉第三處會產生一個**排序錯誤但看似正常**的重播。**相對順序未變**，故不含公司
> 行動之帳本，其重播結果與改動前逐位元相同。

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

### 4.3 宣告式賣空（declared short sale，owner ruling 2026-07-31）

先前的「**不做**放空部位會計」立場被**收窄**、非推翻：放空會計**僅**適用於使用者**明示宣告**的
賣出，永不適用於賣超。交易列帶 `short_sale`（預設 **false**）；**只有**宣告過的賣出可超過持股，
未宣告的賣超仍由 §5.3 之 賣超 路徑處理。旗標**永不由系統推斷** — 系統無法分辨「真放空」與
「漏記買進」，若對每筆賣超自動套用放空會計，一次打字錯誤就會變成一筆**看似合理的已實現虧損**
（比原本荒謬的數字更危險）。

**重播（加權平均，不追蹤批次，與股票成本法一致）**

宣告賣出：先出清**長倉**（產生一般已實現列），餘量開／擴**空倉**，空倉持有的是**收到的淨價款**：

$$\text{short\_proceeds} \mathrel{+}= \frac{q\times p - \text{fee} - \text{tax}}{q}\times q_{\text{short}},\qquad
\text{short\_avg} = \frac{\text{short\_proceeds}}{\text{short\_shares}}$$

買進：先**回補**空倉，再將餘量計入長倉。長倉與空倉因此**互斥**（賣先吃長倉、買先吃空倉），
故一個部位恆為 多／平／空 三態之一，以**單一帶號股數**表達。

**回補損益（owner 規則：買回的每股成本結算獲利，剩下的股數以本次成本為起點）**

$$c_{\text{cover}} = \frac{q\times p + \text{fee} + \text{tax}}{q}\qquad
\text{realized} = (\text{short\_avg} - c_{\text{cover}})\times q_{\text{covered}}$$

已實現列 `kind = "short_cover"`，**記在回補日**（非賣出日）；剩餘股數以 $c_{\text{cover}}$ 起算長倉。
未平倉空倉之呈現：`shares < 0`、成本欄位為**負**（收到的價款），故 `avg = total/shares` 即**放空均價**、
`market_value = price × shares` 為負曝險、`unrealized = (price − avg) × shares` 在**跌價時為正**——
既有估值公式在帶號股數下**完全不需修改**。任何**比率**須除以 `abs(cost_total)`（分母為負會翻號，
使獲利的放空顯示為虧損）；`fully_recovered`（已回本）須以 `not short_open` 設閘（放空基礎恆為負）。

**已驗證工作範例 — `tw_broker/2609`（phase1 場景）**

| 步驟 | 明細 | 結果 |
| --- | --- | ---: |
| 未宣告賣出（空部位） | — | **422** 阻擋（錨點 `guard.short_needs_declaration`） |
| 宣告賣出 2,000 @100 | fee **285**、tax **600** | 淨價款 200,000−285−600 = **199,115** |
| 開倉狀態 | `shares = −2,000`、`original_total = −199,115` | `short_avg` = 199,115/2,000 = **99.5575** |
| 市值（price 96） | −192,000 | `unrealized = +7,115`、`unrealized_pct = +0.035733…`（**正號**） |
| 回補① 買 800 @95 | fee 108 → all-in 76,108，`c=95.135` | realized = (99.5575−95.135)×800 = **3,538** |
| 回補② 買 1,200 @98 | fee 167 → all-in 117,767，`c=98.13916̄` | realized = **1,701.999999999999999999999996** |

回補②為**循環小數**案例：`117,767/1,200` 不終止，故結果帶 28 位尾數——**刻意不四捨五入**，
與 §1.3 之「僅於結算／顯示量化」一致。

> **驗證錨點**：`holding.shares / holding.original_total / holding.original_avg /
> holding.market_value / holding.unrealized_pnl / holding.unrealized_pct / holding.short_open`，
> `scope = tw_broker|2609`；`realized.realized / realized.proceeds_net / realized.kind`，
> `scope = tw_broker/2609@2026-07-01 #16`（3,538）與 `tw_broker/2609@2026-07-06 #17`
> （1,701.999…996）；`fee_engine.fee/tax scope = tw_broker/2609 sell 2000@100 id=46`。

**放空期間之股利：不可入帳。** 放空方需**支付**股利（payment in lieu），本帳本無此借方分錄。
將已登錄之（正值）淨額列為收益，或將 DRIP／配股股數直接加入長倉，**兩者皆為金額錯誤**——後者
另會破壞長／空互斥（一筆等量 DRIP 會使部位淨為 0，持股列連同其價款一併從報表消失）。故：
嚴格路徑 **raise `UnbookableLedgerError`**（`ValueError` 子類，使既有降級點不受影響），儀表板路徑
**跳過該事件並標記 `unbookable_dividend`（待釐清）**，永不入帳。該筆請改以現金收支登錄。
錨點：`holding.unbookable_dividend scope = tw_broker|2609`、`short.unbookable_dividend_removable`。

**已知限制（非缺陷，已裁決）**：`gross_invested` **不含**放空資金（回補由已收價款支應），故純放空
幣別之簡易報酬率為 `None`（XIRR 仍為嚴謹指標）；純放空來回之 XIRR 反映**融資利率**（現金流形態
即為一筆貸款）；配置權重採淨曝險慣例，淨空頭時會翻號。

> **實作位置**：`portfolio/cost_basis.py::build_book`、`_Position`；持倉結果 `portfolio/results.py::Holding`。
> **依據**：`.claude/rules/domain-ledger.md`（Cost basis；Declared short sale 2026-07-31）。

### 4.4 公司行動（SPLIT／EXCHANGE／SPINOFF）

公司行動**改變股數而不移動任何現金**。交易帳本原本無法表達這件事：`transactions.side` 只有 `BUY` 與
`SELL`，兩者都結算金錢。其後果不是少一行報表：一次無法登錄的 3-for-1 分割，會使**其後每一筆賣出**都超過
帳上持股而被賣超護欄攔下——業主確認後，**STICKY 賣超**（§5.3）會**永久丟棄該部位的成本基礎**。該護欄是
正確的、不得放寬；正確的修法是讓股數的變動**可被登錄**。本節即為此。

**公司行動不是收入，也不是買進。** 它**不產生任何 `RealizedRow`**（§5.1），**不動 `gross_invested`**
（§7.1），**不進入 XIRR 現金流**（§7.2）——它只是把既有的基礎重新標記與重新計價。

> **規範來源**：`docs/spec/2026-08-06-corporate-actions.md`（owner 裁定 D1–D39）。本節之三式與欄位移轉表
> **逐字引用**該規格，**不以本手冊自己的話重述**：一份被改寫的公式就是第二個真實來源，而第二個真實來源
> 終將與第一個漂移（此專案已多次因此受傷）。

#### 4.4.1 帳本列與守恆律（conservation law）

**第五本永久帳本** `corporate_actions`（§1.4）：`account_id`、`date`（生效日）、`kind`（`SPLIT` /
`EXCHANGE` / `SPINOFF`）、`from_symbol`、`to_symbol`、`ratio_to`、`ratio_from`、`cost_carry`、`note`。

一筆公司行動只是把**既有部位**重新標記／重新計價，不創造也不銷毀任何東西。故於行動生效之瞬間，**在同一
報價幣內**（此即本節唯一的驗收標準）：

| 量 | 行動前後 | 備註 |
| --- | --- | --- |
| Σ `original_total`（全部部位） | **不變** | SPLIT 不動；EXCHANGE `−P + P = 0`；SPINOFF `−c·P + c·P = 0` |
| Σ `adjusted_total` | **不變** | 同上 |
| Σ `dividend_portion`（= Σorig − Σadj） | **不變** | 由上兩列導出 |
| `gross_invested` | **不變** | 僅 `opening` 與 `buy` 會增加它 |
| Σ `shares × price` | **不變（僅 SPLIT）** | 見下方限制說明 |

**價值腿僅適用於 SPLIT。** 只有分割會同時重新計價**股數與價格**（§4.4.7 之 E17／價格基礎）；EXCHANGE 是
**併入**目的部位而非重新計價它，SPINOFF 的子公司則從行動日**開始**自己的價格序列。故一筆換股若目的標的
的價格序列缺漏或以不同單位報價，**四條基礎腿全部相等而價值腿會跳動**——這是**守恆律的盲點，不是它被違
反**（基礎確實守恆）。此盲點由 D19（匯入時把券商識別碼正規化為股票代號，使其被記為 SPLIT）與 E23（對
已存在之可疑列以 賣超 層級的 `needs_confirm` 提醒，並提供一鍵轉為 SPLIT）處理。

**兩個刻意的例外**——兩者皆為真實經濟事件，故**不應**被守恆，且皆記在行動列**之外**：

1. **零股現金（cash in lieu）** → 一筆**普通賣出**（§4.4.3）。真實處分、真實已實現損益。
2. **重組費（reorganisation fee）** → 一筆 `WITHDRAW` 現金支出（§4.4.7 限制 2）。真實成本。

#### 4.4.2 比例是有理數，且求值順序具規範性

比例以**兩個正整數**儲存（`ratio_to` / `ratio_from`），**永不存成單一小數**。小數比例是**四捨五入後的
商**，而 §1.3 禁止把四捨五入後的商當作權威值——與 I7「均價永不儲存、一律 on read 相除」同一條原則。
「3-for-1」= `(to=3, from=1)`；「1-for-10」= `(1, 10)`；「2-for-7」= `(2, 7)`。

$$\boxed{\text{new\_shares} = \text{shares} \times \text{ratio\_to} \div \text{ratio\_from}}\qquad(\textbf{先乘、後除})$$

**先乘後除是正確性要求，不是風格偏好。** 本手冊作者以專案自身的直譯器實測（`.venv/Scripts/python.exe`）：

| 算式 | 先乘後除 | 加括號的商 | 相等？ |
| --- | ---: | ---: | :---: |
| `210 × 1 / 3` | **70** | `69.99999999999999999999999999` | ✗ |
| `3 × 1 / 3` | **1** | `0.9999999999999999999999999999` | ✗ |
| `935 × 18 / 17` | **990** | `989.9999999999999999999999998` | ✗ |
| `700 × 2 / 7` | **200** | `200.0000000000000000000000000` | ✓（此例恰好相等） |

`data_ingestion/validate.py` 以**裸 `>`、無 epsilon** 比較賣出量與持股。故 `210 × (1/3)` 得到的
69.999…9 會使一筆「賣光 70 股」被判為賣超 → 業主確認 → **STICKY 丟棄成本基礎**：本節存在所要防止的災難，
由算式的求值順序自行重製。窮舉掃描（股數 1–1,000 × `to` 1–20 × `from` 1–20 = 400,000 組）中有 **3,530
組跨越整數邊界**。

**唯一實作出口**：`shared/corporate_actions.py::apply_ratio`。該模組**刻意不提供**任何回傳商
（`to / from`）的屬性——暴露一個商，等於把四捨五入後的小數重新放回每個呼叫端手上，那正是它要防止的缺陷。

**兩項規則缺一不可。** 求值順序保護的是**算術**；它對**輸入**毫無作用。`ratio_to = 0.2857` 滿足
「`Decimal > 0`」、能通過 CSV 匯入與 API，並完整重製上述連鎖反應——且其誤差量級（~5×10⁻⁵）比求值順序
（~10⁻²⁷）大得多，**在任何規模都會咬人**。故 `ratio_to`／`ratio_from` **兩項皆須為正整數**，於驗證階段
強制（E6a）。

#### 4.4.3 生效時點、同日優先序、零股現金

公司行動於其生效日的**起始**生效：同日的買賣是以**行動後**的條件報價（分割後價格、新代號），故行動必須
先套用。同日優先序見 §4.1（`OPENING(0) ≺ CORPORATE_ACTION(10) ≺ BUY(20) ≺ SELL(30) ≺ DIVIDEND(40)`）。
**同日的期初庫存視為行動前**（它描述的是行動前的部位狀態）——此點在入帳時另發一則軟性提醒，因為它本質
上是有歧義的。

**零股現金（cash in lieu）**：反向分割與多數分拆會湊整並以現金支付零股。該現金是**真實處分**、有真實
損益，故記為一筆**普通賣出**（成交價 = 收到現金 ÷ 零股數），**永不**併入行動列——行動列的唯一職責是
**精確地**套用比例。範例見 §4.4.5(e)。

#### 4.4.4 三式（規範形式，逐字引用規格 §4.1／§4.2／§4.3）

`P` = `(account, from_symbol)` 之部位，`Q` = `(account, to_symbol)`。下列每一個 `× ratio` 都指
`× ratio_to / ratio_from`，**先乘後除**（§4.4.2）。

**SPLIT（`from_symbol == to_symbol`）**

```
P.shares          := P.shares × ratio_to / ratio_from
P.short_shares    := P.short_shares × ratio_to / ratio_from     # see E4
P.original_total  := unchanged
P.adjusted_total  := unchanged
P.short_proceeds  := unchanged
```

兩個總額皆不動，故兩個均價於**讀取時**自動除以新股數（各按 `1/ratio` 縮放，I7）；`dividend_portion` 與
`payback_ratio`（§6.4）亦不變——分割本就不改變「成本已被股利回收多少」。`ratio > 1` 為順向分割、
`ratio < 1` 為反向分割，**同一式涵蓋兩者**。

**EXCHANGE（整個部位移轉到新代號）**

```
carried_shares    := P.shares × ratio_to / ratio_from
Q.shares          += carried_shares
Q.original_total  += P.original_total
Q.adjusted_total  += P.adjusted_total
Q.unbookable_dividend |= P.unbookable_dividend      # E19
P.shares          := 0
P.original_total  := 0
P.adjusted_total  := 0
```

涵蓋 de-SPAC 轉換、併購、以及純代號／CUSIP 更名（`ratio = 1`）。若 `Q` 已持有部位，兩者以**加權平均**
合併——即總額相加除以股數相加，正是加權平均法本來的規定，**無特例**。`gross_invested` **不動**（無新資金
進入）。

**SPINOFF（母公司保留部位，子公司誕生）**

```
c                 := cost_carry          # fraction of the parent's basis moving to the child
Q.shares          += P.shares × ratio_to / ratio_from    # child shares per parent share
Q.original_total  += P.original_total × c
Q.adjusted_total  += P.adjusted_total × c
Q.unbookable_dividend |= P.unbookable_dividend           # E19 — the child inherits it too
P.original_total  := P.original_total − (P.original_total × c)
P.adjusted_total  := P.adjusted_total − (P.adjusted_total × c)
P.shares          := unchanged
```

母公司寫成 **`total − carved`，而非 `total × (1 − c)`**：代數上相同，數值上不同——`1 − c` 捨入一次、
`× (1−c)` 又捨入一次。實測反例（`.venv` 實跑）：`total = 3`、`c = 0.6666666666666666666666666667` 時
`total − total×c = 1.000000000000000000000000000`，而 `total × (1−c) = 0.9999999999999999999999999999`
（差 `1E-28`）。**減去恰好加給子公司的那個金額**，使 Σ`original_total` 的守恆**由構造成立**，而非碰巧
成立。（在下方 CA7 範例的 `c = 0.30` 兩式恰好相同——此規則管的是一般情形，不是這個例子。）

`cost_carry` 來自公司 Form 8-K 的配置比例，**永不臆測、內插或給預設值**；缺此值的 SPINOFF 列於驗證階段
即被拒（與 FX 池的 `acq_home_amount`、DRIP 的 `reinvest_shares` 同一立場）。母公司的份額**不儲存**，於
讀取時以 `1 − c` 求得，故母子兩側恰好加總為 1、無捨入外洩（同「均價 on read」之理）。

**完整欄位移轉表（規範，逐字引用規格 §4.4）** — `_Position` 有**九個**欄位，每一個都有明文規則，因為
「公式沒提到它」不是規格：

| 欄位 | SPLIT (P) | EXCHANGE: source P → dest Q | SPINOFF: parent P → child Q |
| --- | --- | --- | --- |
| `quote_ccy` | unchanged | Q keeps its own; E11 guarantees they match | same |
| `shares` | `× to / from` | `P := 0`; `Q += P.shares × to / from` | `P` unchanged; `Q += P.shares × to / from` |
| `original_total` | unchanged | `P := 0`; `Q += P.original_total` | `Q += P.original_total × c`; `P −= (that same amount)` |
| `adjusted_total` | unchanged | `P := 0`; `Q += P.adjusted_total` | `Q += P.adjusted_total × c`; `P −= (that same amount)` |
| `short_shares` | `× to / from` (E4) | **`P := 0`** — 見下 | unchanged (E5 guarantees 0) |
| `short_proceeds` | unchanged (E4) | **`P := 0`** — 見下 | unchanged (E5 guarantees 0) |
| `ever_oversold` | unchanged | **SOURCE** is `False` (E3 rejects). **DESTINATION** must be `False` too — **E22** rejects the action otherwise; nothing is transferred | same (E22 applies to the child's destination as well) |
| `unbookable_dividend` | unchanged | OR-ed into Q: `Q.unbookable_dividend \|= P.unbookable_dividend` (E19) | same OR into the child (E19); `P` keeps its own |
| `unbookable_action` | unchanged | OR-ed into Q: `Q.unbookable_action \|= P.unbookable_action` | same OR into the child; `P` keeps its own |

- **EXCHANGE 必須明文歸零兩個放空欄位，即使 E5 已保證它們「是 0」。** 它們是**幾乎** 0、不是 0：全數回補
  會算 `P − (P/S)×S`，而 `S` 不整除 `P` 時 Decimal 除法不精確，故殘留一個 `ε`。今日它看不見（發出的
  `shares` 是 `0 − 0`，持倉迴圈直接丟棄該部位）；但 EXCHANGE 會把來源部位以**仍然活著**的語意留在部位
  表中，日後在舊代號上的一筆買進就會讓它帶著 `−ε` 的基礎復活。
- **`unbookable_action` 會傳染**，理由與 E19 相同：帶著「被跳過的行動」的部位，其股數處於**行動前的
  單位**卻對上**行動後的價格**；把它移到後繼標的而不帶旗標，等於洗掉這個旗標存在的目的。注意它與
  `shares` 的不對稱——旗標 OR 進目的部位，而**來源保留自己的**，因為在儀表板路徑上來源可能仍是活部位。

**子公司的回本進度必須標示來源（D21）。** 兩個總額同乘 `c`，故 `c` 在比值中約分：

$$\text{child payback} = \frac{c\,(\text{orig}-\text{adj})}{c\cdot\text{orig}} = \frac{\text{orig}-\text{adj}}{\text{orig}} = \text{母公司的 payback，完全相同}$$

實測（借用 CA9 已錨定的兩個總額 60,085／56,085 作為輸入；**CA9 本身不是分拆**，此處僅示範該恆等式）：
`c = 0.30` 與 `c = 0.5831` 皆得
`0.06657235582924190729799450778`，與母公司**逐位元相同**。於是一家去年才分拆、從未發過股利的子公司會
顯示「已回收 6.66% 成本」——在加權平均下**數字本身是誠實的**（基礎承接過來，回收也隨之承接），**標籤
卻不是**。規則：凡部位之基礎源自 SPINOFF carve-out，回本進度須帶來源標示——「已回收 X.XX%（承接自
`<parent>`）」——`fully_recovered`（已回本，§6.4）同樣帶此標示。**這是標籤裁定，不是計算修正：算式不變。**
（`cost_carry == 1`（E9）是**遷移**而非複製：母公司保留股數但基礎為零，於是**實際收過所有股利的那一方**
讀出 0.00%，而子公司讀出全額。）

#### 4.4.5 已驗證工作範例（每例附驗證錨點）

以下範例取自常駐壓測 phase-1 之 `CA*` 場景（`scripts/stress_audit/run_phase1.py::run_corporate_actions`），
帳戶 `tw_broker`（TWD；費用 floor 至整數、min NT$20；賣出稅 0.3%，§3.1）。每一筆費用另有各自的
`fee_engine.fee` / `fee_engine.tax` 錨點，故範例中**沒有任何一個數字是手冊作者自行捏造的**。

**(a) SPLIT — `tw_broker/CA1`，3-for-1，且同日有一筆賣出**

| 日期 | 事件 | 逐步 |
| --- | --- | --- |
| 2026-02-03 | 買 100 @ 600 | 名目 60,000；fee `⌊60,000×0.1425%⌋ = ⌊85.5⌋ = 85`；`original_total = adjusted_total = 60,085` |
| 2026-03-02 | **SPLIT 3-for-1** | `shares = 100 × 3 / 1 = 300`；**兩個總額不變**（60,085）；`original_avg = 60,085/300 = 200.2833…` |
| 2026-03-02 | 賣 40 @ 205 | **同日**：`SELL(30)` 排在 `CORPORATE_ACTION(10)` 之後，故此筆是行動**後**的股數與價格。fee `max(⌊8,200×0.1425%⌋, 20) = max(11, 20) = 20`；tax `⌊8,200×0.3%⌋ = 24`；`proceeds_net = 8,200 − 20 − 24 = 8,156` |
| | 比例移除 | `frac = 40/300`；`adjusted_removed = 60,085 × 40/300 = 8,011.333333333333333333333331` |
| | 已實現 | `8,156 − 8,011.3333… = 144.666666666666666666666669` |
| | 期末 | `shares = 300 − 40 = 260`；`original_total = 52,073.66666666666666666666667` |

> **驗證錨點**：`corp.anchor.split_forward`，`scope = tw_broker/CA1.shares = 260`（`phase1:anchor`）；
> `export.holdings.shares` / `export.holdings.original_cost_total`，`scope = tw_broker|CA1`
> （`phase1:corp_applied` = 260 / 52,073.666…67）；`realized.proceeds_net` = 8,156、
> `realized.adjusted_removed` = 8,011.333…331、`realized.realized` = 144.666…669、`realized.kind` = `sale`，
> `scope = tw_broker/CA1@2026-03-02`；`fee_engine.fee/tax`，`scope = tw_broker/CA1 buy 100@600`（85 / 0）
> 與 `tw_broker/CA1 sell 40@205`（20 / 24）。

**(b) SPLIT 落在已被股利調整的部位上 — `tw_broker/CA9`，2-for-1**

| 日期 | 事件 | 逐步 |
| --- | --- | --- |
| 2026-02-21 | 買 200 @ 300 | fee `⌊85.5⌋ = 85`；`original_total = adjusted_total = 60,085` |
| 2026-03-05 | 現金股利 net 4,000 | `adjusted_total = 60,085 − 4,000 = 56,085`（§6.1 降成本）；`dividend_portion = 4,000` |
| 2026-04-02 | **SPLIT 2-for-1** | `shares = 200 × 2 / 1 = 400`；**兩個總額都不動**（60,085 / 56,085） |
| | 讀取時 | `original_avg = 60,085/400 = 150.2125`；`adjusted_avg = 56,085/400 = 140.2125` |
| | 不變量 | `dividend_portion` 仍為 **4,000**；`payback_ratio = 4,000/60,085 = 0.06657235582924190729799450778`，**行動前後完全相同** |

> **驗證錨點**：`corp.anchor.split_shares_dividend_adj`（`tw_broker/CA9.shares = 400`）與
> `corp.anchor.split_keeps_dividend_portion`（`tw_broker/CA9.dividend_portion = 4000`），皆 `phase1:anchor`；
> `export.holdings.original_cost_total / adjusted_cost_total`，`scope = tw_broker|CA9` = 60,085 / 56,085
> （`phase1:corp_applied`）；`fee_engine.fee`，`scope = tw_broker/CA9 buy 200@300` = 85。

**(c) EXCHANGE — `tw_broker/CA3 → CA4`，1-for-2，併入一個已持有的部位**

| 日期 | 事件 | 逐步 |
| --- | --- | --- |
| 2026-02-11 | 買 CA4 40 @ 500（**目的標的已持有**） | fee `⌊28.5⌋ = 28`；`CA4.original_total = 20,028` |
| 2026-02-12 | 買 CA3 100 @ 200 | fee `⌊28.5⌋ = 28`；`CA3.original_total = 20,028` |
| 2026-03-16 | **EXCHANGE 1-for-2** | `carried = 100 × 1 / 2 = 50` |
| | 目的 `Q` = CA4 | `shares = 40 + 50 = 90`；`original_total = 20,028 + 20,028 = 40,056`；`original_avg = 40,056/90 = 445.0666…` |
| | 來源 `P` = CA3 | `shares := 0`、`original_total := 0`、`adjusted_total := 0` |
| | **守恆** | Σ`original_total`：行動前 40,056 → 行動後 40,056 ✓ |

> **驗證錨點**：`corp.anchor.exchange_merge`，`scope = tw_broker/CA4.shares = 90`（`phase1:anchor`）；
> `export.holdings.original_cost_total`，`scope = tw_broker|CA4` = **40,056**（`phase1:corp_applied`，且
> `corp_refused` / `final` 皆同值）；`fee_engine.fee`，`scope = tw_broker/CA4 buy 40@500` = 28 與
> `tw_broker/CA3 buy 100@200` = 28。

**(d) SPINOFF — `tw_broker/CA7 → CA8`，1-for-4，`cost_carry = 0.30`**

| 日期 | 事件 | 逐步 |
| --- | --- | --- |
| 2026-02-19 | 買 CA7 400 @ 250 | 名目 100,000；fee `⌊100,000×0.1425%⌋ = ⌊142.5⌋ = 142`；`original_total = 100,142` |
| 2026-03-24 | **SPINOFF 1-for-4，`c = 0.30`** | 子公司股數 `= 400 × 1 / 4 = 100`；子公司基礎 `= 100,142 × 0.30 = 30,042.60` |
| | 母公司（`total − carved`） | `100,142 − 30,042.60 = 70,099.40` |
| 2026-03-24 | 買 CA7 100 @ 190（**同日**） | `BUY(20)` 排在 `CORPORATE_ACTION(10)` 之後，故 carve 用的是**行動前的 400 股**。fee `⌊27.075⌋ = 27`；all-in 19,027 |
| | 母公司期末 | `shares = 400 + 100 = 500`；`original_total = 70,099.40 + 19,027 = 89,126.40`；`original_avg = 178.2528` |
| | 子公司期末 | `shares = 100`；`original_total = 30,042.60`；`original_avg = 300.426` |
| | **守恆** | Σ`original_total`：`100,142` → `30,042.60 + 70,099.40 = 100,142.00` ✓ |
| | **順序可觀測** | 若行動排在同日買進**之後**，子公司會是 `500 × 1/4 = 125` 股（實測）——這正是同日優先序為何是規範而非慣例 |

> **驗證錨點**：`corp.anchor.spinoff_child`（`tw_broker/CA8.shares = 100`）、`corp.anchor.spinoff_parent`
> （`tw_broker/CA7.shares = 500`）、`corp.anchor.spinoff_child_basis`
> （`tw_broker/CA8.original_cost_total = 30042.60`）、`corp.anchor.spinoff_parent_basis`
> （`tw_broker/CA7.original_cost_total = 89126.40`），皆 `phase1:anchor`；`fee_engine.fee`，
> `scope = tw_broker/CA7 buy 400@250` = 142 與 `tw_broker/CA7 buy 100@190` = 27。

**(e) 反向分割與零股現金 — `tw_broker/CA2`，1-for-10**

| 日期 | 事件 | 逐步 |
| --- | --- | --- |
| 2026-02-05 | 買 705 @ 30 | fee `max(⌊21,150×0.1425%⌋, 20) = max(30, 20) = 30`；`original_total = 21,180` |
| 2026-03-10 | **SPLIT 1-for-10** | `shares = 705 × 1 / 10` = **70.5**——比例**精確**套用，**不**湊整 |
| 2026-03-12 | 零股現金 → **普通賣出** 0.5 @ 300 | fee `max(⌊150×0.1425%⌋, 20) = max(0, 20) = 20`；tax `⌊150×0.3%⌋ = 0`；`proceeds_net = 130.0` |
| | 比例移除 | `frac = 0.5/70.5`；`adjusted_removed = 150.2127659574468085106382979` |
| | 已實現 | `130.0 − 150.2127…9 = −20.2127659574468085106382979`（**真實的、負的**已實現損益——此處由 NT$20 最低手續費主導） |

> **驗證錨點**：`corp.anchor.split_reverse`，`scope = tw_broker/CA2.shares = 70.5`（`phase1:anchor`）；
> `realized.proceeds_net` = 130.0、`realized.adjusted_removed` = 150.212…979、`realized.realized` =
> −20.212…979、`realized.kind` = `sale`，`scope = tw_broker/CA2@2026-03-12`；`fee_engine.fee/tax`，
> `scope = tw_broker/CA2 buy 705@30`（30 / 0）與 `tw_broker/CA2 sell 0.5@300`（20 / 0）。

**(f) 比例精確性所守護的東西 — `tw_broker/CAR`，1-for-3 於 210 股**

`210 × 1 / 3` = **70**（精確）。同一筆若寫成 `210 × (1/3)` 會得到 `69.99999999999999999999999999`，
而 `validate.py` 的裸 `>` 會據此**拒絕**其後一筆「賣光 70 股」的交易。壓測中該筆賣出**成功以 201 提交**。

> **驗證錨點**：`corp.anchor.split_ratio_exact`，`scope = tw_broker/CAR.shares = 70`（`phase1:anchor`）；
> `corp.sell_exact_ratio_accepted`，`scope = tw_broker/CAR sell 70 (== 210 x 1/3)` = 201（`phase1:corp`）。
> 同型另一例：`corp.anchor.exchange_2for7`（`tw_broker/CA6.shares = 200`，即 `700 × 2 / 7`）與
> `corp.sell_exact_200_accepted` = 201。

#### 4.4.6 邊界矩陣（E1–E24）

「**嚴格路徑**」= `allow_oversell=False`（重算／試算／稅務匯出）；「**儀表板路徑**」= `allow_oversell=True`。
拒絕一律以 `UnbookableLedgerError`（`ValueError` 子類，故既有降級點不受影響）表達；「跳過並標記」
指跳過該事件並將部位標為 `unbookable_action`（**待釐清**）。此矩陣**不只是快樂路徑**：一份只記錄快樂路徑
的手冊，正是業主學到錯誤模型的方式。

| # | 情形 | 嚴格路徑 | 儀表板路徑 |
| --- | --- | --- | --- |
| E1 | 行動落在**從未持有**的標的 | 拒絕——憑空生一個部位等於發明一筆 $0 成本的幽靈持股 | 跳過 + 標記（見 E1a） |
| E1a | 同上，於**儀表板**路徑 | — | **必須跳過、不可 raise**：`portfolio/dashboard.py` 呼叫 `build_book` **沒有** try/except，raise 即 500，違反「每一個 `build_book` 呼叫點都不得 500」的既有規則。另含入帳期驗證（來源部位須**於行動日**存在）與刪除交易／期初列時的再驗證 |
| E2 | 行動落在**已結清（0 股）**的部位——判準**只看股數** | 拒絕（zh 訊息） | 跳過該事件，標記部位 |
| E3 | 行動落在**賣超**部位（`ever_oversold`，基礎已被丟棄） | 拒絕 | 跳過；賣超旗標保留。**縮放一個未定義的基礎，結果仍未定義** |
| E4 | **SPLIT 落在未平倉的宣告式空倉** | 支援：`short_shares × ratio`、`short_proceeds` **不變**。欠更多股、但收到的錢一樣多，故放空均價正確地縮放 | 同左 |
| E5 | **EXCHANGE／SPINOFF 落在未平倉的宣告式空倉** | 拒絕——沒有任何誠實的分錄（先例：放空期間之股利，§4.3） | 跳過 + 標記 |
| E6 | `ratio_to`／`ratio_from` ≤ 0、非數值、或非有限 | **驗證階段**拒絕，永不進入重播。`ratio_from == 0` 會是重播**內部**的除以零，即儀表板 500，故此拒絕是承重的 | — |
| **E6a** | **非整數的比例項**（如 `ratio_to = 0.2857`），來自任何路徑 | **驗證階段**拒絕（D14）。「表單有兩個欄位」不算防線：表單只約束表單，CSV 匯入與 API 兩條路都曾接受小數並完整重製 §4.4.2 的連鎖反應。單欄位的舊格式匯入是**硬解析錯誤**，永不強制轉換 | — |
| E7 | **SPLIT** 的 `ratio == 1` | 入帳時軟性提醒（空操作列）。**僅限 SPLIT**——EXCHANGE 的 `ratio == 1` 是一般的更名，**不得**提醒 | — |
| E8 | `cost_carry` 不在 `[0,1]`，或 SPINOFF 缺此值 | 驗證階段拒絕 | — |
| E9 | SPINOFF 的 `cost_carry == 1` | 軟性提醒：母公司保留股數但基礎為**零**——合法，但幾乎必然是資料錯誤（且會使真正收過股利的一方回本進度讀為 0.00%，見 §4.4.4） | — |
| E10 | `from_symbol` **或** `to_symbol` 未登錄於 `instruments` | 驗證階段拒絕，導向既有的「先登錄」流程。**唯一例外——SPINOFF 的 `to_symbol`（D48，owner ruling 2026-08-15）**：該標的正是這筆行動所產生，要求事先登錄等於要為一個尚不存在的證券先建檔，故降為軟性提醒、存檔時自動建立，市場與計價幣別**沿用母公司**（E11 本就要求兩者相同，屬推導而非臆測；名稱與產業留空，那是另一家公司的事實）。**仍非靜默**：畫面先寫明將建立什麼，因為打錯代號會生出一檔永遠查不到報價的標的（D19 的理由）。來源標的與 EXCHANGE 的 `to_symbol` **維持硬性拒絕** | — |
| E11 | 兩個標的的**報價幣不同** | 驗證階段拒絕。跨幣別搬運基礎需要行動日匯率，發明一個就會汙染基礎 | — |
| E12 | 同日、同帳戶、**標的集合相交**的兩筆行動 | **驗證階段拒絕**（D15），不做 tie-break。以 `id` ASC 排序只是「業主打字的順序」偽裝成經濟順序，而兩種順序**產生不同的金額**（實測 600 股 vs 200 股）；且守恆律**看不見**它（兩者 Σ 皆相同）。真正的兩步事件請記為**一列**（反向分割 + 更名就是一筆 EXCHANGE），或把日期拆開 | — |
| E13 | 同一標的**存在於兩個帳戶** | **全有或全無**（D13）。部位以 `(account, symbol)` 為鍵，故 N 個帳戶需 N 列；**部分套用於驗證階段拒絕**。未套用的帳戶會拿**行動前的股數**去對上**行動後的價格**（`prices` 無 `account_id`，價格修正是全域的），而所有既有檢查都會是綠燈——缺陷活在股數與價格**之間**，系統沒有任何一處計算這個關係 | — |
| E14 | 一筆**回溯到行動之前**的賣出，事後才輸入 | 由日期感知護欄處理——**前提是** `shares_through` 會套用公司行動。這是最容易漏掉的整合點 | — |
| **E15** | **完全相同的行動被輸入兩次** | **驗證階段硬拒絕**（D29），且必須排在 **E12 之前**，並有自己的訊息。行動是**事件**不是交易：沒有任何帳本會讓同一天兩筆相同的 3-for-1 都正確。確認後放行等於**把比例套用兩次**（3-for-1 變 9-for-1） | — |
| E16 | **編輯／刪除**一筆行動會**重算歷史** | 意圖如此（§10、`domain-ledger.md` N2）。與其他帳本編輯一樣進 `ledger_audit` | — |
| E17 | **既存價格基礎 vs 行動** | 見 §4.4.7「價格基礎」：以**成交當時**的計價為權威、於寫入接縫還原、以 `fetched_at` 判別修正、讀取時再表述 | — |
| **E18** | **EXCHANGE／SPINOFF 的 `to_symbol` 持有未平倉空倉** | 拒絕。`Q.shares +=` 會破壞長／空**由構造互斥**的前提，發出的持倉將是「真實成本基礎混著放空價款」，均價失去意義，`abs(cost_total)` 比率與 `fully_recovered` 閘門雙雙失準 | 跳過 + 標記 |
| **E19** | EXCHANGE／SPINOFF **來源帶有 `unbookable_dividend`** | 允許——但旗標**傳染**（`Q \|= P`）。行動本身是合法的，因為一個無關的舊資料問題而封殺它並不合理；但若不傳染，來源會因 0 股被丟棄，**未解決的金額問題就被一個無關事件抹掉**了 | 同左 |
| **E20** | `to_symbol` 與 `from_symbol` 的**每種 kind 的一致性** | SPLIT **要求** `to == from`；EXCHANGE 與 SPINOFF **拒絕** `to == from`。自我 EXCHANGE 會歸零再加回、偽裝成更名卻悄悄按 ratio 重新縮放；自我 SPINOFF 會把 `c` 的基礎切出來又加回同一個部位，**重複計算**。三者皆於驗證階段強制 | — |
| **E21** | 行動引用了**未登錄於 `instruments`** 的標的（經 CSV 匯入或事後刪除標的而繞過 E10） | 其兩個標的都必須加入儀表板的「未登錄跳過集」，否則 `quote_ccy()` 會 `KeyError` → **500**，且例外型別與其他所有降級路徑都不同 | 與該標的的其他列一起被跳過 |
| **E22** | **EXCHANGE／SPINOFF 的 `to_symbol` 部位帶 `ever_oversold`** | 拒絕——E18 的鏡像、E19 的更深一層：E19 阻止**旗標**被洗掉，這裡阻止**成本基礎**被復活到一個基礎已被刻意丟棄的部位上。實測：行動前讀「均價 0／未實現 +1,890」沒人相信；行動後讀「均價 33.33／未實現 −660」看起來再正常不過 | 跳過 + 標記 |
| **E23** | **EXCHANGE、`ratio_to != ratio_from`、`to_symbol` 有既往價格、`from_symbol` 完全沒有** | **`needs_confirm`（賣超層級）**，非硬拒絕、也非被動通知。這四項合起來就是「券商識別碼」的簽名：真正的併購，其來源曾是掛牌證券、有價格。提供一鍵轉為 SPLIT。**確認後該列即以 EXCHANGE 入帳、價值斷崖仍在**（不對任一序列套價格因子），確認換到的是「這個不連續被記錄且被看見」，而不是它消失 | 同左 |
| **E24** | **股利落在已被 EXCHANGE 搬走的標的上**（D32） | 拒絕，**兩條路徑一致**。EXCHANGE 會把來源留在部位表中且欄位歸零（§4.4.4，為了阻止 `−ε` 復活），故「部位存在」且「沒有空倉」兩個既有拒絕都不成立，該筆股利會直接入帳：CASH/NET 會在一個已死代號上記入清倉後已實現收益；DRIP/STOCK 會 `shares += reinvest_shares` **讓部位復活**於 `avg = 0`，而它已下市永遠拿不到價格，**一檔無價持股會讓整個投組的 XIRR 無限期空白** | 跳過該事件，標記部位（待釐清）；該筆請改記為**現金收支** |

> **實作狀態註記（2026-08-11 就地查核，非規則的一部分）**：上列為**裁定規則**；若程式輸出與之不符，
> 依 §12.4 步驟 4 屬**程式缺陷**。本次查核所見：
>
> - **重播已實作**（`cost_basis.py::_apply_action`）：E1、E1a（儀表板路徑跳過）、E2、E3、E4、E5、
>   E18、E19、E22。
> - **帳本載入已實作**（`shared/models/ledger.py::unregistered_symbols` / `without_unregistered`）：
>   E21（行動之**兩個**標的都計入未登錄跳過集）。
> - **股數走查已實作**（`data_ingestion/holdings.py`）：E14（日期感知護欄走行動感知路徑）。
> - **價格基礎已實作**：E17（`pricing/schema.py`、`pricing/store.py`、`pricing/reconcile.py`、
>   `portfolio/price_basis.py`）。
> - **驗證已實作但尚無生產端呼叫者**（`data_ingestion/validate.py::validate_corporate_action`）：
>   E6、E6a、E7、E8、E9、E10、E11、E12、E13、E15、E20、E23——入帳表面為後續工作包，在其接上之前
>   這些拒絕與提醒**只在測試中生效**。
> - **尚未實作：E24**（`_Position` 目前無區分「被行動歸零」與「一般清倉」的標記）。
>
> 此註記僅描述實作進度，**不改變上述任一列的裁定**。

#### 4.4.7 價格基礎、已知限制與硬性排除

**價格基礎（E17）。** 儲存的價格以「**成交當時的計價**」為權威。行情供應商對分割日**之前**的日期回傳的
是**已還原**（post-split）的收盤價，而帳本對同一日的股數**沒有**被還原——兩者相乘即得到一個錯誤的市值。
故：`prices` 以兩欄表達基礎——`close_raw`（供應商原樣交付的值，**不截位**）與 `split_basis`（已套用的
因子），而 `close = close_raw × split_basis` 於寫入接縫**重算**（4 dp cap 套在**乘積**上，§1.3）；讀取
時，若一個價格是**跨越分割日被沿用**的，再以 `portfolio/price_basis.py::split_factor` 重新表述成估值
當日的股數單位。**因子只用於價格，永不用於股數**（股數走 §4.4.2 的兩項整數）。此再表述**限定於 SPLIT**：
EXCHANGE 是併入目的標的而非重新計價它，若把因子擴及 EXCHANGE，會汙染任何一個**已持有**之併購目的標的
的價格歷史。

**（限制 1，長期有效）D11 — `volume` 不做還原。** 價格有 `close_raw` 可還原，`volume` 沒有對應的原始
欄位，且供應商對成交量的還原方向**未經量測**——猜一個方向會違反本節所執行的同一條規則。故跨越分割日的
**量能類訊號不可直接比較**。成交量不是「金額之記錄」（§12.5 class B），故此為**已接受並載明的長期限制**，
而非缺陷；若日後要處理，正確作法是新增自己的 `*_raw` 欄位並連同因子一起帶走。

**（限制 2）D12 — 重組費對「本系統現有的每一個報酬指標」都不可見。這是一個常設限制。**
可登錄的現金收支共**七種**——`DEPOSIT` / `WITHDRAW` / `OPENING` / `REBATE` /
`INTEREST` / `INTEREST_EXPENSE` / `BROKER_FEE`（`data_ingestion/validate.py::CASH_MOVEMENT_KINDS`
= `shared/cash_kinds.py::CASH_KIND_VALUES`），其中**三種是借方**：`WITHDRAW`、`INTEREST_EXPENSE`、
`BROKER_FEE`（`portfolio/cash.py::_movement_sign` → 同一張表；兩軸表見 §9.1.1）。
（**本段於 2026-08-13 就地更新**：先前寫「只有四種、只有 `WITHDRAW` 是借方」並引用
`api/routers/cash.py::_KINDS`——該常數已不存在，述詞亦已由 §9.1.1 的表取代。）
**裁定：重組費記為一筆 `WITHDRAW` 並於 `note` 註明。** 其後果須明說：它在現金對帳單上讀起來像
「業主提領」；它減少外幣池曝險卻不認列已實現換匯（§8、`domain-ledger.md` N1）；而
`portfolio/returns.py::xirr_reporting` 的流量序列**只由** `opening` + `transactions` + `dividends`
構成，故**現金收支永遠不會進入 XIRR**——這一點在 2026-08-13 由業主裁決 **D1 = A** 就三種新 kind
再次確認並升格為明訂規則（§7.2）。**新增的三種 kind 沒有讓這個盲點變小**：`BROKER_FEE` 也是一筆
「真實發生、卻對每一個報酬指標不可見」的金額，只是它現在至少能以自己的名字入帳，而不必偽裝成一筆
提領。

> **⚠ 本限制之 XIRR 部分已於 2026-08-24 由 AI-D42 部分取代——上方原文完整保留，因為「當初為何那樣
> 決定」本身是紀錄的一部分。** 變更的是：`REBATE`／`INTEREST_EXPENSE`／`BROKER_FEE` 三類（**交易與
> 融資的成本**）自此進入 `xirr_reporting` 的流量序列。**未變更的是本限制指名的那個案例**——重組費記為
> `WITHDRAW`（資本移動），在 AI-D42 之下仍然不進 XIRR，故限制 2 的具體結論原樣成立。
> `DEPOSIT`／`WITHDRAW`／`OPENING` 全數維持排除（納入會把 XIRR 從「投入證券的資金報酬」悄悄改成
> 「帳戶報酬」，那是另一個指標）；`INTEREST`（閒置現金利息）亦排除，因為孳生它的本金從未進入 XIRR 的
> 分母，只把收益計入分子是不對稱的。
> **取代的理由，用本段自己的話**：上文寫著「`BROKER_FEE` 也是一筆『真實發生、卻對每一個報酬指標不可
> 見』的金額」——本手冊自己已將此記為盲點，而 D45 撤銷了本來要涵蓋它的帳戶層 IRR。AI-D42 補的是這個
> 已被點名的缺口。量級依據：FE-D1 的 77% 退佣單趟差 **0.229% 資本**，且為 owner 券商之常態計費。

> **本段於 2026-08-11 改寫（D45）。** 先前版本寫的是「這不是永久盲點」，理由是 D36 將另加一個
> **帳戶層 IRR**（whole-account IRR）於 `portfolio/twr.py`，而該指標**看得見** `WITHDRAW`。
> **D36 已由業主裁定不做（D45，2026-08-11），且從未實作**（就地查核：`portfolio/twr.py` 只有
> `twr_index` / `convert_closes` / `build_overlay` 三個純函式，沒有任何 IRR）。因此：
>
> **重組費在本系統現有的每一個報酬指標中都看不見，而且沒有預定的解決方案。** XIRR 的不可見是
> **刻意的**——正因如此，每一個歷史數字、本手冊每一個已錨定的工作範例、以及壓測 oracle 的每一個
> 期望值都**留在原處**，D45 沒有移動任何一個數字。它可見之處只有現金收支本來就可見的地方:
> **現金帳與淨值**。
>
> 這段話刻意不再寫「待 D36」。一份承諾了永遠不會到來的修正的手冊，比一份把盲點講清楚的手冊更糟——
> 讀者會停止尋找變通作法。
> 此外，重組費屬於一整類「永遠到不了 XIRR」的項目（債券／融資利息、利息調整、ADR 管理費、外國稅退還），
> 該類別的完整處理屬於券商匯入 backlog 的範圍——本節不為其中單一成員發明一個局部答案。

**（硬性排除，D34）現金＋股票混合併購（cash-and-stock merger）。**「A 的每一股換 0.6 股 B 外加現金
US$12.00」——**不新增第四種 kind，且沒有任何受支援的輸入方式**。這是**硬性排除**，理由必須寫出來，否則
它看起來會與「零股現金記為普通賣出」的裁定自相矛盾：

- **舊版規格曾載的「兩列作法」（同一生效日記一筆 SELL + 一筆 EXCHANGE）已於 2026-08-10 撤銷，不得以
  任何形式作為程序出現在本手冊。** 它**跑不起來**：`EventPriority` 讓 `CORPORATE_ACTION(10)` 先於
  `SELL(30)`，EXCHANGE 會把來源股數歸零，同日的 SELL 於是落在一個 0 股部位上——嚴格路徑
  `OversellError`，儀表板路徑 **STICKY 賣超、成本基礎被丟棄**：正是本節開頭所說、這整個功能存在要防止
  的災難，而且是**照著文件做**做出來的。把事件重新排序不是選項：行動於其日期之**起始**生效、當日交易以
  行動後條件報價，是本節其餘每一條公式的前提。
- **正確的比例通常也輸入不進去。** 加權平均下現金腿處分掉 `f × N` **股**，故 EXCHANGE 只帶
  `(1−f) × N` 股，公告的比例會**超額交付**。真正正確的比例是 `B_received / ((1−f) × N)`，一般**無法**
  表達為兩個正整數——而 E6a／D14 正是要求它必須是。
- **若整筆只記成一筆 EXCHANGE**：100% 的基礎被搬走，現金**哪裡都沒記**（不是收款、不是已實現、不是
  XIRR 流量），目的標的的均價被高估，而**守恆律會通過**——因為那筆錢就是離開了帳本（§4.4.1 的盲點）。
  即「錯得徹底，且偵測不到」。這就是為什麼它是一條**明載的**排除，而非默默不提。

> **非官方變通（unofficial workaround，若真的遇到）**：把**全部**股數 EXCHANGE 到目的標的，再**賣出
> 目的標的**以取得現金對價（優先序 10 然後 30，故排序可行、不觸發任何護欄）。**這不是規範作法，也不得
> 被當成規範作法引用**：它的已實現金額與「按對價相對價值分攤基礎」的結果**不同**（該處分是以目的標的
> 的條件計價，而非把基礎分攤到兩種對價上），故稅務包所報的損益會與登記機構的配置所隱含的**不一致**。
> 要用請在知道這一點的前提下用，或先記錄該事件並提出詢問。

> **驗證錨點**：公司行動之三式與拒絕路徑由 phase-1 之 `CA*` 場景錨定——`corp.anchor.*` 共 **13 個絕對
> 錨點**（11 個於 `phase1:anchor`、2 個於 `phase1:corp_refused`）、
> `corp.refusal_codes`（`corp_applied` = `[]`；`corp_refused` = `["E5","E2","E1"]`）、
> `corp.anchor.e5_source_unmoved`（`tw_broker/CAX.shares = −500`，被拒的行動**沒有改變任何金額**，只改變
> 揭露）、`corp.anchor.e5_source_flagged`（`unbookable_action = True`）、`corp.e1a_dashboard_200`
> （落在從未持有標的的行動**不得 500**）、`corp.xirr_blanked_by_unapplied`（3 筆未套用的行動 → `xirr`
> 為 `None`）、`corp.xirr_reason_names_row`（原因字串須指名**帳戶、標的、日期**三者）。
> **D11 / D12 / D34 三項限制目前無壓測錨點**（`volume` 與重組費皆非本場景所觸及；現金＋股票併購依定義
> 無法輸入），建議下一輪對抗性對帳時，至少為「重組費 `WITHDRAW` 不影響 XIRR」補一個負向錨點。
> **實作位置**：`shared/corporate_actions.py`（`CorporateAction`、`apply_ratio`、`split_factor`、
> `ActionIndex`；比例代數的唯一擁有者）、`shared/ledger_events.py::EventPriority`、
> `shared/ledger_registry.py::LEDGER_TABLES`、`portfolio/cost_basis.py::build_book`（`_apply_action`、
> `_reject`、`UnbookableLedgerError`、`Book.unapplied_actions`）、`portfolio/results.py::UnappliedAction`、
> `data_ingestion/store.py`（唯一的 SQL）、`data_ingestion/validate.py::validate_corporate_action`、
> `data_ingestion/holdings.py`（行動感知的股數路徑；`shares_naive` 刻意保持**不感知**，因為
> `corporate_delta` 之定義即為兩者之差）、`pricing/schema.py` + `pricing/store.py`
> （`close_raw` / `split_basis`）、`pricing/reconcile.py`（SPLIT 帳本變動時重述既存收盤價）、
> `portfolio/price_basis.py`（讀取時再表述）。
> **依據**：`docs/spec/2026-08-06-corporate-actions.md`（§2 守恆律、§3 資料模型與比例、§4 重播語意與
> 欄位移轉表、§5 邊界矩陣、§8 裁定 D1–D39、§9 排除項）、`.claude/rules/domain-ledger.md`（Cost basis；
> 賣超 STICKY；Declared short sale）、`.claude/rules/data-and-pricing.md`（精度；不得儲存四捨五入後的商）。

---

## 5. 已實現／未實現損益

### 5.1 已實現損益（Realized P&L）

於每筆**賣出**產生一列 `RealizedRow`（`cost_basis.py`，`kind="sale"`；另有一種
`kind="dividend"` 之已實現列，見 §6.3b）：

$$\text{proceeds\_net} = \text{quantity}\times\text{price} - \text{fees} - \text{tax}$$

$$\boxed{\text{realized} = \text{proceeds\_net} - \text{adjusted\_removed}}$$

即：**淨賣出價款（扣費扣稅後）− 賣出比例對應之 `adjusted_avg × shares_sold`**。已實現以**調整後成本**衡量
（股利已折入成本，故不另立股利收入行 → invariant I4，避免重複計算）。跨幣別以
`RealizedPnL.by_currency` 分幣彙總。

> **公司行動（§4.4）與已實現損益的關係。** 公司行動**本身不產生任何 `RealizedRow`**——它不是處分、不是
> 收入、不是買進，只是把既有基礎重新標記／重新計價（§4.4.1 守恆律）。但它會改變**其後每一筆賣出**的
> 已實現金額，因為 `adjusted_avg = adjusted_total / shares` 於**讀取時**相除：分割後總額不變而股數變了，
> 均價即自動按 `1/ratio` 縮放。**兩個真實的例外**確實產生已實現列，且都記在行動列**之外**：**零股現金**
> 是一筆普通賣出（§4.4.3；已驗證例 §4.4.5(e)，`realized = −20.2127659574468085106382979`），**重組費**
> 是一筆 `WITHDRAW` 現金支出（§4.4.7 限制 2）。裁定任一「行動之後」的已實現金額時，必須把
> `corporate_actions` 帳本一併納入重播（§1.4、§12.4 步驟 2），否則會得到一個**看似正常、卻按行動前
> 股數計算**的數字。

**已驗證範例**

| 賣出 | proceeds_net | adjusted_removed | realized | 驗證錨點 |
| --- | ---: | ---: | ---: | --- |
| `tw_broker/0050` 2026-04-10（50@140） | 6,973 | 6,609.0909… | **363.9090…** | `realized.realized tw_broker/0050@2026-04-10` |
| `schwab/TSLA` 2026-04-20（20@260） | 5,199.88 | 5,000.00 | **199.88** | `realized.realized schwab/TSLA@2026-04-20 #3`（`phase1:final`） |

（TSLA 賣出 fee = 0.12（SEC 0.11＋TAF 0.01，見 §3.2／E4）→ `proceeds_net = 5,200 − 0.12 = 5,199.88`。分幣別已實現以逐事件錨點 `realized.realized`（14 筆，`phase1:final`）為準；換算報告幣後之累計已實現 `kpi.realized_total TWD = 186,333.50…`（`phase1:final`，見 §7.1）。native-ccy 之累計加總非單一錨點，故本版改引上述已錨定之逐事件與報告幣總額，不再列 run-specific 之三幣手算彙總。）

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
  - 修復方式：補登遺漏的期初庫存／買入，**或補登遺漏的公司行動**（§4.4）——一次未登錄的 3-for-1 分割會
    使其後每一筆賣出都被判賣超，而 賣超 是**黏性**的：事後補登的買進不會把已丟棄的基礎還回來。
- **未能套用之公司行動（`unbookable_action`，2026-08）**：與上兩者同族的第三種誠實退化。嚴格路徑
  `UnbookableLedgerError`；儀表板路徑**跳過該事件**並將部位標記 `unbookable_action`（**待釐清**）。
  被跳過的行動**沒有改變任何金額**，只改變揭露——股數停在**行動前**的單位，對上的卻是**行動後**的價格，
  故該部位是「待釐清」而不只是「價格過期」。記錄放在 **`Book.unapplied_actions`（帳本層）**而非只放在
  持倉上，因為三種發生方式中有兩種**根本不留下任何持倉可標記**（E2 的來源 0 股已被丟棄、E1 的來源從未
  存在）。凡 `unapplied_actions` 非空，該帳本的**股數即不可信**：XIRR 因此**整個投組**空白（§7.2），
  且原因字串必須指名**帳戶、標的、日期**。判準與拒絕清單見 §4.4.6。

> **驗證錨點**：`guard.oversell_blocks`，`scope = tw_broker/0050 sell 200>held 110`（賣 200 > 持有 110 → 422
> `oversell_unacknowledged`）。（壓測 op 序號逐版重編，故此處以穩定的 check + scope 描述，不釘選 run-specific 之 op 編號。）
> **實作位置**：`portfolio/cost_basis.py`（`OversellError`、`RealizedRow`）、`portfolio/pnl.py::value_holdings`、
> `api/routers/input_center.py::manual_commit`。
> **依據**：`.claude/rules/domain-ledger.md`（P&L and returns；Data integrity）。

---

## 6. 股息三模型

實作：`data_ingestion/dividend_model.py::apply_dividend_model`（衍生 withholding／net／reinvest_shares）
+ `cost_basis.py::build_book` 之股利分支。同日優先序中股利排最後（見 §4.1）。
`CASH_DIVIDEND_TYPES = {CASH, NET}`（TW 現金 + MY 單層淨額共用同一「降成本」定義；**美股現金派發自
P1b（2026-08-13）起亦以 `CASH` 入帳並走同一式**，見 §6.2b——本章仍是**三個模型**，只是 `drip_us`
模型現在同時受理 `DRIP` 與 `CASH` 兩種型別）。

### 6.1 TW 現金（`CASH`，`tw_broker`）— 降成本

記**淨收金額**；折入調整後成本，**不另立收入行**：

$$\text{adjusted\_total} \mathrel{-}= \text{net}\qquad(\text{net 於 TW 現金 = gross}，\text{無預扣})$$

**已驗證範例**：`tw_broker/0050` 股利 net 800（2026-06-12，在最後買入之後、且此後無賣出）→ 全數作用於
最終 110 股 → `dividend_portion = 800.00`、`adjusted_total = 14,050.909…`（見 §4.2）。

### 6.2 US DRIP（`DRIP`，`schwab` / `moomoo_my` 之 US market leg）— 30% 預扣、$0 成本再投資

$$\text{withholding} = \text{gross}\times 0.30\qquad \text{net} = \text{gross} - \text{withholding}$$

$$\text{reinvest\_shares} = \frac{\text{net}}{\text{reinvest\_price}}\quad(\text{reinvest\_price = 登錄之再投資價})$$

再投資股數以 **$0 成本**加入部位：`shares += reinvest_shares`；**`adjusted_total` 不變**（DRIP **不**降調整
後成本）→ 均價因加入零成本股而自然下降。DRIP 於現金流上**中性**（見 §7、§9）。

**已驗證範例 — `schwab/MSFT` 股利 id=1**：`gross 100 → withholding 30.00 → net 70.00`，
`reinvest_price 350 → reinvest_shares = 70/350 = 0.20` 股，`$0` 成本加入。故 MSFT `dividend_portion = 0.00`
（調整後成本未被股利改變），`shares` 增加 0.20。
驗證錨點：`ledger.div.gross/net`（`schwab|MSFT`）、`holding.dividend_portion schwab|MSFT = 0.00`、
`holding.shares schwab|MSFT`。

`US_WITHHOLDING = 0.30` 適用 Schwab 與 `moomoo_my` 之 US market leg 兩處美股（W-8BEN）。**此 30% 乘積
只在 `DRIP` 型別上自動推導**；未再投資的美股現金派發見 §6.2b。

### 6.2b 美股現金股利（`CASH` on `drip_us`；P1b，2026-08-13）— 與 TW／MY 現金同式降成本

DRIP 是美股帳戶的**預設機制，不是唯一機制**：真實券商匯出檔的股利列比再投資列多，差額正是**未再投資
的現金派發**。P1b 於 `dividend_import.py::_MODEL_ALLOWED_TYPES` 將 `CASH` 納入 `drip_us` 的可受理型別
（`{"DRIP", "CASH"}`），**移除了原本每一列都要人工確認的 `dividend_type_mismatch` 軟阻擋**。

**會計上沒有新規則**（owner 裁定 **D35**，2026-08-10；本節只錨定公式，不重新裁決）：`CASH` 落在
`CASH_DIVIDEND_TYPES`（`shared/models/enums.py`），故與 §6.1 TW 現金、§6.3 MY 單層淨額**同一式**：

$$\text{net} = \text{gross} - \text{withholding}\qquad
\text{adjusted\_total} \mathrel{-}= \text{net}$$

若改記為收入行，同一本帳將同時存在**兩套股利會計模型**，`dividend_portion` 與 §6.4 之回本進度／股利
回收率會在**同一個畫面上**依市場而有不同意義——這正是一定義原則所要防止的。

**預扣由使用者填寫，不由 `gross × 0.30` 推導。** `apply_dividend_model` 依**股利型別**（非帳戶模型）
分派：`DRIP` 走 §6.2 的 `gross × 0.30`，`CASH` 走**已登錄之預扣**（`withholding` 未填 → **0**，
`net = gross`）。這是刻意的：券商實扣金額經其自身的分位進位，與 `gross × 0.30` 的乘積**不會逐分相符**；
把使用者自對帳單抄入的數字覆寫成一個算出來的乘積，等於以推導值取代**金額之記錄**。

**但空白預扣在 `drip_us` 上會被追問（軟性）。** W-8BEN 下的美股派發通常有 30% 預扣；`withholding`
留白會使 `net = gross`，**過度沖減 `adjusted_total`**，並在該部位存續期間持續低報未實現損益。故
`drip_us` 帳戶之 `CASH` 列若無 `withholding` → 軟性 `us_cash_dividend_no_withholding`
（`needs_confirm`，**非**硬阻擋——無預扣之美股派發確實存在，例如資本返還），且**該列仍照它所寫的入帳**
（`withholding = 0`、`net = gross`）：警告不靜默改數。此追問綁定 **`drip_us` 模型**而非 `CASH` 型別，
TW 現金股利本就無預扣，不得被追問。

`NET`（MY 單層淨額）在 `drip_us` 上**仍是** `dividend_type_mismatch`（合併雙市場帳戶之防呆，Batch B
F01）。清倉後入帳之美股現金股利與 TW／MY 同走 **§6.3b**（改記一列 `RealizedRow(kind="dividend")`）——
該分支判定的是 `CASH_DIVIDEND_TYPES`，不是市場。

> **驗證錨點**：hermetic 迴歸 `tests/data_ingestion/test_dividends.py`——
> `::test_us_cash_dividend_is_not_a_mismatch`（`schwab/AAPL` `CASH` gross 100／withholding 30 →
> `net = 70`，且 **issue 為空**）、`::test_us_cash_dividend_without_withholding_asks`（空白預扣 → 軟性
> `us_cash_dividend_no_withholding`，payload 仍為 `withholding = 0`／`net = 100`）、
> `::test_tw_cash_dividend_is_not_asked_about_withholding`（TW 現金不被追問）、
> `::test_csv_dividend_type_market_coherent_has_no_mismatch`（`DRIP` 仍無 mismatch）。
> **壓測 `scope` 錨點**（2026-08-13 加入場景）：`schwab/AAPL@2026-06-10` 之 `CASH` 股利
> （gross 25／withholding 7.50），錨定 `holding.dividend_portion scope = schwab|AAPL`（**非零**——
> 同帳戶之 MSFT DRIP 仍為 0，兩者並存正是本節主張「同一美股部位可在不同季度以兩種型態配息」的證據）
> 與 `holding.adjusted_total scope = schwab|AAPL`。此前本節只有 hermetic 錨點：「不需要會計變更」
> 這句話唯有在獨立 oracle 重算該部位並同意之後才算數。
> **實作位置**：`data_ingestion/dividend_import.py::_MODEL_ALLOWED_TYPES`（`drip_us: {"DRIP","CASH"}`）
> 與其 `us_cash_dividend_no_withholding` 分支、`data_ingestion/dividend_model.py::apply_dividend_model`
> （型別分派）、`shared/models/enums.py::CASH_DIVIDEND_TYPES`。
> **依據**：owner 裁定 **D35**（完整敘述於 `docs/spec/2026-08-06-broker-import-backlog.md`；
> `docs/spec/2026-08-06-corporate-actions.md` §8 以 D21 之前提記錄之）、`.claude/rules/domain-ledger.md`
> （Dividend models）。

### 6.3 MY 現金（`NET`，`moomoo_my` 之 MY market leg）— 單層淨額降成本

馬來西亞單層制（single-tier）：記**淨收金額**，與 TW 現金同走降成本：`adjusted_total −= net`。
驗證錨點：`ledger.div.net moomoo_my|1155`；`holding.dividend_portion moomoo_my/1155 = 306.25`
（注意：因該部位在股利後仍有賣出，`dividend_portion` 會隨賣出**比例移除**，故不等於累計股利總額——
交叉參見 §4.1 比例移除、§5.1）。

### 6.2c 除息日 vs 發放日（`ex_date`；R6 / 審查 ⑧，2026-08-26）

**規則**：`dividends` 有兩個日期欄。`date` 是**發放日**（錢入帳／再投資買進的那天），
`ex_date` 是**除息／除權日**，可為 NULL。重播採用的生效日：

```
effective_date = ex_date   若 type = STOCK 且 ex_date 非 NULL
               = date      其餘一切情況
```

**只有 `STOCK`（配股）移動**，判準是「那一天你實際擁有什麼」：

| 類型 | 生效日 | 理由 |
| --- | --- | --- |
| `STOCK` | `ex_date or date` | 配股是**除息日就附著的權利**，股價當天也已調整。用舊股數對已調整股價撐到發放日，10% 配股會讀成約 −9% 虧損，長達約一個月 |
| `DRIP` | `date` | 再投資是發放日以 `reinvest_price` **買進**的，那些股票在此之前不存在 |
| `CASH` / `NET` | `date` | 除息日股價掉、發放日成本才沖減，中間的凹陷是**誠實的**——部位確實變便宜、錢確實還沒到；與配股不同，除息日你**擁有的東西沒有改變** |

⚠ **生效日必須同時作用於三個過濾點**：`build_book` 的事件排序、`LedgerBundle.through(day)`
（趨勢逐日重播）、`before_action_on(day)`（公司行動的四條硬拒絕所讀的書）。其中 `through` 最關鍵——
它以日期過濾 `dividends`，若不改，事件在除息～發放期間**根本不在 bundle 裡**，只改排序等於沒改。
規則因此收在單一 property `Dividend.effective_date`，而非在三處各寫一份條件。

⚠ **既有列一律 NULL，數字逐位元組不變。** 舊列的除息日不可回溯，猜一個正是本手冊處處禁止的事。

驗證錨點：`holding.shares tw_broker/2330` 在 `2026-05-02`（除息日）已含配股 100 股，
而其發放日為 `2026-05-30`；同一組資料在 `ex_date` 為 NULL 時，`2026-05-02` 的股數不含該 100 股。

### 6.3b 清倉後入帳之現金股利（`CASH` / `NET`）— 列為已實現收益

**規則（2026-07-26 稽核 H2；適用 `CASH_DIVIDEND_TYPES` 全部——TW `CASH`、MY `NET`，以及 P1b 起之美股
`CASH`，見 §6.2b）**：現金股利入帳日若落在該
`(account, symbol)` 部位**已歸零之後**（TW／MY 除息在前、發放在後，期間賣光為常態），
則**已無成本可沖減**，其淨額改記為一列**已實現**（`RealizedRow`，`kind="dividend"`）：

$$\text{realized} = \text{proceeds\_net} = \text{net},\qquad
\text{shares\_sold} = \text{original\_removed} = \text{adjusted\_removed} = 0$$

$$\text{sell\_date} = \text{股利入帳日}$$

**判定僅看事件當下之股數**（同日序：期初 0 → 買 1 → 賣 2 → 股利 3）：

| 情形 | 入帳當下股數 | 處理 |
| --- | ---: | --- |
| 持有中配息 | > 0 | `adjusted_total −= net`（§6.1／§6.3，不變） |
| 部分賣出後配息 | > 0 | 同上，作用於剩餘部位（不變） |
| **清倉後配息** | **= 0** | **記一列已實現（`kind="dividend"`）** |
| 清倉後又買回、再配息 | > 0 | 沖減**新**部位成本（非特例） |

**不重複計算（invariant I4 維持）**：兩條路徑互斥——沖減成本或記為已實現，恰好一次。
`dividends` 帳本本身（股利總覽）與 XIRR 現金流（§7.2）本就各自計入該筆，修正後三者一致。

**稅務區隔**：`kind="dividend"` 之列**非資本利得**。年度稅務包之
`realized_gains_{year}.csv` 僅取 `kind == "sale"`；該筆股利已由 `dividends_{year}.csv`
自股利帳本輸出（`export/tax.py`），故不重複申報。

**驗證錨點**：`moomoo_my/5225` 買 200@6.00（2026-05-04）→ 賣 200@6.50（2026-05-20，部位歸零）
→ `NET` 股利 120（2026-06-16）→ 該筆進入 `realized.by_currency[MYR]`
（`scripts/stress_audit/run_phase1.py`「Found-bug op #3」；hermetic 迴歸見
`tests/portfolio/test_post_close_dividend.py`）。

### 6.4 配股（`STOCK`）與顯示用回本進度

- **配股（stock dividend，配股）**：`shares +=`（無現金、無成本變動）；`withholding = net = 0`。
- **股利只計入總報酬一次**（invariant I4）：TW/MY 現金經降成本、US DRIP 經 $0 成本股——皆各只一次；
  **無獨立股利行**（舊有重複計算陷阱）。
- **顯示用（display-only）回本進度／股利回收率**：

$$\text{payback\_ratio} = \frac{\text{cumulative cash dividends}}{\text{original\_total}} = \frac{\text{dividend\_portion}}{\text{original\_total}}$$

  （`cost_basis.py`：`dividend_portion = original_total − adjusted_total`。此為顯示指標，不進報酬分子。）

### 6.5 配息偵測與待確認匯入（inbox 估算）

實作：`api/dividend_inbox.py::detect`（**純讀、自癒**，不寫任何 pending 列）+ `confirm`（確認時**server 端重算**後才寫入帳本，client 數字僅供顯示）。偵測視窗 = 每 symbol 之最早取得日 → 今日；**除息權利判定**採「**除息日前持有**」：

$$\text{shares\_held} = \text{shares\_on}(account, symbol, \text{before}=ex\_date)\quad(\text{事件日期嚴格早於除息日者才計入})$$

（`data_ingestion/holdings.py::shares_on`：期初 + 買 − 賣 + 非現金 `reinvest_shares`，同 §4.1 重播規則。買在除息日當日**不**具權利。）每筆估算毛額：

$$\text{est\_gross} = \text{cash\_amount（每股）}\times \text{shares\_held}$$

依帳戶 `dividend_model` 分三式（確認後成為 §6 對應之帳本列）：

- **DRIP（`drip_us`）**：`est_withhold = est_gross × 0.30`、`est_net = est_gross − est_withhold`（同 §6.2）。**再投資價為估計值**：取**發放／除息日當日或之前最後一筆庫存收盤價**（`_price_on_or_before`，回看窗 14 日），`est_reinvest_shares = est_net / est_reinvest_price`。**無庫存收盤價 → 該筆不可確認（`缺再投資價`）**，須先回補歷史報價；確認後仍可於帳本編輯實際再投資價。
- **MY 現金（`cash` → `NET`）**：`est_net = est_gross`（單層淨額，無預扣，同 §6.3）。
- **TW 現金（`cash_cost_reduction` → `CASH`）**：`est_net = est_gross`（同 §6.1，降成本於重算時套用）。

**TW 配股（stock distribution，面額制）**：另立一筆 share-only 項（family = `stock`）：

$$\text{added\_shares} = \frac{\text{shares\_held}\times \text{stock\_amount（元，面額計）}}{\text{TW\_STOCK\_PAR}=10}$$

即每股配 `stock_amount / 10` 股、**$0 成本**入帳（`STOCK`，見 §6.4；`withholding = net = 0`）。此**面額 10 元換股公式**為 §6.4 配股語意之具體化，裁定 TW 配股股數時以此為準。

**抑制（去重）**：同一 (account, symbol, family) 於除息日 **±45 日**內已有同族帳本股利列，或使用者已略過（skip 指紋持久化）→ 不再出現於 inbox。

> **驗證錨點**：1,060 項壓測未涵蓋 inbox 估算純量（`detect` 為純讀投影，不寫帳本）；本節公式以 `apply_dividend_model`（DRIP 30% 已由 §6.2 之 `ledger.div.gross/net` 錨定）與 `shares_on` 為準。**配股面額換股與 DRIP 再投資價估計之驗證錨點：無（建議納入下次壓測）**。
> **實作位置**：`api/dividend_inbox.py`（`detect`、`confirm`、`_price_on_or_before`、`_TW_STOCK_PAR=10`、`_US_WITHHOLDING=0.30`、`_MATCH_WINDOW_DAYS=45`）、`data_ingestion/holdings.py::shares_on`。
> **依據**：`.claude/rules/domain-ledger.md`（Dividend models；除息權利）、`.claude/rules/markets-and-fees.md`。

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

> **公司行動不動分母（§4.4）。** `gross_invested` 只由 `opening` 與 `buy` 累加，故 SPLIT／EXCHANGE／
> SPINOFF **三式皆不觸及它**：公司行動沒有新資金進入，把它算成投入會是憑空的重複計算。分子亦然——行動
> 本身不產生已實現列（§5.1），也不改變未實現的**總額**（總額不變、股數變，均價於讀取時自行縮放）。故
> **一筆公司行動不改變 `total_return`，只改變它如何被分配到各個代號上**——這正是 §4.4.1 守恆律所斷言
> 的內容。

**已驗證彙總（reporting = TWD，spot USD/TWD = 32.5、MYR/TWD = 7.2；`phase1:final`）**

| KPI | 值（TWD） | 驗證錨點 |
| --- | ---: | --- |
| `realized_total` | 186,333.50 | `kpi.realized_total TWD`（`phase1:final`） |
| `unrealized_total` | 330,003.05 | `kpi.unrealized_total TWD`（`phase1:final`） |
| `total_return`（= 已實現 + 未實現） | **516,336.55** | `kpi.total_return TWD`（`phase1:final`） |
| `total_market_value` | 3,896,529.28 | `kpi.total_market_value TWD`（`phase1:final`） |

（交叉核對：186,333.50 + 330,003.05 = 516,336.55 ✓。）

**混合報告幣別報酬率（blended reporting rate，儀表板 KPI `total_return_rate`）**（`portfolio/dashboard.py` step 10）：

$$\text{realized\_total} = \sum_{ccy}\operatorname{convert}(\text{realized}_{ccy},\ \text{spot}),\qquad \text{unrealized\_total} = \sum_{ccy}\operatorname{convert}(\text{unrealized}_{ccy},\ \text{spot})$$

$$\text{total\_return\_rate} = \frac{\text{reporting\_total\_return}}{\displaystyle\sum_{ccy}\operatorname{convert}(\text{gross\_invested}_{ccy},\ \text{spot})}\quad(\text{混合分母；為 0 → None})$$

其中 `gross_invested`（`cost_basis.build_book` 之 `gross_invested`）= 各幣別**累計買入 all-in 原始成本**。上表 `realized_total` / `unrealized_total` 即此混合值（錨點 `kpi.realized_total` / `kpi.unrealized_total`）。

**月度快照（月度快照）**：`api/snapshots.py::write_snapshot` 每晚以**同一 combiner** 將當月 `total_market_value / total_return / total_return_rate / xirr / by_currency`（by_currency 見 §7.3 幣別視圖）**存為月末記錄**（月結時最後上升值即月末值，upsert-by-month）。快照僅**持久化**本節與 §7.3 之 KPI，**不引入新公式**；缺價／缺匯之選填 KPI 存 NULL（誠實退化）。裁定月末歷史金額時，以快照列所存值 = 當時 combiner 依本手冊公式之輸出為準。

### 7.1a 哪一個數字真的含匯兌 —— A · B · B−A（AI-D41，owner 裁示 2026-08-24）

§8.4 說「總報酬 / XIRR 已內含 FX」。這對 **XIRR** 成立（每筆流量按交易日匯率換算、終值按當前
匯率），**對 `total_return` 不成立**：

$$\text{total\_return}=\sum_{ccy}\big(\text{realized}_{ccy}+\text{unrealized}_{ccy}\big)\times \text{spot}_{\text{today}}(ccy)$$

匯率乘在**該幣別的淨損益**上；**本金**是用原幣衡量的，它自己的匯率變動從未進入這個式子。
而 §7.5 的趨勢序列 `總市值 − 累計淨投入`（每筆流量按**自己交易日**的匯率換算）才是真正含匯兌的
累計結果——它一直被標成「浮動損益」。三個數字，一組拆解：

| | 數字 | 定義 | 意義 |
| --- | --- | --- | --- |
| **A** | `total_return`<br>資產損益（不含本金匯率） | $\sum_{ccy}(\text{realized}+\text{unrealized})_{ccy}\times \text{spot}_{\text{today}}$ | 各幣別原生損益按今日匯率換算 |
| **B** | 含匯兌總損益 | $\text{total\_value}-\text{net\_invested}$（流量逐筆按交易日匯率） | 含匯兌的累計結果 |
| **B−A** | 本金匯率效果 ＋ 交易與融資成本 | 見下方 AI-D48 註 | **自 2026-08-27 起為兩項之和，不再等同換匯損益卡** |
| **本金匯率效果** | `principal_fx_effect` | $B-A-\text{交易與融資成本}$ | 即 §8 換匯損益卡的內容 |
| **交易與融資成本** | `trading_financing_cost` | $\sum \operatorname{sign}(\text{kind})\times\text{amount}\times \text{fx}_{\text{該日}}$，kind ∈ {REBATE, INTEREST_EXPENSE, BROKER_FEE} | 損益符號：券商費用為**負**（與 §7.5 `net_invested` 之符號相反） |

> **AI-D48（2026-08-27）把拆解變成三項**：B 納入了那三類資金收支而 A 從未納入，故 $B-A$ 自此
> 是**兩項之和**。若仍把它整個標為「本金匯率效果」，就是把券商費用偷偷塞進一個匯率標籤裡——
> 正是 AI-D48 要移除的那種誤標，只是換到隔壁一個欄位。因此：
>
> $$B = A + \text{本金匯率效果} + \text{交易與融資成本}$$
>
> 三項**並陳、可相加為 B**，但 **A 與 B 仍永不相加**（I5）——相加會重複計算交叉項。

**A、B、B−A 並陳呈現，永不相加。** 把 §8 的換匯損益加到 A 之上會重複計算交叉項
$(\text{MV}-\text{C})(\text{spot}-\text{acq})$——那正是 I5 禁止的事，只是換到 I5 原文沒有涵蓋的那個數字上。

> `total_return` 的**定義未變**（golden payload、oracle、所有匯出的歷史比對因此完好）；改變的是
> 它的**標籤**，以及 B 與 B−A 並列於其旁。實作：`portfolio/dashboard.py`（同時握有 `ReturnSummary`
> 與 `TrendSeries` 的那一層）。

### 7.2 XIRR（年化、資金加權、FX-aware — 決策主指標）

實作：`portfolio/returns.py::xirr_reporting`（求解器 `pyxirr.xirr`）。**單一報表幣別**；**每筆流量以其
交易日 FX 換算**，終值以**當前 spot** 換算。現金流符號：

| 流量 | 符號 | 金額（報表幣，換算後） |
| --- | :---: | --- |
| 買入 buy | **−** | `−(quantity×price + fees + tax)`，日期 = `trade_date` |
| 賣出 sell | **+** | `+(quantity×price − fees − tax)`，日期 = `trade_date` |
| 現金股利（TW `CASH` / MY `NET`） | **+** | `+net`，日期 = 股利日 |
| **DRIP / STOCK** | **中性** | 不計入（非外部現金流；再投資非 − 流出、股利非 + 流入） |
| **現金收支 — `REBATE`** | **+** | `+amount`，日期 = 現金列日期（AI-D42） |
| **現金收支 — `INTEREST_EXPENSE` / `BROKER_FEE`** | **−** | `−amount`，日期 = 現金列日期（AI-D42） |
| **現金收支 — `DEPOSIT` / `WITHDRAW` / `OPENING` / `INTEREST`** | **不計入** | 前三者是**資本移動**（納入等於把 XIRR 改成帳戶報酬）；`INTEREST` 的本金從未進入分母 |
| 期初庫存 opening | **−** | `−original_cost_total`，日期 = **`build_date`**（使期初資本被計入） |
| 期末市值 | **+** | `Σ price×shares`（各持倉），日期 = `as_of` |

**退化（all-or-nothing）**：任一持有 symbol 缺現價 → 無法形成終值 → 回 `None`（不部分退化）；無號變
（例如全為流出，無 sign change）或非有限結果亦回 `None`。

> **公司行動與 XIRR（§4.4）。** 公司行動**不是現金流**：上表沒有它的列，因為它沒有移動任何錢。它只透過
> **期末市值**那一列影響 XIRR（股數變了），這正確地反映了經濟實質。
>
> **但一筆「未能套用」的行動會使 XIRR 整個投組空白**（`unbookable_action`／`Book.unapplied_actions`，
> §5.3）。這是**刻意的爆炸半徑**：其他每一處，一筆被跳過的行動只損及一檔股票；而 XIRR 是**單一數字**、
> 其終值加總**每一檔**持股，故一個行動前的股數就讓整個加總失真。**退化必須指名該列**——原因字串帶
> 帳戶、標的、日期（錨點 `corp.xirr_reason_names_row`）——否則業主得在多帳戶的帳本裡自己找。
> 實測錨點：3 筆未套用行動 → `kpis.xirr` 為 `None`（`corp.xirr_blanked_by_unapplied`，
> `phase1:corp_refused`）。
>
> **重組費（D12）對 XIRR 不可見，是設計如此——而且沒有第二個指標會看見它（D45，2026-08-11）。**
> ⚠ **本段的前提於 2026-08-24 由 AI-D42 部分取代，但本段的結論不變。** 流量序列現在還包含三類
> **交易與融資的成本**（`REBATE`／`INTEREST_EXPENSE`／`BROKER_FEE`，見上表）；然而重組費是記為
> `WITHDRAW` 的**資本移動**，仍然不在序列中，所以「重組費對 XIRR 不可見」原樣成立。原文保留於
> §4.4.7 限制 2，連同取代的理由。⚠ 已錨定的 XIRR 數字**會**因三類新流量而移動（凡帳本存在該類
> 現金列者），oracle 與錨點須一併更新——這是 AI-D42 明白接受的代價。
> 先前本段寫著該費用「改由帳戶層 IRR 呈現」——**該指標（D36）已由業主裁定不做且從未實作**，所以那句
> 話已刪除而非改寫成「待實作」。這是一個**常設限制**，完整說明見 §4.4.7 限制 2。

> **現金收支一律不進 XIRR——包含 2026-08-13 新增的三種（業主裁決 D1 = 選項 A，2026-08-13）。**
> 現金收支自此有**七種 kind**（§9.1）；新增的 `INTEREST`（利息收入）、`INTEREST_EXPENSE`（融資利息）、
> `BROKER_FEE`（券商費用）與既有四種**同樣不是 XIRR 流量**。這不是「尚未接上」，而是一條**明訂的規則**：
>
> - **理由（D1=A 的裁定內容）**：XIRR 因此維持為一個**純投資報酬**指標，而現金餘額仍逐筆對得上券商
>   對帳單（§9.2）。被否決的選項 B（把利息／費用計入流量）會**改變本系統每一個歷史 XIRR 的意義**，
>   需要一份遷移說明；選項 C（記為相關持股的成本調整）當場否決——這些列多數**沒有 symbol**，無所可附。
> - **落實方式**：`portfolio/returns.py::xirr_reporting` 的函式簽章**根本不接收 `cash_movements`**，
>   其流量序列只由 `opening` + `transactions` + `dividends` 構成。故本規則不是靠一個過濾條件維持的，
>   而是靠**沒有那條輸入線**維持的。
> - **後果（須明說）**：一整類「真實發生、但對報酬指標不可見」的金額——重組費（D12，§4.4.7 限制 2）、
>   融資利息、券商費用、利息收入——只在**現金帳與淨值**（§9、§7.6）可見。**沒有第二個指標會看見它們**
>   （D45 已撤銷帳戶層 IRR）。
> - **沒有任何數字因此改變**：既然這三種 kind 從未進入流量序列，本手冊每一個已錨定的 XIRR 與壓測
>   oracle 的每一個期望值都留在原處。
>
> 驗證錨點：`portfolio/returns.py::xirr_reporting` 之型別簽章（無 `cash_movements` 參數）為結構性錨點；
> §4.4.7 已載明「重組費 `WITHDRAW` 不影響 XIRR」目前**無**壓測負向錨點，建議下一輪對抗性對帳一併補上
> 三種新 kind。

**流量建構範例（`schwab/TSLA`，USD 單幣，各 total 均有錨點）**

| 日期 | 事件 | 流量（USD） | 錨點 |
| --- | --- | ---: | --- |
| 2026-04-01 | 買 20@250 | −5,000.00 | `ledger.tx.total id=23`（TSLA 買，`phase1:final`） |
| 2026-04-20 | 賣 20@260 | +5,199.88 | `ledger.tx.total id=24`（TSLA 賣，`phase1:final`） |
| 2026-05-01 | 買 10@240 | −2,400.00 | `ledger.tx.total id=25`（TSLA 買，`phase1:final`） |
| `as_of` | 期末 10 股 @250 | +2,500.00 | `holding.market_value schwab|TSLA` |

XIRR 即對上述 `(dates, amounts)` 序列求使 NPV=0 之年化率 r。

> **驗證錨點（2026-07-15 常駐 harness 補上）**：XIRR **純量**已由 `scripts/stress_audit/` 之
> **獨立求解器**（Newton+bisection，不使用 `pyxirr`）錨定 — 對同一現金流序列與應用值比對，
> 全套件唯一之「文件化容差」比較 `|Δ| ≤ 1e-6`，實測差**遠在容差內**（`checkpoint1`／`final` 皆 ≪ 1e-6）
> （合併後拓樸 phase-1 實跑 **1,060/1,060** 斷言全過；`kpi.xirr` `phase1:final ≈ 0.4092`）。現金流建構規則仍以 `returns.py::xirr_reporting` 為
> 裁定準據（上表逐項可由已驗證的 `ledger.tx.total` 與 `holding.market_value` 重建）。

#### 7.2.1 觀察期下限：短於 30 天不年化（業主裁決 2026-08-05）

XIRR 為**年化**指標,其指數為 `365 / window_days`,故觀察期愈短、放大愈劇烈。以一筆帳面
`+131.7%` 的部位為例（成本 1,001,425 → 市值 2,320,000）：

| `window_days` | 年化 XIRR | 位數 |
| ---: | ---: | ---: |
| 1 | `1.5 × 10^133` | 136 |
| 14 | `325,589,627,815%` | 12 |
| **30** | `2,749,353%` | 7 |
| 90 | `2,918%` | 4 |
| 365 | `132%` | 3 |

上述數值皆為**算式的正確結果**,只是對退化輸入而言不具可讀性。故：

- **`window_days < 30` 時,`kpis.xirr` 回 `null`**,並於 `freshness.xirr_unavailable_reason`
  載明「觀察期 N 天・不足以年化（需 ≥30 天）」。邊界為**閉區間**：30 天**仍**年化,29 天不年化。
- **被withheld 的是「年化」這個動作,不是報酬本身**：`total_return_rate`（§7.1）不受影響、
  仍在 wire 上,呈現同一筆資訊的未年化形式,故 KPI 區塊不會留白。
- `xirr_window_days` **在任何情況下都照常回報**（含 rate 為 `None` 時）,30–365 天維持既有的
  「短窗參考」提示。
- **計算式本身未改**：`returns.py::xirr_reporting` 的現金流建構與求解完全不變,壓測 oracle
  的錨定值亦不受影響；本節規範的是**呈現層的揭露門檻**。
- **緣由**：2026-08-05 於全新重置的實例上發現 —— 首日輸入第一筆交易後,`window_days = 1`
  使儀表板將 136 位數之值渲染為頭條報酬率,並把版面推寬 1,915px。實作位置
  `portfolio/dashboard.py::_XIRR_MIN_WINDOW_DAYS`；回歸 `tests/contract/test_xirr_short_window.py`
  （含 30／29 兩側邊界）。

> **實作位置**：`portfolio/returns.py`（`total_return`、`xirr_reporting`）、`portfolio/results.py`
> （`ReturnSummary`、`CurrencyReturn`）、`portfolio/dashboard.py`（§7.2.1 呈現門檻）。
> **依據**：`.claude/rules/domain-ledger.md`（Total return；XIRR cashflow signs）、`.claude/rules/data-and-pricing.md`（Returns & FX P&L）。

### 7.3 配置權重、產業配置、幣別視圖與報告幣估值

**報告幣估值通則**：任一報價幣部位換入報告幣一律走

$$\operatorname{convert}(\text{market\_value}_{quote},\ \text{spot}(quote\to reporting))$$

（`market_value = price × shares`，見 §5.2；`spot` 為當前即期，經 `RateResolver`：identity → 直接對 → 反轉對 → KeyError）。缺價 → 該列 `market_value is None` 排除；缺匯 → `weight = None`，**永不臆造**。

**單一持股權重（holding weight）**（`portfolio/dashboard.py` step 8）：

$$\text{weight}_h = \frac{\operatorname{convert}(\text{market\_value}_h,\ \text{spot})}{\text{total\_market\_value}}\quad(\text{total 為 §7.1 報告幣總市值；total}=0\text{ 或缺 → None})$$

此權重驅動 `single_weight` 警示與再平衡 §11。

**產業配置（sector allocation）**（`portfolio/allocation.py::sector_allocation`；市場別配置 `market_view.py::market_allocation` 同式）：

$$\text{sector\_value}_s = \sum_{h\in s}\operatorname{convert}(\text{market\_value}_h,\ \text{spot}),\qquad \text{sector\_weight}_s = \frac{\text{sector\_value}_s}{\sum_s \text{sector\_value}_s}$$

產業別由 registry `instruments.sector` 決定；stale（缺價）持倉略過。

**幣別視圖（combined view）**（`portfolio/allocation.py::combined_view`）：

$$\text{by\_currency\_value}[ccy] = \sum_{h:\ quote=ccy}\text{market\_value}_h\ (\text{原幣，不換算}),\qquad \text{reporting\_total\_value} = \sum_h \operatorname{convert}(\text{market\_value}_h,\ \text{spot})$$

`reporting_total_value` 即 §7.1 之 `total_market_value`；`by_currency_value` 為**各報價幣原生市值**（月度快照之 `by_currency` 即存此，見 §7.1）。

**匯出層之報告幣值與合計列**：匯出報表（`export/holdings*.py`、`ledgers_report.py`、`tax.py`、`rebalance_report.py`）之「報告幣值」欄同走上式 `convert(...)`；其 **TOTAL／小計列**為對應欄之**逐幣別加總**（如 `Σ 淨額`、`Σ original_cost_total`、`Σ 市值`、`Σ dividends.net`、`Σ fx from/to`），**不引入新公式**（逐項見 §12.5）。**唯一例外**——**稅務報表之已實現以「賣出日匯率」換算**（`export/tax.py`）：

$$\text{reporting\_realized} = \text{realized}\times\text{rate}(quote\to reporting\ \text{於賣出日})$$

（**非**當前 spot；供落地稅務用，與 §7.1 以 spot 換算之總報酬視角**不同**，裁定稅務金額時務必分辨。）

> **驗證錨點**：權重／產業／幣別視圖**無壓測純量錨點**（`weight`/`alloc`/`sector`/`by_currency` 斷言計數 = 0，**建議納入下次壓測**）；`convert` 通則已於 §7.1 與 §8 之 rollup 間接驗證；匯出 `original_cost_total` / `adjusted_cost_total` / `shares` 之合計以 `export.holdings.*`（各 20 項）驗證。
> **仲裁邊界註記**：權重／配置為「金額之比值」；本手冊沿用 §11.2 之既例將其**納入仲裁範圍**（附公式）。owner 已於 **2026-07-15 裁定此為定案**：權重／報酬率**維持於仲裁範圍內**、現行作法即為標準——見 §12.5 之邊界說明。
> **實作位置**：`portfolio/allocation.py`（`sector_allocation`、`combined_view`）、`portfolio/market_view.py::market_allocation`、`portfolio/dashboard.py`（holding `weight`、step 10 blends）、`export/holdings.py`、`export/holdings_report.py`、`export/ledgers_report.py`、`export/tax.py`。
> **依據**：`.claude/rules/domain-ledger.md`、`CLAUDE.md`（module map：portfolio 算配置，web 不算）。

### 7.4 股利收入彙總與年度預估

**股利收入彙總（display-only）**（`portfolio/dashboard.py` step 6）：**逐幣別、逐年**加總已入帳股利淨額，**排除配股 `STOCK`**、**含 DRIP 淨額**：

$$\text{dividend\_total}[ccy] = \sum_{d:\ type\ne STOCK}\text{net}_d,\qquad \text{by\_year}[y][ccy] = \sum_{\substack{d:\ year=y\\ type\ne STOCK}}\text{net}_d$$

**幣別永不跨幣相加**。此為**顯示用股利統計**（含 DRIP 再投資淨額作為「已宣告收入」），**與總報酬分離**：股利已於 §5／§6 折入成本（TW/MY）或化為 $0 成本股（US DRIP）各計一次（invariant I4），此統計**不得**再加入總報酬（否則重複計算）；亦與 §6.4 之 `payback_ratio`（僅**現金**股利、單一部位）定義不同。

**年度股利預估（declared-only projection）**（`portfolio/dividends.py::project_dividends`）：對當年度、持有中 symbol 之除息事件（`ex_date.year == year` 且有現金金額）：

$$\text{declared\_gross}[ccy] = \sum \text{shares}_h \times \text{cash\_amount}_{ev},\qquad \text{declared\_net}[ccy] = \sum \text{apply\_dividend\_model}(model_h,\ gross).\text{net}$$

淨額**僅套用預扣**（DRIP 30%；Moomoo-US 每筆平台費屬 probe-pending，暫不計）；幣別由事件幣（fallback 報價幣）鍵定，**永不跨幣相加**；未知 `account_id` → fail-loud（`KeyError`）。

> **驗證錨點：無**（`dividend_summary` / `projection` 無壓測斷言，**建議納入下次壓測**）；其成分 `dividends.net`（`ledger.div.net`，15 項）與 §6 之 DRIP 30% 已驗證。
> **實作位置**：`portfolio/dashboard.py`（step 6 股利彙總）、`portfolio/dividends.py::project_dividends`、`data_ingestion/dividend_model.py::apply_dividend_model`。
> **依據**：`.claude/rules/domain-ledger.md`（Dividend models；no double counting）。

### 7.5 淨值與累計投入趨勢（daily replay）

實作：`portfolio/timeseries.py::daily_value_series`（純函式，combiner 預載價／匯歷史）。自首筆帳本事件日至 `as_of` **逐日重播**，每日兩序列（報告幣）：

- **市值 `total_value`**：$\displaystyle\sum_{h:\ shares>0}\operatorname{convert}(\text{price}_{\le day}\times \text{shares}_h,\ \text{fx}_{\le day})$，價與匯採**當日或之前最後值（carry-forward）**。任一持倉當日**全無報價**或**賣超（負股）**→ 該日標 `incomplete`（**不臆造**、不貢獻市值）。
- **累計淨投入 `net_invested`**：截至當日之流量累加，**流量符號與 XIRR 相反（§7.2 之負號）**：期初 `+original_cost_total`、買入 `+(qty×price+fees+tax)`、賣出 `−(qty×price−fees−tax)`、現金股利（CASH/NET）`−net`；DRIP/STOCK 中性。**自 AI-D48（2026-08-27）起另納三類資金收支**：`REBATE`／`INTEREST_EXPENSE`／`BROKER_FEE`，即 §7.2 XIRR 所納之同一組「交易與融資成本」，金額為 $-\operatorname{sign}(\text{kind})\times \text{amount}$（`sign` 取自 `shared/cash_kinds.py` 之 `credit`，再依本序列之相反符號取負）——故**券商費用／融資利息提高** `net_invested`（與買入手續費一貫），**退佣降低**之。`DEPOSIT`／`WITHDRAW`／`OPENING`（資本移動）與 `INTEREST`（閒置現金利息）**不納**，理由與 §7.2 逐字相同。每筆流量以**其日期之 carry-forward 匯率**換算。

  > **為何 AI-D42 之後還要這一條**：A 與 B **並排印在同一條 KPI 帶上**（§7.1a），其差額標示為「本金匯率效果」。AI-D42 只把 XIRR 搬上那三類，於是自 2026-08-24 起該標示不再為真——差額實為「本金匯率效果 ＋ 三類資金收支」。R4 基準對照之 `excess` 又以 B 為基準，故一筆 B 看不見的券商費會被讀成「贏過大盤」。
  >
  > **AI-D49 — 基準那一腿不吃這三類**：資金收支流量之 `market` 為 `None`，`benchmark_counterfactual.counterfactual()` 將其排除於**兩腿之外**，亦**不計入** `uncovered_ratio`（該比率之語意是「有多少錢找不到基準」，非「有多少錢本來就不該買指數」）。結果：交易與融資成本記在組合這一腿，指數那一腿不代付——與「同一筆錢、同樣的日期，買指數會是多少」之字面一致。

任一流量日期無「當日或之前」匯率 → 整條序列 `available = False`（與 §7.2 XIRR 之 all-or-nothing 一致）。**資金收支流量同受此規則**：一筆缺匯率之外幣券商費會使整條趨勢誠實缺席，而非被靜默略過。

> **驗證錨點**（2026-08-27 起，AI-D51）：壓測 phase 1 之 `trend.*` 族——`trend.net_invested` 於**每個帳本事件日及其前一日**（股利貢獻**除息日**與**發放日**兩者）＋`ASOF`＋期末，與 `oracle.net_invested_through` **精確**比對（與價格無關）。同族另含 `trend.start`／`trend.contiguous_days`／`trend.incomplete`，以及 `trend.total_value`（僅於 oracle 判定當日可估值時）。⚠ **仍無比對「某一天的逐標的股數」**——無任何 app 介面回答該問題，詳見 `scripts/stress_audit/README.md` §6。其成分（`price × shares`、all-in 買入成本、賣出淨額、股利淨額、`convert`）另於 §4／§5／§7 驗證。
> **實作位置**：`portfolio/timeseries.py`（`daily_value_series`、`_at_or_before`、`_fx_at`）、`portfolio/dashboard.py`（step 9 預載歷史）。
> **依據**：`.claude/rules/domain-ledger.md`（XIRR 流量符號；carry-forward valuation）、`.claude/rules/data-and-pricing.md`。

### 7.6 總淨值（含現金）（FU-D29 / deferred C8）

實作：`portfolio/networth.py`（純函式組合層，`portfolio/dashboard.py` step 9b 呼叫）。**顯示／歸因用途，非記錄金額（money-of-record）**，不進入任何報酬指標；在**不修改** §7.5 `daily_value_series` 的前提下，於其上疊加一條每日現金序列後合成（報告幣）：

$$\text{net\_worth}_t \;=\; \underbrace{\textstyle\sum_{h:\ shares>0}\operatorname{convert}(\text{price}_{\le t}\times\text{shares}_h,\ \text{fx}_{\le t})}_{\text{市值 } total\_value_t\ (\S7.5)} \;+\; \underbrace{\textstyle\sum_{p\in pools}\operatorname{convert}(\text{balance}_{p,\le t},\ \text{fx}_{p,\le t})}_{\text{當日現金 } cash_t}$$

- **每日現金 `cash_t`**：對每個 `(account, ccy)` 池，取其**逐日流量（`pool_lines`：movements ± fx legs ± 交割 ± 現金股利）當日或之前最後 running balance（carry-forward）**，以**當日或之前最後匯率**換算報告幣後跨池加總。**未註冊標的之列自動略過**（與 `cash_balances` 一致，不污染序列）。
- **合成 `compose_net_worth`**：沿 §7.5 之日期軸對齊（首筆現金流量前 = 0），**僅新增 `net_worth` 欄，其餘 `TrendPoint` 欄位逐位元不變**（單元測試守護）。
- **incomplete 規則（比照 §7.5）**：某日若有**非零**池無「當日或之前」匯率 → 該日 `cash_t` 標 incomplete，`compose_net_worth` 令 `net_worth = None`（前端畫斷點，**不臆造**）；**零餘額池缺匯率不污染該日**。持倉缺價之日（§7.5 之 incomplete）`net_worth` 仍為部分值（與市值線一致，靠共用標記提示）。
- **一致性錨點（invariant）**：末個現金完整日之 `cash_t` **等於** `cash_balances` 導出、`GET /api/cash` 提供之報告幣現金總額（同一 fixture 雙路徑逐位元相等）。**換匯不加疊**：本序列已把現金各幣別於當日匯率換算合計，非在市值上另計換匯損益（§8.4 invariant I5）。

> **驗證錨點**：`tests/portfolio/test_networth.py`（逐日 carry-forward、換匯兩腿、缺匯 incomplete、零池不污染、負池不 floor、合成不動既有欄）＋ `tests/contract/test_networth_dashboard.py`（跨端點一致）＋ golden 追加（**僅 `net_worth`**）。
> **實作位置**：`portfolio/networth.py`（`daily_cash_series`、`compose_net_worth`、`CashDay`）、`portfolio/dashboard.py`（step 9b）、`portfolio/dashboard_models.py`（`TrendPoint.net_worth` 追加欄）。
> **依據**：`.claude/rules/domain-ledger.md`（現金池；FX 拆解不加疊）、`.claude/rules/data-and-pricing.md`（Decimal；carry-forward）。

---

## 8. 換匯損益（FX P&L）

**專用帳本** `fx_conversions` 記錄**每一筆實際換匯**：`date, account_id, from_ccy, from_amount, to_ccy,
to_amount` → 隱含匯率 `implied_rate = from_amount / to_amount`（**本位幣 per 1 單位外幣**；例 `id=1` TWD
320,000→USD 10,000 → 320,000/10,000 = **32**，錨點 `ledger.fx.implied id=1`）。每個外幣 pool（per account）帶一個
**本位幣（home = 帳戶 `funding_ccy`）成本基礎 = 加權平均取得匯率**。Schwab USD pool 錨定 **TWD**；`moomoo_my`
之 USD pool 錨定 **MYR**。

### 8.1 加權平均取得匯率（home per foreign）

實作：`forex/pools.py::average_acquisition_rate` / `acquisition_basis`。取得來源有**兩類**
（spec 2026-07-30）：`home → foreign` 換匯，以及**帶有 `acq_home_amount` 的外幣現金流入**
（`cash_movements` 之 `DEPOSIT`／`OPENING`／`REBATE` 三種**取得型** kind；其餘四種皆不入取得，
判定式為 `shared/cash_kinds.py::is_fx_acquisition`，完整兩軸表見 §9.1）：

$$\text{avg\_rate} = \frac{\sum \text{from\_amount}\ (\text{home}) + \sum \text{acq\_home\_amount}}{\sum \text{to\_amount}\ (\text{foreign}) + \sum \text{amount}_{\text{有成本}}}\quad(\text{無任何有成本取得則 None})$$

**存金額，不存匯率。** `acq_home_amount` 是**家幣金額**：匯率是平均值，而 §1.3 明訂平均值不得作為
權威（`fx_conversions` 同樣存兩個金額）；顯示用取得匯率一律 `acq_home_amount / amount` 讀取時計算。

**覆蓋率（covered_ratio）。** 外幣流入若**無**成本基礎，其匯率未知且**永不臆測**。現金為可替代物、
加權平均不追蹤批次，故流出按**比例**分攤：

$$\text{covered\_ratio} = \frac{\sum \text{amount}_{\text{有成本}}}{\sum \text{amount}_{\text{有成本}} + \sum \text{amount}_{\text{無成本}}}\quad(\text{無「無成本」項時恆為字面 } 1)$$

**不可**用「總餘額 − 無成本額」：該式在餘額跌破無成本額時**變負**，等同重新製造反號假數字。
覆蓋率恆為 1 時呼叫端**跳過乘法**，故完整覆蓋之帳本與本規格前之引擎**逐位元相同**。
錨點：`fx.covered_ratio scope = schwab / moomoo_my`（phase1 均 **1**）、`fx.basis_gap`（均 **0**）。

**分母只收「取得」，不收「池內孳生」（2026-08-13，第二軸）。** 分母的成員資格由
`shared/cash_kinds.py` 的 **`fx_acquisition` 軸**單獨決定，**不是**「非借方」的同義詞：

- **`INTEREST`（利息收入）是一筆 credit，卻不是取得。** 它是**在外幣池「裡面」孳生的收益**，與
  `domain-ledger.md` 已載明的兩種流入同型——「Sale proceeds and foreign cash dividends are not
  unbased acquisitions; they keep inheriting the pool average」——故它**沿用池的加權平均匯率**，
  既不入分子也不入分母。反之若把它當成無成本取得，一個**從未換過任何一筆匯**、只是收到利息的
  USD 帳戶就會回報 `covered_ratio < 1`，並依 F3 把**現金與股票整個外幣曝險**標為基礎不完整——
  **在每一個真實帳戶上都響的假警報，比不響更糟**。
- **借方永遠不是取得**（`WITHDRAW`／`INTEREST_EXPENSE`／`BROKER_FEE`）：處分既不改變均價也不改變
  覆蓋率（N1）。舊的「`kind == "WITHDRAW"` 才是借方」述詞會讓一筆券商費用以**無成本取得**的身分進入
  分母，把 `covered_ratio` 往下拉。
- **因此輸入端拒收「利息／費用的取得成本」**：`data_ingestion/validate.py::resolve_acq_home_amount`
  在非取得型 kind 上回 `acq_cost_not_an_acquisition`（「利息與費用不是外幣取得，不帶取得成本（沿用
  資金池平均匯率）」）。這個檢查刻意鍵在**取得軸**而非 `== "WITHDRAW"`：`INTEREST` 是 credit，
  只查 `WITHDRAW` 會讓成本填得進來，而 `forex/pools.py` 隨後**忽略**它——一個被要求、被儲存、卻
  不影響任何數字的欄位，比一個拒絕更糟。

> **驗證錨點（兩軸；hermetic）**：`tests/shared/test_cash_kinds.py::test_interest_does_not_dilute_the_coverage_ratio`
> （DEPOSIT 1,000（成本 32,000）+ INTEREST 7 → `covered_ratio = 1`、`acquired_without_basis = 0`）、
> `::test_a_fee_is_not_an_acquisition`（+ BROKER_FEE 100 → `acquired_with_basis = 1,000`、
> `acquired_without_basis = 0`）、`::test_an_unbased_deposit_still_dilutes_the_coverage`（F2 未被破壞：
> 兩筆 1,000、其一無成本 → `covered_ratio = 0.5`）、`::test_foreign_cash_balance_signs_a_fee_as_a_debit`
> （`forex/pools.py::foreign_cash_balance` 對 BROKER_FEE 100 回 **−100**）。
> CSV 門的拒收由 `tests/data_ingestion/test_cash_import.py` 之 `acq_cost_not_an_acquisition` 三列
> （`INTEREST`／`BROKER_FEE`／`INTEREST_EXPENSE`）錨定。
> **獨立重算**：壓測 oracle 另行寫死一份自己的 `ACQUIRING_KINDS = {DEPOSIT, OPENING, REBATE}`
> （`scripts/stress_audit/oracle.py`，**刻意不 import 應用程式的表**——共用同一張表的 oracle 不可能
> 與它相左，而相左正是它的工作）。**phase-1 場景已於 2026-08-13 登錄此三種 kind**（`schwab` 之 USD 池，
> ops 50–52），故第二軸現有 `scope` 錨點：`fx.covered_ratio scope = schwab` 在收到 `INTEREST` 之後
> **仍恰為 1**——本節主張的正是這一點，而它現在有實跑證據，不只是單元測試。

**已驗證範例**

| 帳戶 | home→foreign 換匯 | avg_rate | 錨點 |
| --- | --- | ---: | --- |
| `schwab` | TWD 320,000→USD 10,000（32.0）；TWD 2,310,000→USD 70,000（33.0） | (320,000+2,310,000)/(10,000+70,000) = **32.875** | `fx.avg_rate schwab` |
| `moomoo_my`（USD pool，錨定 MYR） | MYR 44,000→USD 10,000（4.4）；MYR 46,000→USD 10,000（4.6） | 90,000/20,000 = **4.5** | `fx.avg_rate moomoo_my` |

### 8.2 已實現換匯損益（回換 foreign→home 時）

實作：`forex/fx_pnl.py::realized_fx_rows`。對每筆 `foreign → home` 回換：

$$\text{realized\_fx} = \text{home\_received} - \text{foreign\_sold}\times\text{avg\_rate}$$

（刻意**不**走 `shared.fx.convert`，因 `avg_rate` 是**衍生 pool 匯率**，非市場 spot。）`avg_rate = None`（無成本
基礎）→ 回 `None`；有基礎但無回換 → 0。
**已驗證範例（`phase1:final`）**：合併後場景含一筆 Schwab USD→TWD 回換（USD 5,000 → TWD 162,000，隱含匯率
32.4，2026-06-20）。回換前 Schwab USD pool `avg_rate = 32.875`（見 §8.1），故
`realized_fx = 162,000 − 5,000 × 32.875 = −2,375.00 TWD`（以 32.4 回換、低於取得均價 32.875 → 換匯損失）。
`moomoo_my` 於本場景無 foreign→home 回換 → `realized_fx = 0`。
錨點：`fx.realized schwab = −2,375.000`、`fx.realized moomoo_my = 0`、`fx.reporting_realized rollup = −2,375.000`
（均 `phase1:final`；於 `checkpoint1`／`checkpoint2` 尚無回換，故當時 `= 0`——場景依 phase 演進）。

### 8.3 未實現換匯損益（剩餘外幣曝險 mark-to-spot）

實作：`forex/fx_pnl.py::compute_account_fx`。令 `spot = 當前 foreign→home` 匯率：

$$\text{unreal\_stocks} = \text{foreign\_stock\_value}\times\text{covered\_ratio}\times(\text{spot} - \text{avg\_rate})$$

$$\text{unreal\_cash} = \text{foreign\_cash}\times\text{covered\_ratio}\times(\text{spot} - \text{avg\_rate})$$

**同一個比率套在整個外幣曝險上（現金與股票兩條腿）**：`avg_rate` 本身即出自「有成本」母體，
只縮放現金腿會讓**誤差較大的股票腿**不被標示（實測差 +42,359 TWD）。`covered_ratio < 1` 時
`fx_basis_incomplete = true`、`fx_basis_gap = foreign_cash × (1 − covered_ratio)`，兩條腿一併標示；
已實現匯損益不縮放（價款是真實收到的金額），但其成本側用的是同一個不完整均價，故一併標示。

其中 `foreign_cash` 為 **FX 曝險視角**之外幣餘額。自 spec 2026-07-30 起它**亦計入外幣現金流入／流出**，
因此對同一 (account, foreign ccy) **恆等於 §9 之營運現金池**（先前兩者刻意分歧，見 audit C9）；
差異僅存於**成本基礎**：無 `acq_home_amount` 之流入計入餘額但不入加權平均。
`avg_rate is None` 或 `spot is None` → unrealized = `None`。
錨點：`fx.foreign_cash scope = schwab`（**25,800**）／`moomoo_my`（**−11,244.14**）、
`fx.pool_equals_funds`（phase2）。

**已驗證範例（`phase1:final`；spot USD/TWD = 32.5、USD/MYR = 4.6、MYR/TWD = 7.2）**

兩帳戶各以「其剩餘外幣曝險 ×（spot − avg_rate）」計，rollup 換入報告幣（TWD）：

- **Schwab（home = TWD）**：`avg_rate = 32.875`、`spot(USD→TWD) = 32.5` → `spot − avg = −0.375`（USD 貶 → 換匯損失，
  對 Schwab 之 USD 曝險貢獻**負值**）。
- **`moomoo_my`（USD pool，home = MYR）**：`avg_rate = 4.5`、`spot(USD→MYR) = 4.6` → `spot − avg = +0.10`
  （USD 對 MYR 升 → 換匯利得，貢獻**正值**；此與 v1.3-basis run 之「差 0」不同——現 spot 已移動至 4.6），其 MYR 值再經
  `MYR→TWD` 換入報告幣。

兩腿合成後：reporting（TWD）rollup 未實現 FX = **−11,757.483… TWD**。錨點：`fx.reporting_unrealized rollup`
（`phase1:final`）。（各帳戶之外幣曝險分量（FX 視角現金 + 股票市值）隨場景變動且無單一斷言錨點，故本版僅釘選已錨定之
rollup 與可查證之 avg_rate／spot；逐帳戶曝險分解以公式重播為準。）

### 8.4 CRITICAL — 換匯損益是「拆解」，永不加疊（invariant I5）

報表幣別總報酬 / XIRR **已內含** FX（流量按交易日匯率換算、終值按當前匯率）。**換匯損益是該數字的
attribution 拆解（資產損益 vs 換匯損益），絕不是另外加在總報酬之上的一筆額外收益**。任何把
`reporting_unrealized_fx`（如上例 −11,757.48）再加到 `total_return`（§7）之上的做法，都是**重複計算**，屬 bug。

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
| 現金收支 cash movement（**七種 kind**，見下方兩軸表） | **± amount**（正負號由 kind 決定） |
| 換匯 fx（兩腿） | `from_ccy`：**− from_amount**；`to_ccy`：**+ to_amount** |
| 買入 buy | **− (quantity×price + fees + tax)**（all-in debit，記於 `quote_ccy` 池） |
| 賣出 sell | **+ (quantity×price − fees − tax)**（淨額 credit） |
| 現金股利（`CASH` / `NET`） | **+ net**（credit） |
| **DRIP / STOCK** | **0**（股票事件，不動現金） |

> **期初庫存 `opening_inventory` 刻意不動現金池**（其資金早於追蹤起點）。若要現金池從第一天起平衡，
> 需另記一筆 `deposit` 或 `opening`（**期初資金**）現金移動。注意：`opening_inventory`（庫存）與 cash
> movement 的 `opening`（期初資金）是**兩個不同概念**。

`symbol` 未註冊之列會被跳過（與儀表板同一退化規則），不使現金視圖崩潰。

#### 9.1.1 現金收支的七種 kind — 一張表、**兩條正交軸**（2026-08-13）

**金額一律以無號值儲存，方向由 kind 決定。** 自 2026-08-13 起共**七種**，由
`shared/cash_kinds.py` 這**唯一一張表**規範；`portfolio/cash.py::_movement_sign`（現金池）與
`forex/pools.py::_is_debit`／`acquisition_basis`（換匯損益）兩個**互不 import** 的計算模組都讀它。

| kind | 中文標籤 | `credit`（是否**增加**池餘額） | `fx_acquisition`（是否進 **`covered_ratio` 分母**，§8.1） | delta |
| --- | --- | :---: | :---: | --- |
| `DEPOSIT` | 入金 | ✔ | ✔ | **+ amount** |
| `OPENING` | 期初（期初資金） | ✔ | ✔ | **+ amount** |
| `REBATE` | 折讓款 | ✔ | ✔ | **+ amount** |
| `WITHDRAW` | 出金 | ✘ | ✘ | **− amount** |
| **`INTEREST`** | 利息（收入） | ✔ | **✘** | **+ amount** |
| **`INTEREST_EXPENSE`** | 融資利息 | ✘ | ✘ | **− amount** |
| **`BROKER_FEE`** | 券商費用 | ✘ | ✘ | **− amount** |

**為什麼是兩軸，而不是一個布林值。** 舊述詞是「`kind == "WITHDRAW"` 才是借方，其餘皆為 credit
且皆為取得」——在每一個非提領 kind 都是取得型 credit 的期間裡它是對的，並且會在這件事不再為真的
那一刻**靜默失敗**。以舊述詞加入 `BROKER_FEE`，一筆券商費用會**增加**現金餘額，**又**以無成本取得
的身分把 `covered_ratio` 拉低——**兩個錯誤的金額之記錄，而且都不會拋錯**。

- **`INTEREST` 就是證明一個布林值不夠的那一列**：一筆 **credit，卻不是取得**（理由與假警報的代價
  見 §8.1）。
- **反向的組合不存在**：借方永遠不是取得（N1），故表中沒有 `credit=✘, fx_acquisition=✔` 的列。
- **`REBATE` 維持為取得型**：這是它自 spec 2026-07-30 以來的行為，在此改動它會**靜默移動既有帳本的
  `covered_ratio`**——一個沒有裁決在背後的金額之記錄變更。
- **三種新 kind 而非兩種**：把融資利息併入 `BROKER_FEE` 在算術上正確，卻會讓**融資利息在每一個畫面
  上顯示為「券商費用」**。
- **註冊有兩處**：`shared/cash_kinds.py` 的表，與 `data_ingestion/validate.py::CASH_MOVEMENT_KINDS`
  （寫入路徑的可接受集合，`= CASH_KIND_VALUES`）。兩者由測試斷言相等，故「只註冊一半」是測試失敗，
  而不是一個**正負號被靜默寫錯**的池。

> **驗證錨點（hermetic）**：`tests/shared/test_cash_kinds.py`——`::test_every_kind_has_a_spec`、
> `::test_two_axes_are_independent`（逐 kind 參數化，即上表）、`::test_a_debit_is_never_an_acquisition`、
> `::test_debit_kinds_set_matches_the_predicate`、`::test_registration_points_agree`（兩處註冊點相等）、
> `::test_broker_fee_reduces_the_cash_balance`（BROKER_FEE 100 → 池 **−100**；舊述詞下為 **+100**）、
> `::test_margin_interest_reduces_the_cash_balance`（INTEREST_EXPENSE 40 → **−40**）、
> `::test_interest_earned_increases_the_cash_balance`（INTEREST 7 → **+7**）。
> CSV 門之拒收：`tests/data_ingestion/test_cash_import.py` 之三列 `acq_cost_not_an_acquisition`（見 §8.1）。
> **已知測試缺口（就地清點 2026-08-13）**：`cash_import.py` 的別名表已收錄五個新 zh 標籤
> （`利息`／`利息收入`／`融資利息`／`利息支出`／`券商費用`），但
> `::test_zh_kind_labels_are_accepted` **只涵蓋 `入金`／`出金`／`期初資金`／`折讓款` 四個舊標籤**——
> 新別名目前**無任何測試**，建議補上。
> **壓測 `scope` 錨點**（2026-08-13 加入）：三筆皆記於 `schwab` 的 **USD** 池（唯一能觀察到第二軸的
> 位置，因該池背後有兩筆換匯）——`cash.balance scope = schwab|USD` 與
> `fx.covered_ratio scope = schwab`。oracle 的 `DEBIT_KINDS`／`ACQUIRING_KINDS`
> （`scripts/stress_audit/oracle.py`）是**獨立於 app 的表**另寫的，故此處的一致並非同一份定義自我確認。
> **不進報酬指標**：七種 kind **全部**不是 XIRR 流量（業主裁決 **D1 = A**，2026-08-13；見 §7.2）。

### 9.2 對帳單（running-balance statement）與同日排序

實作：`pool_lines` → `_ordered` → `running_statement` / `running_min`。**同日排序：credit 先於 debit**
（`key = (date, 0 if delta≥0 else 1)`），使同日入金能覆蓋同日支出，不虛假地瞬間為負。
`running_statement` 回傳每列 + 其後的**逐列 running balance**；`running_min` 回傳**期間內最小 running
balance**（空池為 0）。

**已驗證期末餘額（reporting = TWD；`phase1:final`）**

| 池 | 期末餘額 | 錨點 |
| --- | ---: | --- |
| `tw_broker` / TWD | 1,089,099 | `cash.balance` / `cash.statement.terminal tw_broker|TWD` |
| `schwab` / USD | 18,159.42 | `cash.balance schwab|USD` |
| `schwab` / TWD | 532,000 | `cash.balance schwab|TWD` |
| `moomoo_my` / USD | 829.95 | `cash.balance moomoo_my|USD` |
| `moomoo_my` / MYR | **123,201.91** | `cash.balance moomoo_my|MYR` |

（`cash.balance` 與 `cash.statement.terminal` 兩組錨點期末一致，證明彙總視圖與逐列對帳單收斂於同一值。）

> **Batch B 合併之 MYR 池（重要）**：現金池以 `(account_id, ccy)` 為鍵（`portfolio/cash.py`），故合併後
> `moomoo_my` 之 MYR 曝險為**單一 `(moomoo_my, MYR)` 操作池**。合併後拓樸之壓測套件已就此單一池直接錨定
> `cash.balance moomoo_my|MYR = 123,201.91`（`phase1:final`；US 市場 leg 之 MYR 供資與 MY 市場 leg 之 MYR
> 現在同屬此池，per-ccy 守恆由 `data_ingestion/moomoo_merge.py` 之 in-span self-check 保證）。**先前 v1.3-basis
> 版本以兩個 legacy 池之和推導此值；本版已改採當前實跑直接錨定之單一池終值**（未動任何公式，§9.1 餘額式不變）。

### 9.3 負池語意與護欄（date-aware guard）

**負池通常代表漏記入金或換匯**。護欄分三類——**寫入門（硬拒、不可 ack）**、**更正門（可 `ack_negative`）**、
**交易門（軟警告）**。出金／換匯之硬護欄為 owner 裁定 **FU-D43a**／**FU-D34**（載於 `api/routers/cash.py`
檔頭與 `data_ingestion/fx_import.py` docstring；本節 2026-08-29 同步為實作語意，見 §12.3 `v1.9`）：

- **出金門之硬護欄（FU-D43a）——不可 ack**：一筆 `WITHDRAW` **永不可**使其池透支。兩道檢查**並行**
  （`validate.py::_withdraw_issues`——手動表單、編輯門、CSV 匯入門共用同一守門）：①（期末餘額）
  `amount` 超過該池**當前餘額**（與賬戶現金線同一 `cash_balances` 數字；恰等於餘額者可過）；
  ②（date-aware，audit C3）該筆會**新引入或加深**時間線上低於零之 dip（如回填出金早於其資金到位）。
  兩者皆答 **422 `withdraw_insufficient_balance`**——獨立錯誤碼，**`ack_negative` 不可繞過**，前端據此
  不提供 ack。**批次感知**：CSV 同檔為一批、時間線按日期排序（同檔入金可支應同檔出金；兩筆僅「聯合」
  透支之出金**皆**被攔）；編輯門先剔除舊列自身效果（self-exclusion）再檢。
- **換匯門之硬護欄（FU-D34，需求五）——不可 ack、不提供融資**：三個門（`POST /api/cash/fx`、
  `PUT /api/ledgers/fx/{id}`、CSV 匯入）跑**同一** `fx_ccy_issues`／`fx_balance_issues`（住在
  `data_ingestion/fx_import.py`，pool 算術由上層注入）：① `from_amount` 超過 from-pool 當前餘額；
  ②（date-aware）該筆會新引入或加深 dip（換匯日早於資金到位）。兩者皆答 **422
  `fx_insufficient_balance`**，無任何 ack。**批次感知（CSV）**：批 = **實際會被寫入**之列（結構有效
  **且**被勾選）——被拒或被剔除之列不得資助 sibling；同檔先換入之幣仍可支應後續換出（E1a 保留）。
- **既有 dip 規則（兩硬護欄之 date-aware 分支共通）**：判準為 `after.low < min(before.low, 0)`——
  一筆**不加深**既有 dip 之列**永不**被該 dip 阻擋，已在赤字之帳本仍可更正。
- **更正門之 `negative_cash`＋`ack_negative`——僅存於此**：**縮減資金面**之編輯／刪除（PUT／DELETE
  現金收支、DELETE 換匯）以 **date-aware `running_min`** 檢查更正後帳本，**所有受影響池**（編輯之舊＋新
  (account, ccy)；換匯刪除之**兩腿**）任一於**某時點**降至負 → **422 `negative_cash`**
  （`此筆會使 … 現金於某時點降至 … — 通常代表漏記入金或換匯;確認無誤可強制寫入`），`ack_negative`
  可強制寫入——這是**更正門**，負池是待修的資料問題而非可執行的禁令。編輯中**新出金本身**仍受 FU-D43a
  硬擋、永不降格為可 ack 警告；可 ack 的只有「移除舊列效果」那一半（deposit/opening-side 語意）。
- **交易門之軟警告（soft）**：`api/routers/input_center.py::_cash_overdraft_issue` — **僅當**帳戶已啟用現金
  追蹤（≥1 筆 cash movement）**且**該筆買入會使該標的現金池 < 0 時，附一則**警告 issue（永不硬阻擋）**。
  未追蹤現金的帳戶不會觸發。

**護欄只看 `WITHDRAW`，且刻意不隨新 kind 擴張（2026-08-13）。** §9.1.1 把 `INTEREST_EXPENSE` 與
`BROKER_FEE` 列為**借方**，但兩者**不進** `running_min` 提領護欄（audit C3），也不觸發 N1 的外幣提領
提示——兩者仍**只**鍵在 `WITHDRAW`：

- **一筆提領是使用者的「意圖」**，在它被寫入之前擋下一個會透支的意圖，攔的是資料輸入錯誤；
- **一筆費用或融資利息是「已發生的事實之記錄」**，而**融資帳戶本來就會有負的現金餘額——那正是融資
  的定義**。擋下它等於拒絕記錄對帳單上確實發生的事。
- 據此，credit 型的四種（`DEPOSIT`／`OPENING`／`REBATE`／`INTEREST`）進池時**不做任何餘額檢查**
  （`validate.py`：`kind != "WITHDRAW"` 即直接回空 issue 清單）。

驗證錨點——出金側：`tests/data_ingestion/test_cash_import.py::test_credits_need_no_balance_guard`、
`::test_withdraw_over_balance_is_hard_and_writes_nothing`、
`::test_backdated_withdraw_before_its_funding_is_blocked`（date-aware）、
`::test_two_withdrawals_that_only_jointly_overdraft_are_both_caught`；換匯側：
`tests/contract/test_cash_api.py::test_fx_over_balance_hard_block`、
`::test_fx_ack_negative_no_longer_bypasses`（FU-D34 無 ack）、
`tests/api/test_r1_fx_door_parity.py::test_a_pre_existing_dip_does_not_block_a_covered_conversion`
（既有 dip 規則）、`tests/data_ingestion/test_r1_fx_import_injection.py::`
`test_the_row_that_will_not_be_written_does_not_fund_its_sibling`（批 = 會被寫入之列）；更正門：
`tests/contract/test_cash_api.py::test_movement_edit_delta_guard_and_delete`、
`::test_deposit_edit_to_withdraw_keeps_removal_ack`、
`tests/api/test_r1_fx_door_parity.py::test_deleting_a_conversion_that_strands_a_later_withdrawal_needs_an_ack`。

**範例與現行覆蓋**：寫入門之透支由 `withdraw_insufficient_balance`／`fx_insufficient_balance` 硬拒（如上，
無 ack）；更正門一旦偵得某受影響池於**某時點**會降至負且未 `ack_negative`，即回 **422 `negative_cash`**
（訊息形如 `此筆會使 … 現金於某時點降至 …`）。合併後拓樸之當前壓測場景**未觸發**任一阻擋（其唯一一筆
Schwab USD→TWD 回換 USD 5,000 → TWD 162,000 通過檢查而成交，見 §8.2；該場景無 `negative_cash` 斷言）。
護欄行為由上列單元／契約測試錨定（更正門含 `tests/api/…` 之 `_negative_response`／`_pool_min` 路徑），
非由本 phase-1 場景之單一 op 錨定。

> **實作位置**：`portfolio/cash.py`（`cash_balances`、`pool_lines`、`running_min`、`running_statement`）、
> `data_ingestion/validate.py::_withdraw_issues`（FU-D43a）、`data_ingestion/fx_import.py::fx_balance_issues`
> （FU-D34）、`api/routers/cash.py`（`movement_guard`／`fx_change_guard`／`fx_delete_guard`、`_pool_min`、
> `_negative_response`——pool 算術以 `cash_pool_fn` 注入，見 `architecture.md` 之 C3 seam）、
> `api/routers/input_center.py::_cash_overdraft_issue`。
> **依據**：`.claude/rules/data-and-pricing.md`（cash pools；audit C3/C5/C9）；FU-D43a／FU-D34
> （owner 裁定，載於上列檔頭／函式 docstring）。

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
- **重算（重算）**：由五本帳本**完全重建**所有統計（見 §1.4；含 `corporate_actions`，§4.4）。

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

> **裁定日期註記**：owner 裁定 canonical 日期為 **2026-07-13**（程式碼 docstring 所載），為權威裁定日。
> 發版紀錄（MEMORY / v0.1.18）曾記為 07-14，惟以 **canonical = 2026-07-13** 為準（兩者指同一裁定 Option 1）。
> 仲裁時以「symbol-level 目標套用於合併部位」之語意為準。

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

### 11.4 再平衡彙總與 leg 金額

每 leg：`amount = shares × price`；每列（symbol）之 `shares / amount / fee / tax` = 該列各 leg 加總。整體彙總（報告幣）：

$$\text{turnover\_reporting} = \sum_{rows}\operatorname{convert}(\text{total\_amount},\ \text{rate})$$

$$\text{total\_fees\_reporting} = \sum_{rows}\operatorname{convert}(\text{total\_fee}+\text{total\_tax},\ \text{rate})$$

$$\text{cash\_after} = \sum_{rows}\begin{cases}+\operatorname{convert}(\text{total\_amount}-\text{fee}-\text{tax},\ \text{rate}) & \text{SELL（淨流入）}\\[2pt] -\operatorname{convert}(\text{total\_amount}+\text{fee}+\text{tax},\ \text{rate}) & \text{BUY（成本流出）}\end{cases}$$

皆為 compute-only 投影，不寫帳本；`rate` 與估值同儀表板 spot（§7.3）。

### 11.5 試算交易（What-if）投影

實作：`strategy/whatif.py::compute_whatif`。**純投影**，複用**真實費引擎**（§3 `compute_fees`）與**真實帳本重播**（§4 `build_book`），永不寫帳本。帳戶綁定（Q1）：顯式 `account_id` 優先，否則**持股最多**之帳戶；未持且未指定 → `WhatIfError` → 400。`amount = shares × price`。

- **買入**：`total_cost = amount + fee + tax`；`new_shares = held_shares + shares`；

$$\text{new\_original\_avg} = \frac{\text{held\_orig\_total} + \text{total\_cost}}{\text{new\_shares}},\qquad \text{new\_adjusted\_avg} = \frac{\text{held\_adj\_total} + \text{total\_cost}}{\text{new\_shares}}$$

  （同 §4 加權平均。）
- **賣出**：`proceeds_net = amount − fee − tax`（§5.1）；`adjusted_cost_removed = held_adj_avg × shares`（**等同** §4.1 之比例移除 `frac × adjusted_total`，因 `held_adj_avg = held_adj_total / held_shares`）；`realized = proceeds_net − adjusted_cost_removed`（§5.1）；`oversell = shares > held_shares`（**僅旗標**，試算不阻擋）。
- `new_weight = new_position_reporting / new_total`，其中 `new_total = current_total − old_position_reporting + new_position_reporting`（誠實退化：缺價／缺匯 → None）。

> **驗證錨點**：§11.4／§11.5 均為 compute-only，無壓測純量錨點；其 fee/tax 經 §3 `fee_engine.*`、成本／已實現經 §4／§5.1 之公式與錨點間接驗證。**建議納入下次壓測**。
> **實作位置**：`strategy/rebalance.py`（`compute_rebalance` 彙總段、`_Leg.amount`）、`strategy/whatif.py`（`compute_whatif`、`_new_weight`）。
> **依據**：`CLAUDE.md`（rebalance ruling）、`.claude/rules/domain-ledger.md`（費綁帳戶 I6；weighted-average；realized）。

---

## 12. 附錄

### 12.1 工作範例索引（每例附驗證錨點）

> **編號說明**：本表的 `E#` 是**工作範例編號**，與 §4.4.6 邊界矩陣的 `E#`（**邊界案例**編號）是兩套獨立
> 的編號，彼此無對應關係。

| # | 範例 | 章節 | 驗證錨點（`scope`） |
| --- | --- | --- | --- |
| E1 | TW 費／稅（2330 買 1,000@600 → fee 855） | §3.1 | `fee_engine.fee tw_broker/2330 buy 1000@600` |
| E2 | TW 現股賣稅（2330 賣 300@700 → tax 630） | §3.1 | `fee_engine.tax tw_broker/2330 sell 300@700` |
| E3 | TW ETF 賣稅（0050 賣 50@140 → tax 7） | §3.1 | `fee_engine.tax tw_broker/0050 sell 50@140` |
| E4 | US Schwab 賣（TSLA 20@260 → fee 0.12） | §3.2 | `fee_engine.fee schwab/TSLA sell 20@260` |
| E5 | US Moomoo 賣（NVDA 25@600 → fee 5.89） | §3.3 | `fee_engine.fee moomoo_my/NVDA sell 25@600` |
| E6 | MY 費 + 印花（1155 買 1,000@9.50 → fee 9.40／tax 10.00） | §3.4 | `fee_engine.fee/tax moomoo_my/1155 buy 1000@9.50` |
| E7 | 加權平均成本（0050 完整重播 → orig 14,850.91／adj 14,050.91） | §4.2 | `holding.* tw_broker|0050` |
| E8 | 已實現（0050 賣 → 363.9091） | §5.1 | `realized.realized tw_broker/0050@2026-04-10` |
| E9 | 未實現（TSLA → 100.00） | §5.2 | `holding.unrealized_pnl schwab|TSLA` |
| E10 | DRIP（MSFT gross 100 → 0.20 股 $0 成本，div_portion 0） | §6.2 | `holding.dividend_portion schwab|MSFT = 0.00` |
| E11 | TW 現金股利降成本（0050 net 800 → div_portion 800） | §6.1 | `holding.dividend_portion tw_broker|0050 = 800` |
| E12 | 總報酬（TWD 516,336.55） | §7.1 | `kpi.total_return TWD`（`phase1:final`） |
| E13 | FX 加權均率（schwab 32.875／moomoo 4.5） | §8.1 | `fx.avg_rate schwab / moomoo_my` |
| E14 | 未實現換匯（rollup −11,757.48 TWD） | §8.3 | `fx.reporting_unrealized rollup`（`phase1:final`） |
| E15 | 現金池期末（tw_broker TWD 1,089,099） | §9.2 | `cash.balance tw_broker|TWD`（`phase1:final`） |
| E16 | 負池護欄（更正門之 `negative_cash`＋`ack_negative`；寫入門為 FU-D43a／FU-D34 硬拒；當前場景未觸發，行為由單元測試錨定） | §9.3 | 單元 `_negative_response`／`_pool_min` |
| E17 | 賣超阻擋（422 `oversell_unacknowledged`） | §5.3／§10.5 | `guard.oversell_blocks`（`tw_broker/0050 sell 200>held 110`） |
| E18 | **SPLIT**（CA1 3-for-1 + 同日賣出 → 260 股；已實現 144.666…669） | §4.4.5(a) | `corp.anchor.split_forward`；`realized.realized tw_broker/CA1@2026-03-02` |
| E19 | **SPLIT 不動股利折入部分**（CA9 2-for-1 → 400 股；`dividend_portion` 4,000 不變） | §4.4.5(b) | `corp.anchor.split_shares_dividend_adj`；`corp.anchor.split_keeps_dividend_portion` |
| E20 | **EXCHANGE**（CA3→CA4 1-for-2 併入已持有 → 90 股／40,056） | §4.4.5(c) | `corp.anchor.exchange_merge`；`export.holdings.original_cost_total tw_broker\|CA4` |
| E21 | **SPINOFF**（CA7→CA8 1-for-4、`c=0.30` → 子 100 股／30,042.60；母 500 股／89,126.40） | §4.4.5(d) | `corp.anchor.spinoff_child_basis`；`corp.anchor.spinoff_parent_basis` |
| E22 | **反向分割 + 零股現金**（CA2 1-for-10 → 70.5 股；0.5 股普通賣出 realized −20.2127…979） | §4.4.5(e) | `corp.anchor.split_reverse`；`realized.realized tw_broker/CA2@2026-03-12` |
| E23 | **比例精確性**（CAR `210 × 1 / 3 = 70`，賣光 70 股得 201） | §4.4.5(f) | `corp.anchor.split_ratio_exact`；`corp.sell_exact_ratio_accepted` |
| E24 | **未能套用之行動之退化**（CAX 股數 −500 不變、旗標 True；XIRR 全投組空白且原因指名該列） | §4.4.6／§5.3／§7.2 | `corp.anchor.e5_source_unmoved`；`corp.anchor.e5_source_flagged`；`corp.xirr_blanked_by_unapplied`；`corp.xirr_reason_names_row` |
| E25 | **現金收支七種 kind／兩軸**（`BROKER_FEE` 100 → 池 **−100**（舊述詞為 +100）；`DEPOSIT` 1,000（成本 32,000）+ `INTEREST` 7 → `covered_ratio` **1**） | §9.1.1／§8.1 | `cash.balance scope = schwab|USD`、`fx.covered_ratio scope = schwab`（phase-1 ops 50–52，2026-08-13 加入）；單元 `tests/shared/test_cash_kinds.py`（`test_broker_fee_reduces_the_cash_balance`／`test_interest_does_not_dilute_the_coverage_ratio`） |
| E26 | **美股現金股利（P1b）**（`schwab/AAPL` `CASH` gross 100／withholding 30 → `net` **70**、issue 為空；空白預扣 → 軟性追問且仍記 `net = 100`） | §6.2b | `holding.dividend_portion scope = schwab|AAPL`（phase-1，2026-08-13 加入之 `CASH` 股利）；單元 `tests/data_ingestion/test_dividends.py::test_us_cash_dividend_is_not_a_mismatch`／`::test_us_cash_dividend_without_withholding_asks` |

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
| 現金收支種類（七種） | `CashKind` / `CASH_KIND_VALUES` / `CASH_MOVEMENT_KINDS` | §9.1.1 |
| 借方（減少池餘額）種類 | `DEBIT_KINDS` / `is_debit` / `movement_sign` | §9.1.1 |
| 外幣取得軸（進 `covered_ratio` 分母） | `fx_acquisition` / `is_fx_acquisition` | §8.1／§9.1.1 |
| 利息收入／融資利息／券商費用 | `INTEREST` / `INTEREST_EXPENSE` / `BROKER_FEE` | §9.1.1 |
| 單一持股權重 | `weight` | §7.3 |
| 產業／市場配置權重 | `sector_weight` / `weights` | §7.3 |
| 幣別視圖原幣市值 | `by_currency_value` | §7.3 |
| 報告幣總市值 | `reporting_total_value` / `total_market_value` | §7.1／§7.3 |
| 稅務已實現（賣出日匯率換算） | `reporting_realized` | §7.3 |
| 混合報告幣報酬率 | `total_return_rate`（blended） | §7.1 |
| 股利收入彙總 | `dividend_total` / `total_by_currency` | §7.4 |
| 年度股利預估 | `declared_gross` / `declared_net` | §7.4 |
| 淨值趨勢市值／累計淨投入 | `total_value` / `net_invested`（`TrendPoint`） | §7.5 |
| 配息偵測估算 | `est_gross` / `est_net` / `est_reinvest_shares` | §6.5 |
| 配股面額換股常數 | `TW_STOCK_PAR = 10` | §6.5 |
| 再平衡週轉／費用／預估餘額 | `turnover_reporting` / `total_fees_reporting` / `cash_after` | §11.4 |
| 試算後新均價 | `new_original_avg` / `new_adjusted_avg` | §11.5 |
| 公司行動（帳本） | `corporate_actions` / `CorporateAction` | §4.4 |
| 分割／換股／分拆 | `SPLIT` / `EXCHANGE` / `SPINOFF`（`CorporateActionKind`） | §4.4.4 |
| 比例（兩個正整數） | `ratio_to` / `ratio_from`（唯一套用出口 `apply_ratio`） | §4.4.2 |
| 分拆基礎移轉比例 | `cost_carry` | §4.4.4 |
| 同日優先序 | `EventPriority`（`OPENING`/`CORPORATE_ACTION`/`BUY`/`SELL`/`DIVIDEND`） | §4.1／§4.4.3 |
| 未能套用之公司行動 | `unbookable_action` / `UnappliedAction` / `Book.unapplied_actions` | §4.4.6／§5.3 |
| 行情原值／分割基礎 | `close_raw` / `split_basis`（`prices` 兩欄制） | §4.4.7 |

### 12.3 版本歷史

| 版本 | 日期 | 說明 |
| --- | --- | --- |
| `v1.0-draft` | 2026-07-15 | 首版草稿。基線 `v0.1.18 + feat/p3-batch3`。經 966 項對抗性對帳（966/966 通過）核對。**待 owner 確認為仲裁標準**。 |
| `v1.1-draft` | 2026-07-15 | **對抗性完整性稽核**：全庫清點所有金額／比值／指標計算後補齊缺漏之 class A 公式——新增 §6.5（配息偵測 inbox 估算：除息前持股權利、DRIP 再投資價估計、TW 配股面額 10 元換股）、§7.1 混合報告幣報酬率 + 月度快照、§7.3（單一持股／產業／市場配置權重、幣別視圖、報告幣估值通則、匯出合計列、稅務已實現以賣出日匯率換算）、§7.4（股利收入彙總 + 年度預估）、§7.5（淨值與累計投入趨勢）、§11.4（再平衡週轉／費用／預估餘額 + leg 金額）、§11.5（What-if 試算）。新增 §12.5「仲裁範圍外之數值公式一覽」逐項列舉全部 class B（技術指標／警示門檻／匯出比值）與 class C（LLM 額度／花費），達成「完全列舉」。基線不變；**仍待 owner 確認**。 |
| `v1.2` | 2026-07-15 | **owner 正式簽署為仲裁標準，自 v0.1.19 起生效**（去除「待 owner 確認」草稿狀態、版號脫離 -draft）。併入 owner 四項裁定：① 新增英文鏡像 `docs/accounting-formula-manual.en.md`（供 AI／agent 讀取之工作副本；本繁中文件為仲裁正本，每次 zh 變更須於同一 change set 內重生鏡像）；② 本次啟用（本列）；③ §11.1 再平衡裁定 canonical 日期定為 **2026-07-13**（發版紀錄之 07-14 僅為出貨日）；④ §3 費率誠實聲明：owner 完整費表已在案（→ `docs/reference/broker-fee-schedules-2026-07.md`），於 fee-engine-v2 升級時取代種子費率，升級前 §3 記述現行引擎所計並列明已知分歧（sec_fee 0.0000278→0.0000206、TAF/CAT/平台/交收費未建模、MY 費表結構不同、TW 群益 2.3 折先收後退＋捨入分歧），並於 §12.4 增設費用爭議註記；⑤ §7.3／§12.5 邊界裁定為定案（權重／報酬率維持於仲裁範圍內）。基線不變。 |
| `v1.3` | 2026-07-15 | **fee-engine v2 上線**（owner sign-off；§3 全面改寫）。① **TW 捨入 FE-D3**：fee/tax 由 四捨五入 改為**無條件捨去（ROUND_DOWN）至整數 NT$**，min NT$20 於 floor 之後比較（群益 142.5→142；當沖 tax 例 11→10）；② **US 規費 v2**：Schwab／Moomoo US 佣金 $0/平台 $0.99、SELL 加 SEC `0.0000206`+TAF `0.000195`（cap $9.79）、交收 `0.003/股`（cap 1%）、CAT `0.000003/股`——各成分分捨入後相加；③ **MY v2**：佣金 `0.03%`（min RM0.01）+平台 RM3+清算（cap RM1,000）+**SST 8%**；印花改為 `ceil(金額/1000)×RM1`（正股 cap RM1,000、**ETF 免徵**）；④ **FE-D2 US 印花**：US 交易之 MY 印花以 MYR 計、USD 記帳（`stamp_fx` 由呼叫端解析，缺匯率→0+soft issue）；⑤ **FE-D1 折讓款**：新增 §3.6 forecast `⌊fee×0.77⌋`（**非金額之記錄**，永不入 `compute_fees`；inbox/確認為 Wave B）；⑥ snapshot 帶 `engine="v2"`，**逐列費制**（舊列以舊快照裁定、永不重算）。費率一律置於 config。§3 範例驗證錨點更新為 fee-engine v2 壓測 phase1（`fee_engine.*` 80/80）。同 change set 重生英文鏡像。基線不變。 |
| `v1.4` | 2026-07-22 | **Batch B（Moomoo 合併）修訂**（基線 `v0.1.20 + Batch B`）。① **帳戶模型**：合併前的兩個 per-market Moomoo 帳戶（legacy ids 見 `data_ingestion/moomoo_merge.py`）併為單一雙市場帳戶 `moomoo_my`（settlement USD／funding MYR；規則綁 (帳戶, 市場)：US→(`moomoo_us`,`drip_us`)、MY→(`moomoo_my`,`cash`)，載於 `account_market_rules`）——§2 帳戶表 4→3 列、invariant I6 由「綁帳戶」改為「綁 (帳戶, 市場)」、§3.3／§3.4／§6.2／§6.3／§8／§9 之帳戶標籤與 `scope` 錨點全面 re-anchor 至 `moomoo_my`（市場由 symbol 帶出）。② **全錨點重新對帳**：壓測套件已重生為合併後拓樸（1,060 斷言、66 ops、1,060/1,060 通過、0 fail；spot USD/MYR 4.5→**4.6**、含一筆 Schwab USD→TWD 回換）。就此當前實跑更新所有場景依賴之終值：§7.1 總報酬 514,752.85→**516,336.55**（realized 186,333.50／unrealized 330,003.05）、§8.2 realized FX 0→**−2,375**（Schwab 回換）、§8.3 未實現 FX rollup −31,830.94→**−11,757.48**（`moomoo_my` 因 spot 4.6≠avg 4.5 現貢獻正值）、§9.2 現金池全面更新且 MYR 池改為單一直接錨定之 `moomoo_my|MYR = 123,201.91`、§5.1 TSLA proceeds/realized 5,199.86/199.86→**5,199.88/199.88**（SEC fee 0.14→0.12）；修正既存筆誤 E5（NVDA fee 1.41→5.89）、E6（1155 fee/tax 10.45/9.50→9.40/10.00）。③ **錨點穩健化**：波動之 `id=NN`（逐版重編）自 §12.1 fee 例移除、保留穩定之 check+scope；場景不再觸發之 `negative_cash`（舊 op47）改記為單元測試錨定（§9.3／E16）；賣超錨點改以 `guard.oversell_blocks` scope 記述。④ 驗證基礎行、§7.2 harness 計數（1,006→1,060）、§6.5 計數（966→1,060）同步更新。同 change set 重生英文鏡像。**無任何公式或會計定義變更——純為 (帳戶, 市場) 綁定 relabel + 錨點重新對帳。** |
| `v1.5` | 2026-07-26 | **清倉後入帳之現金股利改列已實現收益**（稽核 H2，owner 裁定 2026-07-26；基線 `v0.1.24`）。新增 **§6.3b**：CASH/NET 股利若入帳當下該 `(account, symbol)` 部位股數已為 0，已無成本可沖減，改記一列 `RealizedRow(kind="dividend")`（`realized = proceeds_net = net`；shares_sold／original_removed／adjusted_removed 皆 0；`sell_date` = 入帳日）。修正前該筆被 0 股部位吸收後隨部位一併丟棄，導致**股利總覽與 XIRR 有計入、總報酬沒有**的三方不一致；修正後恰好計入一次，**invariant I4 維持**。§5.1 同步標明 `RealizedRow` 現有 `kind: "sale" | "dividend"` 兩種。**稅務區隔**：年度稅務包 `realized_gains_{year}.csv` 僅取 `kind == "sale"`，該筆已由 `dividends_{year}.csv` 自股利帳本輸出，不重複申報。驗證錨點：`moomoo_my/5225` 買 200@6.00 → 賣 200@6.50（歸零）→ NET 股利 120 進入 `realized.by_currency[MYR]`（run_phase1「Found-bug op #3」；壓測 ops 66→**69**、斷言 1,060→**1,088**、fail=0）；hermetic 迴歸 `tests/portfolio/test_post_close_dividend.py`（5 例，含「清倉後買回再配息」仍走沖減成本）。**對既有真實帳本的影響：已清倉標的的歷史總報酬會上升**（原本漏計之股利現在計入）。同 change set 重生英文鏡像。除本條外無其他公式變更。 |
| `v1.6` | 2026-08-01 | **外幣現金流入之成本基礎 + 宣告式賣空**（owner 裁定 2026-07-30／07-31；基線 `v0.1.25`）。① **§8.1／§8.3 改寫**：取得來源由「僅換匯」擴為「換匯 **+** 帶 `acq_home_amount` 之外幣現金流入」；**存家幣金額、不存匯率**（匯率是平均值，§1.3 禁止其為權威；顯示用匯率讀取時計算）。新增 **`covered_ratio`**（有成本取得 ÷ 全部取得），流出**按比例**分攤——禁用「總餘額 − 無成本額」（餘額跌破時翻負，等同重製反號假數字）；該比率**同時**縮放現金與股票**兩條腿**（`avg_rate` 本身出自有成本母體，只縮放現金腿會漏標誤差更大的股票腿，實測差 +42,359 TWD）。`covered_ratio` 恆為字面 1 時呼叫端跳過乘法，故完整覆蓋之帳本**與本版前逐位元相同**。`foreign_cash` 現亦計入外幣現金流入／流出，故對同一 (account, foreign ccy) **恆等於 §9 營運現金池**（先前刻意分歧，audit C9），差異僅存於成本基礎。② **新增 §4.3 宣告式賣空**：`short_sale`（預設 false，**永不推斷**）；宣告賣出先出清長倉再開空倉（持有淨價款），買進先回補再入長倉，長／空互斥故部位以**單一帶號股數**表達；回補損益 `(short_avg − 買回每股全額成本) × 回補股數`，記於**回補日**，`kind="short_cover"`（進稅務資本利得表）。比率須除 `abs(cost_total)`、`fully_recovered` 須以 `not short_open` 設閘（放空基礎恆為負）。**放空期間之股利不可入帳**（放空方需支付；嚴格路徑 raise `UnbookableLedgerError`，儀表板路徑跳過並標記 `unbookable_dividend`）。已裁決之限制：`gross_invested` 不含放空資金、純放空 XIRR 反映融資利率、權重採淨曝險慣例。③ **賣超防呆改為日期感知**（`shares_through(交易日)`，對稱於現金之 `running_min`）、`oversold` 改為**黏性**（後續買進不清除，因被丟棄的成本基礎不會回來）。驗證錨點：`tw_broker/2609` 完整放空生命週期（見 §4.3 表）、`fx.covered_ratio/basis_gap/foreign_cash`；壓測 ops 69→**77**、斷言 1,088→**1,806**、fail=0，phase 2（線上 demo）1,192 斷言 fail=0。同 change set 重生英文鏡像。 |
| `v1.7` | 2026-08-11 | **公司行動（SPLIT／EXCHANGE／SPINOFF）**（owner 裁定 D1–D39，規格 `docs/spec/2026-08-06-corporate-actions.md`；基線 `v0.1.28 + feat/corporate-actions`）。① **新增 §4.4**（置於 §4 成本基礎之下、§4.3 之後，**不重新編號任何既有章節**——§7.5 於同一句中以現行編號指稱「§5 已實現／未實現損益」與「§7 總報酬」，重新編號會使該指稱與全庫既有的 §5.1／§7.2 等引用同時失效）：帳本列與**守恆律**（Σ`original_total`／Σ`adjusted_total`／Σ`dividend_portion`／`gross_invested` 皆不變；價值腿**僅** SPLIT）＋兩個刻意例外（零股現金 = 普通賣出、重組費 = `WITHDRAW`）；**比例為兩個正整數**且 `qty × to ÷ from` **先乘後除**（實測 `210×1/3 = 70` vs `210×(1/3) = 69.999…9`，後者會被 `validate.py` 的裸 `>` 判為賣超 → STICKY 丟棄基礎）；**三式與 `_Position` 九欄位移轉表逐字引用規格 §4.1–§4.4**（手冊不以自己的話重述公式）；D21 子公司回本進度須標示承接來源；**六個已驗證工作範例**（§12.1 之 E18–E24）；**邊界矩陣 E1–E24**（含賣超／宣告式賣空之交互 E3/E4/E5/E18/E22 與 E24）；價格基礎（`close_raw` / `split_basis`、讀取時再表述、**僅 SPLIT**）。② **§1.4／§1.1／§12.4：永久帳本由四本改為五本**（新增 `corporate_actions`）——遺漏它重算，會得到一個「看似正常、卻按行動前股數計價」的金額。③ **§4.1 同日優先序由 `0/1/2/3` 改為 `EventPriority` 之 `0/10/20/30/40`**，公司行動插入於 `OPENING` 與 `BUY` 之間；**相對順序未變**，故不含行動之帳本逐位元不變。④ **交叉引用**：§5.1（行動不產生 `RealizedRow`）、§5.3（`unbookable_action` 為第三種誠實退化）、§7.1（不動 `gross_invested`）、§7.2（行動非現金流；未套用之行動使 XIRR **全投組**空白且原因須指名帳戶／標的／日期）。⑤ **兩項限制**：**D11**（`volume` 不做還原）為**長期**限制；**D12**（重組費）**非**永久盲點——D36 裁定 XIRR **刻意不動**、另加**帳戶層 IRR** 於 `portfolio/twr.py`，本版就地查核該檔仍只有 TWR 指數／基準疊圖，故記為「**待 D36**」。⑥ **D34：現金＋股票混合併購為硬性排除**，舊規格的「兩列作法」已撤銷且**不得**作為程序出現（`CORPORATE_ACTION(10)` 先於 `SELL(30)` → EXCHANGE 歸零來源 → 同日 SELL 落在 0 股部位 → STICKY 賣超）；最接近的可表達作法記為**非官方變通**並載明其不精確之處。⑦ 驗證基礎更新為當前實跑（phase-1 **118 ops／3,791 斷言／0 fail**；phase 2 **1,192／0 fail**），公司行動錨點 `corp.*` 共 23 項全數通過。同 change set 重生英文鏡像。**除本條外無任何既有公式或會計定義變更。** |
| `v1.7a` | 2026-08-11 | **D45 — D36（帳戶層 IRR）由業主裁定不做。** §4.4.7 限制 2 與 §7 XIRR 註記皆改寫:重組費（D12）先前被記載為「對 XIRR 不可見、但帳戶層 IRR 會看見」，該第二個指標已撤銷且從未實作，故 D12 回復為**常設限制**——重組費在本系統現有的每一個報酬指標中都看不見，只在現金帳與淨值可見。**沒有任何數字改變**（XIRR 本來就不含現金收支），改的是限制的陳述:承諾一個不會到來的修正，比把盲點講清楚更糟 |
| `v1.8` | 2026-08-13 | **現金收支七種 kind／兩軸表 + 美股現金股利（P1b）**（業主裁決 **D1 = A**，2026-08-13；D35，2026-08-10）。① **新增 §9.1.1**：現金收支由四種增為**七種**（新增 `INTEREST` 利息、`INTEREST_EXPENSE` 融資利息、`BROKER_FEE` 券商費用），由 `shared/cash_kinds.py` 這唯一一張表以**兩條正交軸**規範——`credit`（是否增加池餘額）與 `fx_acquisition`（是否進 `covered_ratio` 分母）。舊述詞「`kind == "WITHDRAW"` 才是借方、其餘皆為取得型 credit」會讓一筆 `BROKER_FEE` **增加**現金餘額**又**拉低 `covered_ratio`（兩個錯誤的金額之記錄且皆不拋錯）；`INTEREST` 即是證明一個布林值不夠的那一列——**credit 但非取得**。② **§8.1 增訂第二軸之規範**：分母只收取得型 kind，池內孳生之收益（利息，同 sale proceeds 與外幣現金股利）**沿用池均價**；否則一個從未換匯、僅收到利息的 USD 帳戶會回報 `covered_ratio < 1`，並依 F3 把現金與股票整個曝險標為基礎不完整（**在每個真實帳戶上都響的假警報**）。輸入端據此以 `acq_cost_not_an_acquisition` 拒收利息／費用的取得成本。③ **§7.2 增訂明訂規則**：**七種 kind 全部不進 XIRR**（D1=A）——`xirr_reporting` 的簽章根本不收 `cash_movements`，流量序列仍只由 `opening` + `transactions` + `dividends` 構成；被否決的選項 B 會改變每一個歷史 XIRR 的意義，選項 C 因多數列**沒有 symbol** 而當場否決。④ **§9.3 明訂護欄不擴張**：`running_min` 提領護欄與 N1 外幣提領提示仍**只**鍵在 `WITHDRAW`——提領是使用者的**意圖**（值得擋），費用／融資利息是**已發生事實之記錄**，且融資帳戶本來就會有負現金餘額。⑤ **新增 §6.2b**：`drip_us` 模型自 P1b 起同時受理 `DRIP` 與 `CASH`，移除每列的 `dividend_type_mismatch` 軟阻擋；美股現金股利與 TW／MY 同式降成本（**D35**，本節只錨定公式不重新裁決），**預扣由使用者填寫而非 `gross × 0.30`**（券商實扣經其自身分位進位，與乘積不會逐分相符），空白預扣於 `drip_us` 上以軟性 `us_cash_dividend_no_withholding` 追問但**不改數**。§6 前言與 §6.3b 同步標明其適用範圍為 `CASH_DIVIDEND_TYPES` 全體。⑥ **§4.4.7 限制 2 就地更新**：原文「只有四種 kind、只有 `WITHDRAW` 是借方（`api/routers/cash.py::_KINDS`）」已失效（該常數不存在），改引 `CASH_MOVEMENT_KINDS`；D12 之結論**不變**且由 D1=A 再次確認。⑦ 新增 §12.1 之 **E25／E26**、§12.2 四則詞彙。**沒有任何既有數字改變**：三種新 kind 從未進入任何報酬流量序列，`covered_ratio` 恆 1 之帳本仍跳過乘法，故本手冊每一個已錨定的工作範例與壓測 oracle 的每一個期望值都留在原處。⑧ **壓測場景同版補齊**：phase-1 新增 ops 50–52（`schwab` USD 池之 `INTEREST`／`INTEREST_EXPENSE`／`BROKER_FEE`）與一筆 `schwab/AAPL` `CASH` 股利，實跑 **122 ops、3,799/3,799、0 fail**；oracle 之 `DEBIT_KINDS`／`ACQUIRING_KINDS` 係獨立於 app 的表另寫，故一致非自我確認。此前之「只有 hermetic 錨點」狀態已解除。同 change set 重生英文鏡像。 |
| `v1.9` | 2026-08-29 | **§9.3 護欄語意同步至實作（FU-D43a／FU-D34）**（QA R1 BUG-02；基線 `v0.1.28 + staged`）。§9.3 原記「現金門（deposit/withdraw、fx.convert）之硬護欄 = `running_min < 0` 且未 `ack_negative` → 422 `negative_cash`」——與實作及其自引之測試相左：**出金**為硬拒 **422 `withdraw_insufficient_balance`（FU-D43a：`ack_negative` 不可繞過、無融資）**，**換匯**為硬拒 **422 `fx_insufficient_balance`（FU-D34，需求五：無 ack、不提供融資）**；兩者皆為「期末餘額 + date-aware dip」雙檢、批次感知（CSV 同檔互為 sibling；換匯之批 = 結構有效**且**被勾選之列），並共用「不加深既有 dip 永不阻擋」規則（`after.low < min(before.low, 0)`）。credit 型 kind 進池本就不做餘額檢查（§9.1.1，未變）。`negative_cash`＋`ack_negative` **僅**存於縮減資金面之更正門（PUT／DELETE 現金收支、DELETE 換匯——date-aware `running_min`、**所有受影響池**：編輯之舊＋新 (account, ccy)、換匯刪除之兩腿）。§9.3 依此改寫（寫入門硬拒／更正門可 ack／交易門軟警告三類）並補列換匯側與更正門驗證錨點；§12.1 E16 措辭同步限縮。仲裁意涵：此前依 §1.1 仲裁一筆被拒之出金會誤判 app 為錯——本版起以實作（owner 簽署之 docstring 裁定 + §9.3 引用之測試）為準。同 change set 重生英文鏡像。**無任何公式或會計定義變更——純為護欄語意之文件同步。** |

### 12.4 如何仲裁一個爭議金額

給定一個「站上顯示為 X，但認為應為 Y」的金額：

1. **定位金額類型** → 對應章節：費／稅 §3；持倉成本／均價 §4；**公司行動（分割／換股／分拆）§4.4**；
   已實現 §5.1；未實現／資本利得 §5.2；
   股利 §6；**配息偵測估算 §6.5**；總報酬／報酬率（含混合率）§7.1；XIRR §7.2；**配置權重／產業／幣別視圖／
   報告幣估值／稅務已實現 §7.3**；**股利收入彙總／年度預估 §7.4**；**淨值與投入趨勢 §7.5**；換匯損益 §8；
   現金餘額 §9；再平衡 §11（**彙總 §11.4；試算 What-if §11.5**）。若該數字非以上任一 → 查 §12.5 是否屬
   仲裁範圍外之 class B／C（技術指標、警示門檻、LLM 額度）。
2. **取出相關帳本列**（五本永久帳本）：
   - 費／稅、成本、已實現、未實現 → `transactions`（該 account×symbol，**依 `trade_date` 排序**）+
     `dividends` + `opening_inventory` + **`corporate_actions`（該 account，`from_symbol` **或**
     `to_symbol` 命中該標的者皆須取出——換股會把基礎從**另一個代號**搬進來）**。
   - 換匯損益 → 該帳戶之 `fx_conversions` + `fx_rates`（當前 spot）。
   - 現金 → `cash_movements` + `fx_conversions` + 該池之 `transactions` + 現金股利。
3. **依該章節公式逐步重算**（重算）。務必套用：**同日優先序**
   `OPENING(0) ≺ CORPORATE_ACTION(10) ≺ BUY(20) ≺ SELL(30) ≺ DIVIDEND(40)`（§4.1）、賣出**比例
   移除**、股利模型（§6）、**公司行動三式與其比例之「先乘後除」（§4.4.2／§4.4.4）**、精度規範
   （§1.3，儲存全精度、僅結算／顯示量化）。
4. **比對**：重算值 = 裁定值。若與程式碼輸出不符 → 為程式碼 bug（提報）；若與本手冊公式不符 → 為手冊
   缺陷（提報並更新）。
5. **稽核佐證**：若該列曾被更正，查 `ledger_audit`（§10.3）取變更前值還原歷史。
6. **換匯爭議專屬檢查**：確認爭議者**未把換匯損益加疊於總報酬之上**（§8.4，invariant I5 — 最常見的重複
   計算來源）。

> **費用爭議專屬註記（fee-engine v2 已上線，逐列費制）**：裁定任一費／稅金額時，先讀該爭議列之
> **`fee_rule_snapshot`（§3、§10.2）——最終裁定依據**：帶 `engine="v2"` 者以 §3.1–§3.4 之 v2 公式裁定；無
> `engine` 標記之舊列以其快照所載之 v1 費率／捨入裁定（**永不重算**）。權威費表為
> `docs/reference/broker-fee-schedules-2026-07.md`。若 US 印花有爭議，另查快照 `stamp_fx_rate`／`stamp_myr`
> （FE-D2 換算軌跡）。TW 折讓款（`⌊fee×0.77⌋`，§3.6）為 **forecast、非金額之記錄**，不作為費／稅金額之
> 仲裁對象（歸類見 §12.5 class B）。

### 12.5 仲裁範圍外之數值公式一覽（完整列舉）

**完全列舉原則**：站上顯示／推播／匯出之**每一個數字**，若非**在仲裁範圍內附有公式**（§3–§11，class A 金額），
即**明列於下之範圍外**。範圍外分兩類：**class B 資訊型指標**（技術指標／警示門檻／分數／百分比——非「金額之
記錄」）與 **class C 營運成本會計**（LLM 額度／花費之美元計量）。範圍外項目**不作為金額爭議之仲裁對象**；其
正確性由各自單元測試守護，非本仲裁文件所裁。

**邊界說明（A/B 之界）**：配置權重（holding／sector／market weight，§7.3）與報酬率（§7.1）為「金額之比值」，
本手冊**納入 class A**（附公式），因其直接由市值金額導出、且驅動 §11／警示決策；其餘純比值／分數／門檻一律
class B。此界線經 **owner 於 2026-07-15 裁定為定案：權重／報酬率維持於仲裁範圍內，現行作法即為標準**（見 §7.3
仲裁邊界註記），此爭點已了結。

**Class B — 資訊型指標（informational；非金額之記錄）**

| 指標 | 公式（一行） | 實作位置 | 何以在範圍外 |
| --- | --- | --- | --- |
| day-change % | `(last − prev)/prev`（純價，刻意排除 FX） | `api/digest_service.py::_pct_from_last_two` | 百分比；推播硬規定僅帶百分比與計數 |
| 組合當日漲跌 | `Σ(wᵢ·pctᵢ)/Σwᵢ`（值權重） | `api/digest_service.py::_weighted_pct` | 百分比 |
| movers 排名 | 依 day-change % 排序取 top-N | `api/digest_service.py::_movers` | 排名 |
| SMA／均線 | `Σ(最後 N 收盤)/N` | `portfolio/technicals.py::moving_average` | 指標（幣值參考位，非記錄） |
| price_vs_maN | `(price − maN)/maN`（N=20/60/120） | `portfolio/technicals.py::ma_signals` | 比值 |
| 年化波動率 | `stdev_sample(日報酬) × √252` | `portfolio/technicals.py::annualized_volatility` | 波動度 |
| 最大回撤 | `min((close − running_peak)/running_peak)` | `portfolio/technicals.py::max_drawdown` | 比值 |
| RSI(14) | `100 − 100/(1+RS)`，`RS=avg_gain/avg_loss`（Wilder 平滑） | `portfolio/technicals.py::rsi` | 指標 |
| 均線交叉 | `sign(SMA_fast − SMA_slow)` 之翻轉 + `days_ago` | `portfolio/technicals.py::ma_cross` | 分類 |
| 52 週位置 | `pct_from_high=(price−hi)/hi`、`pct_from_low=(price−lo)/lo` | `portfolio/technicals.py::week52_position` | 比值（hi/lo 為幣值參考） |
| 趨勢結構／量能 | 半窗高低比較；`ratio_to_avg=latest/avg`，`surge=ratio≥2` | `portfolio/technicals.py::trend_structure`／`volume_signal` | 分類／比值 |
| price_vs_cost | `(price − original_avg)/original_avg`、`…/adjusted_avg` | `portfolio/technicals.py::price_vs_cost` | 比值（輸入為成本金額，輸出比值） |
| 法人連買／連賣、net_buy_sum | 連續天數；`Σ 近 N 日 daily_net` | `portfolio/external_signals.py` | 計數／外部籌碼（非記錄） |
| chg_pct／yoy／mom／percentile | `(curr−prev)/prev`；`count(h≤v)/len` | `portfolio/external_signals.py` | 比值／排名 |
| VIX／Fear&Greed 分區 | 門檻分類；`change = newest − oldest` | `portfolio/external_signals.py` | 分類 |
| PER／PBR／殖利率、融資融券、月營收 yoy/mom、指數收盤 | 直通或 `chg_pct/yoy/mom` | `portfolio/external_signals.py` | 外部脈絡（幣值參考，非記錄） |
| 市場別配置權重 | `sector_value / market_total`（同 §7.3） | `portfolio/market_view.py::market_allocation` | 比值 |
| 分析師共識 delta | `score_now − score_then`；目標均價下修 `(then−now)/then` | `api/alert_inputs.py`／`strategy/alerts.py` | 分數／比值 |
| SymbolMetric | `pct_from_52w_high`、`vol_30d`、`vol_90d`（√252 年化） | `api/alert_inputs.py::assemble` | 指標 |
| TechScore（複合） | `clamp(50 + Σ(score·applied_w·0.5), 0, 100)` | `strategy/rules/composite.py::compose` | 分數（0–100） |
| 12-1 動能／MA-cross／RSI-regime／trend-filter 分數 | 各 rule 之 [−1,1] 分數（params 常數見 `strategy/rules/params.py`） | `strategy/rules/*.py` | 分數 |
| 警示門檻比較 | `single_weight`／`sector_weight`／`fx_drift=\|spot/avg−1\|`／`drawdown=−pct_from_52w_high`（warn=0.5×risk）／`vol_spike=vol_30d/vol_90d`／`rebalance_drift band=min(abs, 0.25×target)`（Swedroe 5/25）／`calib_gap`（pp） | `strategy/alerts.py::compute_alerts_from` | 觸發布林（是否示警，非金額） |
| 匯出資訊欄 | `_return_ratio=unrealized_pnl/adjusted_cost_total`；TOTAL 權重 `Σ weight`；`sum_target=Σ targets`；`cash_level=max(0, 1−Σtargets)`；tax `rate_used` | `export/holdings_report.py`／`export/rebalance_report.py`／`export/tax.py` | 比值／百分比 |
| 讀取視窗推導 | `required_sessions`；`required_calendar_days=ceil(sessions×1.4×1.6)` | `api/signals_service.py` | 整數視窗 |
| TW 折讓款預估（§3.6, FE-D1） | `⌊fee × rebate_rate⌋`（rebate_rate=0.77） | `data_ingestion/fees.py::forecast_tw_rebate`（inbox/確認為 Wave B） | **FORECAST**；先收後退之預估，非金額之記錄，實際退款到帳確認後方入現金帳（`kind='rebate'`） |

**Class C — 營運成本會計（operational cost；美元計量，非投組金額之記錄）**

| 項目 | 公式（一行） | 實作位置 | 何以在範圍外 |
| --- | --- | --- | --- |
| 單次呼叫成本 | `cost = (in_tok × in_price_per_mtok + out_tok × out_price_per_mtok) / 1,000,000`（USD） | `shared/llm.py::cost_of` | LLM 營運花費，非投組金額 |
| 剩餘額度 | `budget_remaining = Σ topups − Σ usage.cost`（累計，無 reset） | `shared/llm_config.py::budget_remaining` | 額度會計 |
| 額度閘門 | `remaining ≤ 0 → LLMBudgetExceeded` | `shared/llm_config.py::check_budget` | 閘門 |
| 額度警戒門檻 | 預設 `1.00`（USD）；`quota_low` 於 `remaining < threshold` 觸發 | `shared/llm_config.py::get_alert_threshold`、`strategy/alerts.py` | 門檻／營運 |
| 用量匯出 | `llm_usage` / `job_runs` 直通匯出（token、cost 直讀，無新計算） | `export/usage.py` | 直通營運紀錄 |

> **完整性宣稱（complete-by-enumeration）**：截至基線 `v0.1.18 + feat/p3-batch3`，站上產生之數字經本次對抗性
> 清點後，**非落於 §3–§11（class A，附仲裁公式），即落於本 §12.5（class B／C，明列範圍外）**。日後新增任何
> 顯示／推播／匯出之數字，須同步歸類並補入本手冊（class A 補公式；class B／C 補本表），否則即為手冊缺陷
> （見 §12.4 步驟 4）。**尚未納入壓測錨點之 class A 公式**（§6.5、§7.3–§7.5、§11.4–§11.5）已於各節標注
> 「驗證錨點：無（建議納入下次壓測）」，供下一輪對抗性對帳補齊。

---

_本手冊為 `portfolio-dash` 之會計公式仲裁標準（已由 owner 於 2026-07-15 簽署，自 v0.1.19 起生效）。所有
工件（程式碼、規則檔、CHANGELOG）維持英文；本仲裁文件之繁中正文為經標示之刻意例外，且為**仲裁正本**；
英文鏡像 `docs/accounting-formula-manual.en.md` 僅供 AI／agent 讀取，每當本繁中文件變更須於同一 change
set 內同步重生。_
