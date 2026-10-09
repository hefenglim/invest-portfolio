#!/usr/bin/env python3
"""Generate the synthetic broker-raw-format corpus the adapter is regression-tested against.

    .venv/Scripts/python scripts/gen_broker_corpus.py [--check]

Why this exists
---------------
The transformation rules in ``data_ingestion/broker/`` were derived from a REAL 1,375-row
broker export. That export is the owner's personal financial data, is git-ignored, and can
never be a CI fixture — so without this the converter's logic would be covered by nothing
automated, and any refactor of it would be unverifiable.

This is the same split D27 drew for the corporate-action acceptance gate, applied to the
INPUT side: **the program is committed, real data is not**. What CI reads is a corpus in the
broker's exact wire shape whose tickers are invented and whose amounts are re-coded.

Design constraints (do not relax)
---------------------------------
* **No RNG.** Every value is a literal constant, so a re-run is byte-identical and a diff is
  always a real change. ``--check`` asserts exactly that, and the test suite runs it.
* **Shape-faithful, content-fictional.** Column order, date format, the ``$``/comma money
  formatting, the ``as of`` dual date, the ``TDA TRAN - …`` description grammar and the
  option-symbol layout are the broker's own. Only the identities and the numbers are made up.
* **Three files with an overlap**, because the real thing is five exports with overlapping
  date windows — merging them is the normal path, and the overlap is a case the importer has
  to handle rather than an accident of this fixture.

⚠ **This corpus proves the adapter can READ the shape. It does not prove your cost basis is
right.** Those are two different claims, and the second one is the owner-run acceptance step
against the real export. Same caveat the §10.5 corpus carries, for the same reason.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "golden" / "broker"

HEADER = ["Date", "Action", "Symbol", "Description", "Quantity", "Price",
          "Fees & Comm", "Amount"]

# Invented tickers. Deliberately unlike any real one, and deliberately including a pair
# (OLDX -> NEWX) whose exchange is 1-for-1 — the case that nets zero in BOTH dimensions and
# so is arithmetically indistinguishable from an internal journal.
_A, _B, _C = "ALFA", "BETA", "GAMM"
_OLD, _NEW = "OLDX", "NEWX"
_PAR, _SPN = "PARE", "SPIN"
_PRE = "PREH"          # a position that predates the corpus
_OPT = "ALFA 01/16/2026 50.00 P"

# The wire row that appears in BOTH file 1 and file 2 — the boundary duplicate a pair of
# overlapping exports produces. Held as one constant so the two copies cannot drift apart.
_BOUNDARY_WIRE = ["12/29/2024", "Wire Received", "", "WIRE INCOMING", "", "", "", "$5,000.00"]

# --------------------------------------------------------------------------- file 1
# Opens the history. Contains: funding, ordinary trades, a full DRIP triple whose printed
# quantity does NOT satisfy qty x price == amount, a null journal pair, interest both ways,
# a fee, and a cancel whose original sits on a DIFFERENT DATE.
FILE_1: list[list[str]] = [
    ["01/03/2024", "Wire Received", "", "WIRE INCOMING", "", "", "", "$20,000.00"],
    ["01/08/2024", "Buy", _A, "ALFA CORP", "100", "$25.00", "$0.00", "-$2,500.00"],
    ["02/12/2024", "Buy", _A, "ALFA CORP", "50", "$28.00", "$0.00", "-$1,400.00"],
    # A DRIP triple. 47.5 / 26.19 = 1.813669... and the broker PRINTS 1.8137 — so
    # quantity x price = 47.500... != 47.50 exactly. Amount and price are the authority.
    ["03/15/2024", "Cash Dividend", _A, "ALFA CORP DIVIDEND", "", "", "", "$67.86"],
    ["03/15/2024", "NRA Tax Adj", _A, "ALFA CORP W-8 WITHHOLDING", "", "", "", "-$20.36"],
    ["03/15/2024", "Reinvest Shares", _A, "ALFA CORP REINVESTMENT",
     "1.8137", "$26.19", "", "-$47.50"],
    # A null account-type journal: classified as noise, and it nets zero, so it drops.
    ["04/02/2024", "Journaled Shares", _A,
     "TDA TRAN - INTERNAL TRANSFER BETWEEN ACCOUNTS OR ACCOUNT TYPES (ALFA)",
     "25", "", "", "$0.00"],
    ["04/02/2024", "Journaled Shares", _A,
     "TDA TRAN - INTERNAL TRANSFER BETWEEN ACCOUNTS OR ACCOUNT TYPES (ALFA)",
     "-25", "", "", "$0.00"],
    ["05/06/2024", "Sell", _A, "ALFA CORP", "60", "$31.00", "$0.07", "$1,859.93"],
    ["06/28/2024", "Credit Interest", "", "CREDIT INTEREST FOR THE PERIOD", "", "", "",
     "$3.11"],
    ["07/31/2024", "Margin Interest", "", "MARGIN INTEREST CHARGE", "", "", "", "-$8.42"],
    # A symbol-less-looking fee row that names its ticker only in the text.
    ["08/15/2024", "ADR Mgmt Fee", "", "ADR MANAGEMENT FEE (BETA)", "", "", "", "-$4.10"],
    # A cancelled order. Rule 4: the original goes with the cancel — and the cancel is dated
    # the NEXT DAY, so a same-day-only matcher leaves a trade that never happened.
    ["09/10/2024", "Buy", _B, "BETA LTD", "40", "$12.00", "$0.00", "-$480.00"],
    ["09/11/2024", "Cancel Buy", _B, "BETA LTD CANCELLED", "40", "$12.00", "$0.00",
     "$480.00"],
    _BOUNDARY_WIRE,
]

# --------------------------------------------------------------------------- file 2
# The corporate-action file, plus the remaining suppression shapes. Its first row is the
# boundary duplicate: the same wire the previous export already reported.
FILE_2: list[list[str]] = [
    _BOUNDARY_WIRE,
    ["01/15/2025", "Buy", _B, "BETA LTD", "85", "$60.00", "$0.00", "-$5,100.00"],
    # 3-for-1 forward split: legs are UNEQUAL, which is what the quantity term protects.
    ["02/20/2025", "Journaled Shares", _B, "TDA TRAN - STOCK SPLIT (BETA)",
     "-85", "", "", "$0.00"],
    ["02/20/2025", "Journaled Shares", _B, "TDA TRAN - STOCK SPLIT (BETA)",
     "255", "", "", "$0.00"],
    # 1-for-10 reverse split.
    ["03/05/2025", "Buy", _C, "GAMM INC", "1000", "$0.40", "$0.00", "-$400.00"],
    ["03/20/2025", "Journaled Shares", _C, "TDA TRAN - MANDATORY REVERSE SPLIT (GAMM)",
     "-1000", "", "", "$0.00"],
    ["03/20/2025", "Journaled Shares", _C, "TDA TRAN - MANDATORY REVERSE SPLIT (GAMM)",
     "100", "", "", "$0.00"],
    # ★ THE killer case: a 1-for-1 ticker exchange. Zero cash on both legs AND equal share
    # counts, so it nets zero in both dimensions — indistinguishable from a journal by
    # arithmetic alone. Only the (date, symbol) key keeps it out of the drop.
    ["04/10/2025", "Buy", _OLD, "OLDX HOLDINGS", "200", "$9.00", "$0.00", "-$1,800.00"],
    ["05/22/2025", "Journaled Shares", _OLD, "TDA TRAN - MANDATORY - EXCHANGE (OLDX)",
     "-200", "", "", "$0.00"],
    ["05/22/2025", "Journaled Shares", _NEW, "TDA TRAN - MANDATORY - EXCHANGE (NEWX)",
     "200", "", "", "$0.00"],
    # A reorganisation FEE on the same family of description — must NOT read as an action.
    ["05/22/2025", "Journaled Shares", _NEW,
     "TDA TRAN - MANDATORY REORGANIZATION FEE (NEWX)", "", "", "", "-$38.00"],
    # A spin-off and the cash-in-lieu of its fractional share.
    ["06/12/2025", "Buy", _PAR, "PARE GROUP", "150", "$40.00", "$0.00", "-$6,000.00"],
    ["07/01/2025", "Journaled Shares", _SPN,
     "TDA TRAN - NON-TAXABLE SPIN OFF/LIQUIDATION DISTRIBUTION (SPIN)",
     "37", "", "", "$0.00"],
    ["07/01/2025", "Cash In Lieu", _SPN, "CASH IN LIEU OF FRACTIONAL SHARE", "", "", "",
     "$6.25"],
    # A DECLARED short and its cover. Never inferred — the ledger needs the flag.
    ["08/04/2025", "Sell Short", _C, "GAMM INC", "30", "$5.00", "$0.05", "$149.95"],
    ["09/09/2025", "Buy", _C, "GAMM INC BUY TO COVER SHORT POSITION", "30", "$4.00",
     "$0.00", "-$120.00"],
    # An ACAT platform transfer: a security leg and its cash leg, netting zero.
    ["09/20/2025", "Journaled Shares", _A,
     "TDA TRAN - TRANSFER OF SECURITY OR OPTION OUT (ALFA)", "10", "", "", "$0.00"],
    ["09/20/2025", "Journaled Shares", _A,
     "TDA TRAN - TRANSFER OF SECURITY OR OPTION OUT (ALFA)", "-10", "", "", "$0.00"],
    ["09/20/2025", "Journaled Shares", "",
     "TDA TRAN - CASH MOVEMENT OF OUTGOING ACCOUNT TRANSFER", "", "", "", "-$100.00"],
    ["09/20/2025", "Journaled Shares", "",
     "TDA TRAN - CASH MOVEMENT OF OUTGOING ACCOUNT TRANSFER", "", "", "", "$100.00"],
    # A mark-to-market round trip and an intra-account transfer.
    ["10/01/2025", "Journaled Shares", "", "TDA TRAN - MARK TO THE MARKET", "", "", "",
     "-$250.00"],
    ["10/01/2025", "Journaled Shares", "", "TDA TRAN - MARK TO THE MARKET", "", "", "",
     "$250.00"],
    ["10/02/2025", "Journaled Shares", "", "TDA TRAN - INTRA-ACCOUNT TRANSFER", "", "", "",
     "-$75.00"],
    ["10/02/2025", "Journaled Shares", "", "TDA TRAN - INTRA-ACCOUNT TRANSFER", "", "", "",
     "$75.00"],
    # A bare Journal +N / -N share pair.
    ["10/15/2025", "Journal", _A, "JOURNALED SHARES", "5", "", "", "$0.00"],
    ["10/15/2025", "Journal", _A, "JOURNALED SHARES", "-5", "", "", "$0.00"],
    # A DRIP re-booked at a corrected price: the adjustment and the reinvest it reverses.
    ["11/05/2025", "Reinvest Shares", _A, "ALFA CORP REINVESTMENT", "2.0000", "$30.00", "",
     "-$60.00"],
    ["11/06/2025", "Reinvestment Adj", _A, "ALFA CORP REINVESTMENT ADJUSTMENT", "2.0000",
     "$30.00", "", "$60.00"],
    # A position whose FIRST event is a sell, and which oversells: both pre-history
    # detectors fire on the same symbol, and the union must not double-count it.
    ["11/20/2025", "Sell", _PRE, "PREH CORP", "80", "$15.00", "$0.06", "$1,199.94"],
    # An option round trip. Recognised so rule 7 does not fire; routed nowhere (P3).
    ["12/01/2025", "Sell to Open", _OPT, "SELL TO OPEN", "1", "$2.00", "$0.65", "$134.35"],
    # A dual date: printed as posted-as-of-trade. The TRADE date is the second one.
    ["12/18/2025 as of 12/15/2025", "Buy", _A, "ALFA CORP", "10", "$33.00", "$0.00",
     "-$330.00"],
]

# --------------------------------------------------------------------------- file 3
# ONE row, and it is deliberately unmapped. Kept in its own file so the two corpora above
# stay parseable: a test asserts this raises, which is the only way to prove rule 7 is live
# rather than merely written down.
FILE_3: list[list[str]] = [
    ["01/06/2026", "Journaled Shares", _A,
     "TDA TRAN - A DESCRIPTION THIS ADAPTER HAS NEVER SEEN (ALFA)", "1", "", "", "$0.00"],
]

FILES: dict[str, list[list[str]]] = {
    "schwab_2024.csv": FILE_1,
    "schwab_2025.csv": FILE_2,
    "schwab_unmapped.csv": FILE_3,
}


def render(rows: list[list[str]]) -> str:
    """One file, in the broker's own wire shape: quoted fields, CRLF, a preamble line.

    The preamble is not decoration — a real export carries one, and the adapter LOCATES its
    header rather than assuming line 1. A fixture without it would leave that untested.
    """
    buf = io.StringIO()
    # The preamble doubles as the file's own label. Anyone opening one of these in Excel
    # should be able to tell in one line that it is invented — the repo also holds a
    # git-ignored folder of REAL broker exports, and the two must never be confused.
    buf.write(
        '"SYNTHETIC FIXTURE - generated by scripts/gen_broker_corpus.py. '
        'Fictional tickers and amounts; no real account data. Do not edit by hand."\r\n'
    )
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(HEADER)
    writer.writerows(rows)
    return buf.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="verify the committed files match this generator, and write nothing",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    drift: list[str] = []
    for name, rows in FILES.items():
        path = OUT_DIR / name
        text = render(rows)
        if args.check:
            # ``newline=""`` so the CRLF line endings are compared, not translated away.
            # (Not ``Path.read_text(newline=…)``: that keyword is 3.13+, and this project
            # is pinned to 3.12.)
            current = (
                path.open(encoding="utf-8", newline="").read() if path.exists() else ""
            )
            if current != text:
                drift.append(name)
            continue
        path.write_text(text, encoding="utf-8", newline="")
        print(f"wrote {path.relative_to(OUT_DIR.parent.parent.parent)} ({len(rows)} rows)")

    if args.check:
        if drift:
            print("DRIFT: " + ", ".join(drift), file=sys.stderr)
            print("re-run without --check to regenerate", file=sys.stderr)
            return 1
        print(f"OK — {len(FILES)} files match the generator")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
