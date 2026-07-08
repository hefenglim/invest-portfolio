"""News content pipeline: HTML fetch -> LLM organize -> separate news DB -> variable.

A dedicated sub-package (decision 2026-07-06, human sign-off): news links come from
FinMind / yfinance / Yahoo-TW, a general HTML fetcher pulls the article body, the default
LLM organizes it into {title, date, body_summary, related_stocks}, and the result is stored
in a SEPARATE SQLite database (news.db) — larger text volume kept off the ledger DB and
ready for future multi-account shared read/write. News is qualitative-only (invariant #1:
the LLM never emits numbers of record; insight cards still compute every figure locally).
"""
