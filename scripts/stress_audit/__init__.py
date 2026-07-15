"""Permanent stress-audit harness for portfolio-dash (壓力驗證 + 帳目可信度).

An independent, Decimal-exact accounting oracle that re-derives every money-of-record
figure from .claude/rules/*.md (importing NOTHING from portfolio_dash) and reconciles it
against the running app across four surfaces (API JSON / CSV export / print-report HTML /
browser DOM). See README.md for the SOP and the accumulation rules, and the /stress-audit
skill for the one-command entry point.

The modules are run as scripts (each puts its own directory on sys.path), so they import
sibling modules by bare name (``import oracle``, ``import common``). Invoke via run_all.py
or the phase runners with the repo .venv python.
"""
