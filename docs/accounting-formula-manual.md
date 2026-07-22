# 投資組合會計公式手冊（Accounting-Formula Manual）

> **版本**：`v1.4`（2026-07-22）
> **程式碼基線**：`v0.1.20 + Batch B（Moomoo 合併）`
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
> **驗證基礎**：本文件所有帶數字之工作範例，均取自或核對於**合併後拓樸（Batch B）之常駐壓測實跑**——
> 一組 **1,060 項對抗性對帳斷言**（adversarial reconciliation，`scripts/stress_audit/evidence/oplog.jsonl`
> ＋ `scripts/stress_audit/evidence/assertions.jsonl`；phase-1 `--ui` 實跑 **66 ops、1,060/1,060 全數通過、
> 0 fail**）。每一數字範例均標注其 `scope` 驗證錨點；場景依賴之終值另標其 phase（`phase1:final` 等）。手冊
> 作者未自行捏造任何數字。**注意**：壓測場景會逐版演進（合併後場景與 v1.3-basis 之 966 項run 不同），故本版已
> 就每一錨點重新對帳至上述當前實跑（見 §12.3 v1.4）。

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
  - 修復方式：補登遺漏的期初庫存／買入。

> **驗證錨點**：`guard.oversell_blocks`，`scope = tw_broker/0050 sell 200>held 110`（賣 200 > 持有 110 → 422
> `oversell_unacknowledged`）。（壓測 op 序號逐版重編，故此處以穩定的 check + scope 描述，不釘選 run-specific 之 op 編號。）
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

`US_WITHHOLDING = 0.30` 適用 Schwab 與 `moomoo_my` 之 US market leg 兩處美股（W-8BEN）。

### 6.3 MY 現金（`NET`，`moomoo_my` 之 MY market leg）— 單層淨額降成本

馬來西亞單層制（single-tier）：記**淨收金額**，與 TW 現金同走降成本：`adjusted_total −= net`。
驗證錨點：`ledger.div.net moomoo_my|1155`；`holding.dividend_portion moomoo_my/1155 = 306.25`
（注意：因該部位在股利後仍有賣出，`dividend_portion` 會隨賣出**比例移除**，故不等於累計股利總額——
交叉參見 §4.1 比例移除、§5.1）。

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

> **實作位置**：`portfolio/returns.py`（`total_return`、`xirr_reporting`）、`portfolio/results.py`
> （`ReturnSummary`、`CurrencyReturn`）。
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
- **累計淨投入 `net_invested`**：截至當日之流量累加，**流量符號與 XIRR 相反（§7.2 之負號）**：期初 `+original_cost_total`、買入 `+(qty×price+fees+tax)`、賣出 `−(qty×price−fees−tax)`、現金股利（CASH/NET）`−net`；DRIP/STOCK 中性。每筆流量以**其日期之 carry-forward 匯率**換算。

任一流量日期無「當日或之前」匯率 → 整條序列 `available = False`（與 §7.2 XIRR 之 all-or-nothing 一致）。

> **驗證錨點：無**（`trend` / `net_invested` 無壓測斷言，**建議納入下次壓測**）；其成分（`price × shares`、all-in 買入成本、賣出淨額、股利淨額、`convert`）已於 §4／§5／§7 驗證。
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

實作：`forex/pools.py::average_acquisition_rate`。僅計 `home → foreign` 方向之換匯：

$$\text{avg\_rate} = \frac{\sum \text{from\_amount}\ (\text{home})}{\sum \text{to\_amount}\ (\text{foreign})}\quad(\text{無此類換匯則 None})$$

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

$$\text{unreal\_stocks} = \text{foreign\_stock\_value}\times(\text{spot} - \text{avg\_rate})$$

$$\text{unreal\_cash} = \text{foreign\_cash}\times(\text{spot} - \text{avg\_rate})$$

其中 `foreign_cash` 為 **FX 曝險視角**之外幣餘額（由換匯 + 外幣買賣 + 外幣現金股利重建；**與 §9 營運現金池
不同**，見 `forex/pools.py` 檔頭 C9 說明）。`avg_rate is None` 或 `spot is None` → unrealized = `None`。

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

**負池通常代表漏記入金或換匯**。護欄分兩層：

- **現金門（deposit/withdraw、fx.convert）之硬護欄**：以 **`running_min`（date-aware，含未來回填）** 檢查
  「會使該池在**某時點**降至負」；若 `running_min < 0` 且未 `ack_negative` → **422 `negative_cash`**
  （`此筆會使 … 現金於某時點降至 … — 通常代表漏記入金或換匯;確認無誤可強制寫入`）。編輯／刪除須使
  **所有受影響池**（舊 + 新 account/ccy）皆不為負。
