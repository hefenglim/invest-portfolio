"""Single entry point for the portfolio-dash stress-audit harness (re-runnable).

Phase 1 = clean-room LOCAL isolated server + fresh DB (under evidence/) + ABSOLUTE
          oracle reconciliation, including the independent XIRR scalar (tolerance check).
          Own uvicorn, PD_DISABLE_SCHEDULER=1.
Phase 2 = investor-realistic UI-first stress on the live demo (--base-url), ADDITIVE,
          with baseline-snapshot + DELTA reconciliation, plus the dividend-inbox 確認
          flow. Phase-2 data intentionally STAYS on the demo (--keep-data, the default).

RUN WITH THE REPO .venv PYTHON (so the spawned uvicorn uses the project deps), e.g.:
  .venv/Scripts/python.exe -m scripts.stress_audit.run_all --phase 1
  .venv/Scripts/python.exe scripts/stress_audit/run_all.py --phase 1

Usage:
  run_all.py                                         # phase 1 (clean-room, default)
  run_all.py --phase 1 --no-ui                       # phase 1, API only (no browser)
  run_all.py --phase 2 --base-url https://<demo>     # live demo (real URL from human_noted)
  run_all.py --phase all --base-url https://<demo>   # both

Evidence (all under scripts/stress_audit/evidence/, git-ignored, regenerated per run):
  Phase 1 -> oplog.jsonl        / assertions.jsonl
  Phase 2 -> oplog_phase2.jsonl / assertions_phase2.jsonl
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable  # run with the repo .venv python; the subprocesses inherit it


def _run(args: list[str]) -> int:
    print("\n>>>", " ".join(args), flush=True)
    return subprocess.run([PY, *args], cwd=str(HERE)).returncode


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["1", "2", "all"], default="1",
                    help="1 = local clean-room (default, safe); 2 = live demo (mutating); all")
    ap.add_argument("--base-url", default=None,
                    help="Phase 2 target (the live demo; real URL in docs/human_noted/). "
                         "Required for --phase 2 / all.")
    ap.add_argument("--no-ui", action="store_true",
                    help="Phase 1: skip the browser happy paths (API only).")
    ap.add_argument("--keep-data", action=argparse.BooleanOptionalAction, default=True,
                    help="keep run artefacts (phase-1 DB; phase-2 demo data stays additive). "
                         "Default: keep.")
    args = ap.parse_args()

    if args.phase in ("2", "all") and not args.base_url:
        sys.exit("--phase 2/all needs --base-url (the demo URL from docs/human_noted/).")

    rc = 0
    if args.phase in ("1", "all"):
        p1 = ["run_phase1.py"]
        p1 += [] if args.no_ui else ["--ui"]
        p1 += ["--keep-data"] if args.keep_data else ["--no-keep-data"]
        rc |= _run(p1)
    if args.phase in ("2", "all"):
        rc |= _run(["run_phase2.py", "--base-url", args.base_url])
        rc |= _run(["run_phase2_inbox.py", "--base-url", args.base_url])
    sys.exit(rc)


if __name__ == "__main__":
    main()
