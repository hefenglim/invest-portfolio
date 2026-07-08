/* portfolio-dash — shared windowed pager (WPB, 2026-07-07).

   window.pdPager.create({host, limit, offset, totalCount, onPage}) renders
   `◀ 1 2 3 … N ▶` (current ±2 + first/last + ellipsis), a 「第 X–Y / 共 N 筆」
   label and a jump-to-page input into `host`. Pure display/navigation: it never
   fetches and never touches money — the CONSUMER owns the endpoint call.

   Contract:
   - onPage(newOffset) fires on any page action; the pager marks itself busy
     (aria-busy on host, buttons disabled) until the consumer calls update().
   - update({limit?, offset?, totalCount?}) re-renders with fresh numbers and
     clears the busy state. Call it after every fetch resolves (or fails).
   - Hidden entirely (host.hidden) while the total fits on one page.

   Convention: missing host -> create() returns a no-op controller, so callers
   on surfaces without the pager markup skip gracefully (settings-JS 略過 rule). */
(function () {
  'use strict';

  function create(opts) {
    const host = opts && opts.host;
    const noop = { update: function () {}, setBusy: function () {} };
    if (!host) return noop;
    const onPage = (opts && opts.onPage) || function () {};
    const state = {
      limit: Math.max(1, (opts && opts.limit) || 50),
      offset: Math.max(0, (opts && opts.offset) || 0),
      total: Math.max(0, (opts && opts.totalCount) || 0),
    };
    host.classList.add('pd-pager');

    const el = (tag, cls, text) => {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text !== undefined) n.textContent = text;
      return n;
    };

    function pageCount() { return Math.max(1, Math.ceil(state.total / state.limit)); }
    function currentPage() { return Math.floor(state.offset / state.limit) + 1; }

    function go(page) {
      const p = Math.min(Math.max(1, page), pageCount());
      if (p === currentPage()) return;
      setBusy(true);
      onPage((p - 1) * state.limit);
    }

    /* windowed page list: 1 … (cur-2..cur+2) … N, with '…' for gaps */
    function pageList() {
      const n = pageCount();
      const cur = currentPage();
      const keep = new Set([1, n]);
      for (let p = cur - 2; p <= cur + 2; p += 1) {
        if (p >= 1 && p <= n) keep.add(p);
      }
      const sorted = Array.from(keep).sort(function (a, b) { return a - b; });
      const out = [];
      let prev = 0;
      sorted.forEach(function (p) {
        if (prev && p - prev > 1) out.push(null); /* gap -> ellipsis */
        out.push(p);
        prev = p;
      });
      return out;
    }

    function render() {
      host.replaceChildren();
      const onePage = state.total <= state.limit;
      host.hidden = onePage;
      if (onePage) return;
      const n = pageCount();
      const cur = currentPage();

      const prev = el('button', 'pg-btn', '◀');
      prev.type = 'button';
      prev.disabled = cur <= 1;
      prev.setAttribute('aria-label', '上一頁');
      prev.addEventListener('click', function () { go(cur - 1); });
      host.appendChild(prev);

      pageList().forEach(function (p) {
        if (p === null) { host.appendChild(el('span', 'pg-gap', '…')); return; }
        const b = el('button', 'pg-btn' + (p === cur ? ' active' : ''), String(p));
        b.type = 'button';
        if (p === cur) b.setAttribute('aria-current', 'page');
        b.addEventListener('click', function () { go(p); });
        host.appendChild(b);
      });

      const next = el('button', 'pg-btn', '▶');
      next.type = 'button';
      next.disabled = cur >= n;
      next.setAttribute('aria-label', '下一頁');
      next.addEventListener('click', function () { go(cur + 1); });
      host.appendChild(next);

      const from = state.offset + 1;
      const to = Math.min(state.offset + state.limit, state.total);
      /* counts only — thousands separators via toLocaleString (no money here) */
      const fmtN = function (v) { return Number(v).toLocaleString('en-US'); };
      host.appendChild(el('span', 'pg-label',
        '第 ' + fmtN(from) + '–' + fmtN(to) + ' / 共 ' + fmtN(state.total) + ' 筆'));

      const jump = el('span', 'pg-jump');
      jump.appendChild(el('span', null, '跳至'));
      const input = el('input', 'input pg-jump-input');
      input.type = 'number';
      input.min = '1';
      input.max = String(n);
      input.value = String(cur);
      input.setAttribute('aria-label', '跳至頁');
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') go(Number(input.value) || cur);
      });
      input.addEventListener('change', function () { go(Number(input.value) || cur); });
      jump.appendChild(input);
      jump.appendChild(el('span', null, '頁'));
      host.appendChild(jump);
    }

    function setBusy(busy) {
      if (busy) {
        host.setAttribute('aria-busy', 'true');
        host.querySelectorAll('button, input').forEach(function (node) {
          node.disabled = true;
        });
      } else {
        host.removeAttribute('aria-busy');
        render(); /* re-enable via a clean re-render (edge states restored) */
      }
    }

    function update(next) {
      if (next) {
        if (next.limit != null) state.limit = Math.max(1, next.limit);
        if (next.offset != null) state.offset = Math.max(0, next.offset);
        if (next.totalCount != null) state.total = Math.max(0, next.totalCount);
      }
      host.removeAttribute('aria-busy');
      render();
    }

    render();
    return { update: update, setBusy: setBusy };
  }

  window.pdPager = { create: create };
})();
