/* portfolio-dash — 設定 · 介面偏好 (WPC 2026-07-07, wired to GET/PUT /api/ui-prefs).

   每頁筆數: a backend-persisted global page size every pager consumer clamps
   against its endpoint max (min(pdPrefs.page_size, endpoint max)). Saving also
   refreshes window.pdPrefs + the localStorage pd_prefs cache so other pages pick
   the new value up on their next boot. Missing node -> skip (settings-JS 略過
   convention; the panel lives only on the canonical tabbed settings.html). */
(function () {
  'use strict';
  const sel = document.getElementById('pref-page-size');
  if (!sel || !window.pdApi) return;

  function _toast(msg, kind, code) {
    if (window.toast) window.toast(msg, kind, code);
  }

  function applyLocal(pageSize) {
    if (window.pdPrefs) window.pdPrefs.page_size = pageSize;
    try {
      localStorage.setItem('pd_prefs', JSON.stringify({ page_size: pageSize }));
    } catch (e) { /* cache is best-effort */ }
  }

  /* boot: reflect the persisted value (fresh GET — the source of truth) */
  window.pdApi.get('/api/ui-prefs').then(function (p) {
    if (p && p.page_size != null) {
      sel.value = String(p.page_size);
      applyLocal(p.page_size);
    }
  }).catch(function () { /* leave the default option selected */ });

  sel.addEventListener('change', function () {
    const value = Number(sel.value);
    sel.disabled = true;
    window.pdApi.put('/api/ui-prefs', { page_size: value }).then(function (p) {
      const saved = (p && p.page_size != null) ? p.page_size : value;
      sel.value = String(saved);
      applyLocal(saved);
      _toast('已儲存', 'ok', '每頁筆數 ' + saved + ' 筆（重新整理頁面後生效）');
    }).catch(function (err) {
      _toast((err && err.message) || '儲存失敗', 'fail', err && err.code);
      /* re-sync to the server's last-good value */
      window.pdApi.get('/api/ui-prefs').then(function (p) {
        if (p && p.page_size != null) sel.value = String(p.page_size);
      }).catch(function () { /* keep current */ });
    }).then(function () { sel.disabled = false; });
  });
})();
