# mypy: ignore-errors
"""MY (Bursa) sources: klsescreener + Bursa scrape; discovery notes for free MY data.

Discovery findings (filled in at probe time, 2026-06-08, sample code 3182 = GENTING):

- **klsescreener** (https://www.klsescreener.com/v2/stocks/view/{code}):
  Reachable (HTTP 200, no Cloudflare/bot block observed with a plain UA header).
  The live price lives in ``<h2 id="price" data-value="2.260">2.260</h2>`` (there is
  also a duplicate sticky-header copy at ``#price-fixed`` / ``#price_header-fixed``
  used by the page's scroll behaviour — ``#price`` is the primary node). Both the
  text content and the ``data-value`` attribute carry the **string** ``"2.260"`` —
  i.e. **3 decimal places are preserved**, unlike yfinance's float64 columns which
  collapse MY sub-pip precision into binary noise. This makes klsescreener a usable
  string-returning corroboration source for MY tick-precision verification.
  Note: GENTING (3182) trades around RM2.26, i.e. *not* a sub-RM1 counter, so this
  sample does not exercise the 0.005 sub-RM1 / 0.001 ETF tick edge cases directly —
  but it does prove the *site* serves 3-dp strings, which is what matters for a
  future adapter (the precision ceiling is in the source, not lost in transport).

- **bursamalaysia.com** (equities prices page): Reachable at the network level but
  returns **HTTP 403 "Just a moment..."** — a Cloudflare JS challenge page, not the
  real content. JS-rendered/bot-gated as anticipated; not scrapable with a plain
  ``requests`` GET. Would need a headless browser (Playwright) to pass the
  challenge — out of scope for a lightweight probe / not worth it given klsescreener
  already works.

- **marketstack** (https://marketstack.com, `/v1/eod`): Keyed. A request without a
  valid key returns HTTP 401 ``invalid_access_key``. Free tier exists (per their
  marketing) but requires signup; KLSE (`.XKLS`) coverage unverified without a key.
  Catalogue note only — no fixture.

- **eodhd** (https://eodhd.com, `/api/eod/{code}.KL`): Keyed. Returns HTTP 403
  ``Forbidden`` for an unauthenticated/demo request — stricter than marketstack
  (does not even surface a structured "need a key" JSON error). Free tier exists
  per their docs but requires registration; KLSE (`.KL`) coverage unverified without
  a key. Catalogue note only — no fixture.

- **twelvedata** (https://api.twelvedata.com, `/price`): Keyed. The `demo` key is
  rejected with HTTP 401 and a message pointing to their (claimed free) signup flow.
  KLSE (`:XKLS`) coverage unverified without a real key. Catalogue note only — no
  fixture.

- **i3investor** (https://klse.i3investor.com/web/stock/overview/{code}): Reachable
  (HTTP 200, ~280 KB page), serves a real overview/quote page for the sample code
  (GENTING). Decimal numbers observed on the page are in **2-dp** form (e.g.
  ``2.26``), so it does not appear to preserve 3-dp precision — likely rounds/
  truncates for display the same way yfinance's float64 does. Usable as a secondary
  corroboration / news-and-fundamentals source, not as a 3-dp price-precision source.

- **Malaysiastock.biz** (https://www.malaysiastock.biz, Corporate-Infomation.aspx):
  Reachable (HTTP 200, ~125 KB page), serves a real quote page for the sample code
  (title: "GENTING ▼-2.2% Share Price"). Decimal numbers observed are in **3-dp**
  form (e.g. ``2.260``, ``2.270``, ``0.050``) — like klsescreener, this site appears
  to preserve/display Bursa's native tick precision as text. A second viable
  string-returning corroboration candidate alongside klsescreener, worth keeping in
  the catalogue for redundancy (single-source risk).

Bottom line for the spec phase: **klsescreener is the recommended scrape-based MY
corroboration source** — reachable without bot-blocking, serves 3-dp price strings.
Malaysiastock.biz is a viable secondary/fallback with the same 3-dp property.
i3investor is reachable but only useful for narrative/fundamentals (2-dp display).
bursamalaysia.com direct scraping is blocked by Cloudflare. The keyed APIs
(marketstack / eodhd / twelvedata) all advertise free tiers but none are usable
without registering for a real key — their KLSE coverage and precision remain
unverified and are catalogue notes only.
"""

import requests
from bs4 import BeautifulSoup

KLSE_VIEW = "https://www.klsescreener.com/v2/stocks/view/{code}"
BURSA_PRICES = "https://www.bursamalaysia.com/market_information/equities_prices"
MY = ["5212", "3182", "5347", "1155", "1818"]
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; portfolio-dash-probe)"}


def fetch_klse_html(code: str) -> str:
    resp = requests.get(KLSE_VIEW.format(code=code), headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def parse_klse_price(html: str) -> str | None:
    """Extract the current price string from a klsescreener stock-view page.

    Discovered DOM shape (probe time): the live price lives in
    ``<h2 id="price" data-value="2.260">2.260</h2>``. There is a duplicate copy in
    the page's sticky header (``#price-fixed`` / ``#price_header-fixed``) used only
    for scroll behaviour — ``#price`` is the primary node. Prefer the ``data-value``
    attribute (the page's own canonical string form) and fall back to the element's
    text content; both carry full decimal precision (e.g. ``"2.260"``, 3 dp).
    """
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("#price")
    if node is None:
        return None
    value = node.get("data-value")
    if isinstance(value, str) and value.strip():
        return value.strip()
    text = node.get_text(strip=True)
    return text or None