- **交易門之軟警告（soft）**：`api/routers/input_center.py::_cash_overdraft_issue` — **僅當**帳戶已啟用現金
  追蹤（≥1 筆 cash movement）**且**該筆買入會使該標的現金池 < 0 時，附一則**警告 issue（永不硬阻擋）**。
  未追蹤現金的帳戶不會觸發。

**範例與現行覆蓋**：`running_min` 硬護欄一旦偵得某池於**某時點**會降至負且未 `ack_negative`，即回 **422
`negative_cash`**（訊息形如 `此筆會使 … 現金於某時點降至 …`）。合併後拓樸之當前壓測場景**未觸發** `negative_cash`
阻擋（其唯一一筆 Schwab USD→TWD 回換 USD 5,000 → TWD 162,000 通過 running_min 檢查而成交，見 §8.2；該場景無
`negative_cash` 斷言）。此硬護欄之行為由單元測試錨定（`tests/api/…` 之 `_negative_response`／`_pool_min` 路徑），
非由本 phase-1 場景之單一 op 錨定。

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
| E16 | 負池護欄（`negative_cash` 硬護欄；當前場景未觸發，行為由單元測試錨定） | §9.3 | 單元 `_negative_response`／`_pool_min` |
| E17 | 賣超阻擋（422 `oversell_unacknowledged`） | §5.3／§10.5 | `guard.oversell_blocks`（`tw_broker/0050 sell 200>held 110`） |

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

### 12.3 版本歷史

