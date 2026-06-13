# HANDOFF — 交付 Claude Code 操作說明

## 交付物
本專案(前端設計稿 + specs)整包下載後,與後端 repo 合併為 spec 19 §19.2 的佈局:

1. 後端 repo(`invest-portfolio-codebase` 內容)為基底。
2. 本專案根目錄全部 `*.html / *.js / *.css` → 移入 `web/`(`uploads/`、`.thumbnail` 不需要)。
3. `specs/` 整個資料夾 → repo 根目錄。
4. 在 repo 根建 `CLAUDE.md`,內容如下(可直接複製):

```markdown
# portfolio-dash — Claude Code 工作守則

## 任務入口
讀 specs/README.md:依其實作順序逐 spec 開發(08+17 先行)。
每個 spec 的完成定義 = `make all` 全綠(specs/17 §17.6)。

## 鐵律(違反即重做)
1. 金額一律 Decimal,API 序列化為字串;進位唯一 ROUND_HALF_UP(specs/18 §18.4)。
2. 四帳本 append-only;試算類 endpoint 純計算絕不寫入。
3. 不可跨幣別加總;報告幣別合併只在 portfolio/ 計算層。
4. 費稅計算唯一入口 compute_fees(specs/18 §18.5 三方一致性)。
5. 禁止為通過而改測試/快照,除非 commit 註明 spec 條文變更(specs/17 §17.7)。
6. 前端接線一律經 web/api.js(specs/19 §19.1),接線完成即刪 mock。
7. 計算 bug 修復前先補 worked example 重現(specs/18 §18.7)。

## 分層規則(既有架構,不可破壞)
shared/ 不 import 上層;bootstrap.py 是唯一同時碰 data_ingestion+shared 的組合根;
scheduler/ 只觸發、不含業務邏輯;新 api/ 層只做「呼叫核心+序列化」,不寫業務運算。
```

## 驗收口徑
- 全部完成 = specs/README 表內 19 份 spec 各自測試綠 + `make all`(unit/contract/e2e/regress)全綠。
- 人工抽驗動線:specs/17 §17.5 的 E1–E10 流程手動走一遍。

## 已知待辦(不阻塞)
- specs/18 §18.0 費率真值表:使用者以實際對帳單最終核對(SEC fee、馬股印花稅 cap、
  Moomoo 平台費買賣是否皆收)。
