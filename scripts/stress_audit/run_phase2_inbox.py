"""Supplemental Phase 2 step: drive the dividend-inbox 重新偵測 + 確認入帳 UI flow on the
demo's /dividend-inbox.html page, then reconcile the newly-confirmed dividends. Appends
to the Phase 2 evidence files (additive).

The real demo URL lives in docs/human_noted/; supply it via --base-url.
"""

from __future__ import annotations

import argparse
import sys

import common as C
from phase2 import Ops2, reconcile_abs, snapshot

DEMO_PLACEHOLDER = "https://invest-demo.example.ts.net"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=DEMO_PLACEHOLDER,
                    help="live demo base URL (real value in docs/human_noted/)")
    args = ap.parse_args()
    if args.base_url == DEMO_PLACEHOLDER:
        sys.exit("Phase 2 inbox needs a real --base-url (the demo URL from docs/human_noted/).")

    ev = C.Evidence(oplog=C.EVIDENCE / "oplog_phase2.jsonl",
                    assertions=C.EVIDENCE / "assertions_phase2.jsonl", reset=False)
    api = C.Api(args.base_url, verify=False)
    import ui as UI
    ui = UI.UiDriver(args.base_url)
    ui.start()
    op = Ops2(ev, api, ui)
    try:
        res = op.inbox_refresh_confirm(max_confirm=3)
        print("inbox:", res)
        post = snapshot(api)
        reconcile_abs(ev, api, "inbox_final", post)
    finally:
        ui.stop()
        api.close()
    print(f"[phase2-inbox] pass={ev.n_pass} fail={ev.n_fail}")
    for f in ev.fails[:30]:
        print("  FAIL", f["check"], "|", f["scope"], "| exp=", f["expected"], "got=", f["actual"])


if __name__ == "__main__":
    main()