| 版本 | 日期 | 說明 |
| --- | --- | --- |
| `v1.0-draft` | 2026-07-15 | 首版草稿。基線 `v0.1.18 + feat/p3-batch3`。經 966 項對抗性對帳（966/966 通過）核對。**待 owner 確認為仲裁標準**。 |
| `v1.1-draft` | 2026-07-15 | **對抗性完整性稽核**：全庫清點所有金額／比值／指標計算後補齊缺漏之 class A 公式——新增 §6.5（配息偵測 inbox 估算：除息前持股權利、DRIP 再投資價估計、TW 配股面額 10 元換股）、§7.1 混合報告幣報酬率 + 月度快照、§7.3（單一持股／產業／市場配置權重、幣別視圖、報告幣估值通則、匯出合計列、稅務已實現以賣出日匯率換算）、§7.4（股利收入彙總 + 年度預估）、§7.5（淨值與累計投入趨勢）、§11.4（再平衡週轉／費用／預估餘額 + leg 金額）、§11.5（What-if 試算）。新增 §12.5「仲裁範圍外之數值公式一覽」逐項列舉全部 class B（技術指標／警示門檻／匯出比值）與 class C（LLM 額度／花費），達成「完全列舉」。基線不變；**仍待 owner 確認**。 |
| `v1.2` | 2026-07-15 | **owner 正式簽署為仲裁標準，自 v0.1.19 起生效**（去除「待 owner 確認」草稿狀態、版號脫離 -draft）。併入 owner 四項裁定：① 新增英文鏡像 `docs/accounting-formula-manual.en.md`（供 AI／agent 讀取之工作副本；本繁中文件為仲裁正本，每次 zh 變更須於同一 change set 內重生鏡像）；② 本次啟用（本列）；③ §11.1 再平衡裁定 canonical 日期定為 **2026-07-13**（發版紀錄之 07-14 僅為出貨日）；④ §3 費率誠實聲明：owner 完整費表已在案（→ `docs/reference/broker-fee-schedules-2026-07.md`），於 fee-engine-v2 升級時取代種子費率，升級前 §3 記述現行引擎所計並列明已知分歧（sec_fee 0.0000278→0.0000206、TAF/CAT/平台/交收費未建模、MY 費表結構不同、TW 群益 2.3 折先收後退＋捨入分歧），並於 §12.4 增設費用爭議註記；⑤ §7.3／§12.5 邊界裁定為定案（權重／報酬率維持於仲裁範圍內）。基線不變。 |
| `v1.3` | 2026-07-15 | **fee-engine v2 上線**（owner sign-off；§3 全面改寫）。① **TW 捨入 FE-D3**：fee/tax 由 四捨五入 改為**無條件捨去（ROUND_DOWN）至整數 NT$**，min NT$20 於 floor 之後比較（群益 142.5→142；當沖 tax 例 11→10）；② **US 規費 v2**：Schwab／Moomoo US 佣金 $0/平台 $0.99、SELL 加 SEC `0.0000206`+TAF `0.000195`（cap $9.79）、交收 `0.003/股`（cap 1%）、CAT `0.000003/股`——各成分分捨入後相加；③ **MY v2**：佣金 `0.03%`（min RM0.01）+平台 RM3+清算（cap RM1,000）+**SST 8%**；印花改為 `ceil(金額/1000)×RM1`（正股 cap RM1,000、**ETF 免徵**）；④ **FE-D2 US 印花**：US 交易之 MY 印花以 MYR 計、USD 記帳（`stamp_fx` 由呼叫端解析，缺匯率→0+soft issue）；⑤ **FE-D1 折讓款**：新增 §3.6 forecast `⌊fee×0.77⌋`（**非金額之記錄**，永不入 `compute_fees`；inbox/確認為 Wave B）；⑥ snapshot 帶 `engine="v2"`，**逐列費制**（舊列以舊快照裁定、永不重算）。費率一律置於 config。§3 範例驗證錨點更新為 fee-engine v2 壓測 phase1（`fee_engine.*` 80/80）。同 change set 重生英文鏡像。基線不變。 |
| `v1.4` | 2026-07-22 | **Batch B（Moomoo 合併）修訂**（基線 `v0.1.20 + Batch B`）。① **帳戶模型**：合併前的兩個 per-market Moomoo 帳戶（legacy ids 見 `data_ingestion/moomoo_merge.py`）併為單一雙市場帳戶 `moomoo_my`（settlement USD／funding MYR；規則綁 (帳戶, 市場)：US→(`moomoo_us`,`drip_us`)、MY→(`moomoo_my`,`cash`)，載於 `account_market_rules`）——§2 帳戶表 4→3 列、invariant I6 由「綁帳戶」改為「綁 (帳戶, 市場)」、§3.3／§3.4／§6.2／§6.3／§8／§9 之帳戶標籤與 `scope` 錨點全面 re-anchor 至 `moomoo_my`（市場由 symbol 帶出）。② **全錨點重新對帳**：壓測套件已重生為合併後拓樸（1,060 斷言、66 ops、1,060/1,060 通過、0 fail；spot USD/MYR 4.5→**4.6**、含一筆 Schwab USD→TWD 回換）。就此當前實跑更新所有場景依賴之終值：§7.1 總報酬 514,752.85→**516,336.55**（realized 186,333.50／unrealized 330,003.05）、§8.2 realized FX 0→**−2,375**（Schwab 回換）、§8.3 未實現 FX rollup −31,830.94→**−11,757.48**（`moomoo_my` 因 spot 4.6≠avg 4.5 現貢獻正值）、§9.2 現金池全面更新且 MYR 池改為單一直接錨定之 `moomoo_my|MYR = 123,201.91`、§5.1 TSLA proceeds/realized 5,199.86/199.86→**5,199.88/199.88**（SEC fee 0.14→0.12）；修正既存筆誤 E5（NVDA fee 1.41→5.89）、E6（1155 fee/tax 10.45/9.50→9.40/10.00）。③ **錨點穩健化**：波動之 `id=NN`（逐版重編）自 §12.1 fee 例移除、保留穩定之 check+scope；場景不再觸發之 `negative_cash`（舊 op47）改記為單元測試錨定（§9.3／E16）；賣超錨點改以 `guard.oversell_blocks` scope 記述。④ 驗證基礎行、§7.2 harness 計數（1,006→1,060）、§6.5 計數（966→1,060）同步更新。同 change set 重生英文鏡像。**無任何公式或會計定義變更——純為 (帳戶, 市場) 綁定 relabel + 錨點重新對帳。** |

### 12.4 如何仲裁一個爭議金額

給定一個「站上顯示為 X，但認為應為 Y」的金額：

1. **定位金額類型** → 對應章節：費／稅 §3；持倉成本／均價 §4；已實現 §5.1；未實現／資本利得 §5.2；
   股利 §6；**配息偵測估算 §6.5**；總報酬／報酬率（含混合率）§7.1；XIRR §7.2；**配置權重／產業／幣別視圖／
   報告幣估值／稅務已實現 §7.3**；**股利收入彙總／年度預估 §7.4**；**淨值與投入趨勢 §7.5**；換匯損益 §8；
   現金餘額 §9；再平衡 §11（**彙總 §11.4；試算 What-if §11.5**）。若該數字非以上任一 → 查 §12.5 是否屬
   仲裁範圍外之 class B／C（技術指標、警示門檻、LLM 額度）。
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
