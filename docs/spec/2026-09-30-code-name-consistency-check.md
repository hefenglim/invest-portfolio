# Spec — does a card's 「name (code)」 name the right instrument?

**Status:** proposal for the owner (ruling 8 = C, 2026-09-30: write the spec first, decide after).
**Origin:** the verifier's R9 observation ③ — card #220 wrote 「聯詠 (3008)」 (3008 is LARGAN)
and card #39 wrote 「聯詠（2412）」 (2412 is 中華電信); neither was flagged.

## 1. What exists today

`llm_insight/figure_check.py::_unknown_symbols` (M9, widened by DEF-082) flags a parenthesised
ticker-shaped code that is neither a registered symbol nor a form of a registered name. It asks
"does this code exist?", never "is it the company the sentence names?". A code that exists but
is paired with another company's name passes.

## 2. Measured on the demo (2026-09-30, read-only, 26 registered instruments, 224 cards)

Every 「name (code)」 / 「name（code）」 in title + summary + body, classified against the registry:

| class | occurrences | what it is | examples |
| --- | ---: | --- | --- |
| A — code not registered | 222 | abbreviations and currencies the existing check already handles | 本益比 (PE), 新台幣 (TWD), Moomoo MY (US) |
| B — name matches the registry (whole or part) | 378 | correct | 台積電 (2330), Maybank (1155), 玉山金 (2884) |
| C — the name is ANOTHER registered instrument's name | 5 | **wrong, provably** | 聯發科技 (2412) ×3 (2412 = 中華電信, 聯發科 = 2454); 台積電 (3008) ×2 (3008 = LARGAN) |
| D — the name is not in the registry at all | 119 | mostly correct aliases, some wrong | 大立光 (3008) ×24, 長榮 (2603) ×25, 微軟 (MSFT) ×13, 特斯拉 (TSLA) — correct; 聯詠 (3008) ×4, 陽明 (2603) ×2 — wrong |

Why D is large: the registry stores some names in English (3008 LARGAN, 2603 Evergreen, 1155 Maybank,
2609 Yang Ming) while cards write the Chinese name. A check that flags every mismatch would raise
~110 false alarms on 224 cards — worse than today's silence.

## 3. Options

| option | rule | on the demo | cost |
| --- | --- | --- | --- |
| **1. Another registered name** | flag when the text right before 「(code)」 ends with the name of a DIFFERENT registered instrument and not with the code's own name | catches the 5 in class C (2 cards), 0 false alarms; misses 聯詠 (3008) (聯詠 is not registered) and 陽明 (2603) (2609 is registered in English) | small: `figure_check` + the flag rendering the unknown-code flag already has |
| **2. Option 1 + Chinese aliases in the registry** | an optional `alias` per instrument (e.g. 3008 大立光, 2603 長榮, 2609 陽明), editable in the watchlist drawer; names ∪ aliases feed the check | additionally catches 陽明 (2603) once 2609 has the alias 陽明; still misses a company that is not registered at all (聯詠) | medium: one additive column, the edit UI, the AI resolve door may pre-fill it |
| 3. Flag every mismatch | name ≠ registry name → flag | ~110 false alarms (class D) | rejected |

No registry-only rule can catch 「聯詠 (3008)」: nothing in the ledger knows 聯詠 is 3034. That
would need a full exchange directory (every TWSE/TPEx/Bursa/US symbol and name) — a new data
source, out of scope.

## 4. Behaviour (option 1; option 2 only widens the name set)

- A new flag kind beside `unknown_symbols`: `mismatched_names: [{code, written, registered, belongs_to}]`,
  e.g. `{code: "3008", written: "台積電", registered: "LARGAN", belongs_to: "2330"}`.
- The card shows it with the existing figure-check styling: 「名稱與代號不符：台積電 (3008) —
  3008 登錄為 LARGAN；台積電 是 2330」.
- Matching: the written name is the run of CJK / Latin characters immediately before the
  parenthesis (prefix words such as 「您持有」「股」 are allowed — the check asks whether the run
  ENDS with a registered name). Case-insensitive for Latin; whole registry names and the
  DEF-082 word forms both count as the code's own name.
- Stored cards are re-checked on read like every other figure flag — no migration.

## 5. Tests

A contract test per class (A / B / C / D) over the real route, the two class-C demo shapes as
fixtures, a "no false alarm" test over the D examples above, and mutation evidence (drop the
"not the code's own name" condition → the B examples start flagging).

## 6. Decision needed

Option 1, option 2, or not now.
