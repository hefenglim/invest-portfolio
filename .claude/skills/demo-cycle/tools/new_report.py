"""new_report.py — scaffold a demo-cycle report: ONE self-contained HTML file.

Self-contained is a hard requirement, not a preference: these reports are read from
disk, mailed, and opened months later. No CDN, no relative asset, no build step. The
kit's CSS + JS (and optionally the app's own `web/format.js`, so a demo formats money
exactly the way the app does) are inlined at generation time.

    .venv/Scripts/python.exe .claude/skills/demo-cycle/tools/new_report.py \
        --kind spec --slug drip-preview --title "DRIP 預覽 — 規格 demo"

Kinds and where they land (owner decision 2026-07-27):
    spec       docs/spec/<date>-<slug>.html        new-feature proposal, demo IS the spec
    audit      docs/audit/<date>-<slug>.html       full-site audit
    fix        docs/audit/<date>-<slug>.html       remediation evidence
    rootcause  docs/audit/<date>-<slug>.html       where implementation diverged from the audit

The scaffold ships ONE working lab (a live CSS-cascade sandbox with a before/after
switch, a width slider, measured readouts and a verdict) plus a passing self-check, so
the first thing you do is edit a demo that already runs — never assemble one from prose.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
KIT = Path(__file__).resolve().parents[1] / "report_kit"

KINDS = {
    "spec": ("docs/spec", "feature proposal · spec by demo"),
    "audit": ("docs/audit", "full-site audit"),
    "fix": ("docs/audit", "remediation evidence"),
    "rootcause": ("docs/audit", "root-cause divergence demo"),
    # `manual` has no section skeleton — it exists so hand-authored documents
    # (--body/--demo-js) get the same chrome, tokens and self-check strip as a report.
    "manual": ("docs", "user manual"),
}

HOWTO = {
    "spec": "每一節先給<strong>現況</strong>與<strong>提案</strong>的並排對照，"
            "再給一個<strong>真的能操作的 demo</strong>。規格以 demo 為準，文字只是註解。"
            "你在 demo 上點過、認可了，才進入實作。",
    "audit": "每條發現都拆成<strong>症狀（實測）</strong>與<strong>根因（已驗證／推測待驗）</strong>兩層。"
             "症狀是量出來的；標為「推測待驗」的根因可能在實作時被推翻——"
             "你在核准前就知道哪幾項可能變形。",
    "fix": "每項修正都附<strong>改前／改後</strong>與<strong>可驗證的證據</strong>（實測數字、"
           "gate 輸出、可操作的 demo）。沒有證據的項目一律標為未完成。",
    "rootcause": "左邊是原本<strong>推測</strong>的根因，右邊是<strong>實測</strong>的根因，"
                 "中間的 demo 讓你親手驗證哪一個才對——跑的是真實機制，不是截圖、不是模擬。",
    "manual": "每一節都附一個<strong>可以動手玩的 demo</strong>。看不懂就直接玩那個 demo，"
              "文字是註解。",
}

SUB = {
    "spec": "這份文件用可操作的 demo 定義規格。文字描述容易各自解讀，demo 不會：你看到的行為就是要實作的行為。",
    "audit": "全站功能流程・按鈕接線・金額與成本計算・排版・系統級稽核。所有症狀均在跑著的實例上量測取得。",
    "fix": "稽核發現的逐項修正與驗證證據。每項都可在此頁當場複驗。",
    "rootcause": "實作時被推翻的根因推測，逐一還原機制並讓你親手切換前後狀態。",
    "manual": "完整使用手冊：四階段流程、三支量測工具、報告 kit API、使用技巧與誤判清單。",
}

# Section skeletons per kind: (anchor, toc label, heading, body html)
SECTIONS: dict[str, list[tuple[str, str, str, str]]] = {
    "spec": [
        ("s1", "① 問題", "問題與目標",
         '<p class="lede">TODO：現在做不到什麼、誰會痛、成功長什麼樣。一段就好。</p>'),
        ("s2", "② 現況 vs 提案", "現況 vs 提案",
         '<div class="vs">\n'
         '  <div class="vsbox before"><h4>現況</h4><p>TODO：今天的行為。</p></div>\n'
         '  <div class="vsbox after"><h4>提案</h4><p>TODO：改成什麼行為。</p></div>\n'
         '</div>\n{{LAB}}'),
        ("s3", "③ 邊界", "邊界、例外與不做的事",
         '<p class="lede">TODO：合法的例外（列舉自市場／券商規則，不是想像）、明確不做的範圍。'
         '收緊任何領域不變量前，先在這裡列出它必須放行的真實情形。</p>'),
        ("s4", "④ 驗收", "驗收條件",
         '<div class="tw"><table>\n'
         '<thead><tr><th>#</th><th>可觀察的驗收條件</th><th>怎麼驗</th></tr></thead>\n'
         '<tbody><tr><td>1</td><td>TODO</td><td>TODO：pytest / e2e / probe_live / demo 上的操作</td></tr>'
         '</tbody></table></div>\n'
         '<p class="note">實作前需要 owner 在此頁確認。確認後這一節就是驗收清單。</p>'),
    ],
    "audit": [
        ("s1", "① 範圍與方法", "稽核範圍與方法",
         '<p class="lede">TODO：涵蓋的頁面／API／計算；量測用的工具與版本；'
         '哪些是實測、哪些是讀碼推論（後者一律標「推測待驗」）。</p>'),
        ("s2", "② 分項評分", "分項評分",
         '<div class="tw"><table>\n'
         '<thead><tr><th>面向</th><th class="n">評分</th><th>依據</th></tr></thead>\n'
         '<tbody>\n'
         '  <tr><td>功能流程接線</td><td class="n">—</td><td>TODO</td></tr>\n'
         '  <tr><td>金額與成本計算</td><td class="n">—</td><td>TODO</td></tr>\n'
         '  <tr><td>前端顯示數字正確性</td><td class="n">—</td><td>TODO</td></tr>\n'
         '  <tr><td>排版與響應式</td><td class="n">—</td><td>TODO</td></tr>\n'
         '  <tr><td>系統級（錯誤處理／降級／型別）</td><td class="n">—</td><td>TODO</td></tr>\n'
         '</tbody></table></div>'),
        ("s3", "③ 高風險", "高風險缺口",
         '<h3>H1 · TODO 標題 <span class="sev h">HIGH</span></h3>\n'
         '<div class="vs">\n'
         '  <div class="vsbox before"><h4>症狀（實測）</h4><p>TODO：量到的數字＋在哪量的。</p></div>\n'
         '  <div class="vsbox after"><h4>根因（推測待驗 / 已驗證）</h4><p>TODO。</p></div>\n'
         '</div>'),
        ("s4", "④ 中風險", "中風險缺口", '<p class="lede">TODO</p>'),
        ("s5", "⑤ 低風險", "低風險 / 體質建議", '<p class="lede">TODO</p>'),
        ("s6", "⑥ 互動演示", "互動演示", "{{LAB}}"),
        ("s7", "⑦ 通過項目", "通過項目（實測確認）",
         '<p class="lede">TODO：實際驗過而且正確的部分。稽核報告只列問題會失真。</p>'),
        ("s8", "⑧ 修復路線", "建議修復路線",
         '<div class="tw"><table>\n'
         '<thead><tr><th>#</th><th>項目</th><th>風險</th><th>建議批次</th></tr></thead>\n'
         '<tbody><tr><td>H1</td><td>TODO</td><td><span class="sev h">HIGH</span></td>'
         '<td>批次 1</td></tr></tbody></table></div>'),
        ("s9", "⑨ 重現步驟", "重現步驟",
         '<pre><span class="cm"># 起一個乾淨實例（合成資料，可丟棄）</span>\n'
         'DB_PATH=&lt;tmp&gt;/probe.db PD_DISABLE_SCHEDULER=1 .venv/Scripts/python.exe scripts/seed_demo.py\n'
         'DB_PATH=&lt;tmp&gt;/probe.db PD_DISABLE_SCHEDULER=1 .venv/Scripts/python.exe -m uvicorn \\\n'
         '    portfolio_dash.api.app:create_app --factory --port 8477\n\n'
         '<span class="cm"># 量測（不是讀碼推論）</span>\n'
         '.venv/Scripts/python.exe .claude/skills/demo-cycle/tools/probe_live.py layout http://127.0.0.1:8477 ...\n'
         '</pre>'),
    ],
    "fix": [
        ("s1", "① 修正總表", "修正總表",
         '<div class="tw"><table>\n'
         '<thead><tr><th>#</th><th>稽核項</th><th>修法</th><th>證據</th><th>狀態</th></tr></thead>\n'
         '<tbody><tr><td>H1</td><td>TODO</td><td>TODO</td><td>TODO</td>'
         '<td><span class="pill pass">DONE</span></td></tr></tbody></table></div>'),
        ("s2", "② 根因分歧", "實作時發現的根因修正",
         '<p class="lede">稽核推測的根因與實測不同之處。<strong>沒有分歧就寫「無」</strong>——'
         '這一節空著代表推測全中，是有意義的資訊。</p>\n'
         '<div class="vs">\n'
         '  <div class="vsbox before"><h4>稽核推測</h4><p>TODO</p></div>\n'
         '  <div class="vsbox after"><h4>實測根因</h4><p>TODO</p></div>\n'
         '</div>'),
        ("s3", "③ 驗證證據", "驗證證據",
         '<p class="lede">TODO：gate 輸出（pytest／mypy／ruff／stress-audit）、probe_live 前後量測、'
         'verify_live 結果。貼真實輸出，不要轉述。</p>'),
        ("s4", "④ 前後對照", "前後對照演示", "{{LAB}}"),
        ("s5", "⑤ 剩餘事項", "剩餘事項",
         '<p class="lede">TODO：本次刻意不做的、需要 owner 裁決的、後續批次的。</p>'),
    ],
    "rootcause": [
        ("s1", "① 分歧一", "TODO 標題",
         '<div class="vs">\n'
         '  <div class="vsbox before"><h4>原本推測</h4><p>TODO</p></div>\n'
         '  <div class="vsbox after"><h4>實測根因</h4><p>TODO</p></div>\n'
         '</div>\n{{LAB}}'),
        ("s2", "② 總結", "一句話總結與防重演",
         '<div class="tw"><table>\n'
         '<thead><tr><th>案例</th><th>照原推測實作的後果</th></tr></thead>\n'
         '<tbody><tr><td>①</td><td class="bad">TODO</td></tr></tbody></table></div>\n'
         '<p style="margin-top:18px">防重演：TODO（LESSONS_LEARNED 的「類別」教訓、'
         'CHANGELOG 的分歧小節、現場註解）。</p>'),
    ],
}

# One WORKING lab. Ships live so the author edits something that already runs.
LAB_HTML = """<div class="lab">
    <h3>實驗：<span id="{p}-title">TODO — 這個 demo 證明什麼</span></h3>
    <div class="dsub">下面是真的 iframe，跑真的 CSS 級聯與真的 JS。拖動滑桿可改變它的視窗寬度。</div>
    <div class="modes" id="{p}-modes">
      <button data-m="0" data-tone="bad" class="on bad">現況（壞）<small>TODO</small></button>
      <button data-m="1" data-tone="good">修法<small>TODO</small></button>
    </div>
    <div class="sizer">
      <span class="w">視窗寬 <b id="{p}-w">420</b> px</span>
      <input type="range" id="{p}-range" min="320" max="900" step="10" value="420">
    </div>
    <div class="stage"><iframe id="{p}-frame" title="demo"></iframe></div>
    <div class="readout">
      <div class="ro" id="{p}-ro-a"><div class="k">computed white-space</div><div class="v">—</div></div>
      <div class="ro" id="{p}-ro-b"><div class="k">document scrollWidth</div><div class="v">—</div></div>
      <div class="ro" id="{p}-ro-c"><div class="k">被裁切的元素</div><div class="v">—</div></div>
    </div>
    <div class="verdict" id="{p}-verdict">—</div>
    <div class="note">TODO：一句話說明「照原建議改會怎樣」或「這個 demo 對應到畫面上的哪個後果」。</div>
  </div>"""

LAB_JS = """/* ---- lab {p} : a real cascade, measured live. Replace with the real case. ---- */
(function () {{
  var frame = PD.$('#{p}-frame'), mode = 0;
  function css(m) {{
    return '.row{{display:flex;gap:8px;padding:10px;border-bottom:1px solid #262e39}}' +
           '.row>*{{background:#161b22;border:1px solid #262e39;border-radius:5px;' +
           'padding:4px 8px;font-size:11.5px;color:#aab3c0}}' +
           '.sub{{padding:10px;color:#78838f;font-size:12px;' +
           (m === 0 ? 'white-space:nowrap;overflow:hidden;text-overflow:ellipsis' : '') + '}}';
  }}
  var html = '<div class="row">' +
    ['報告幣別 TWD', '資料新鮮', 'AI 未啟用', '⟳ 重新整理'].map(function (t) {{
      return '<span>' + t + '</span>';
    }}).join('') + '</div>' +
    '<div class="sub"><span id="s">唯讀 — 各資料表筆數與最早記錄，供保留期限評估；目前不做任何寫入。</span></div>';
  var probe = '{{ws:getComputedStyle(document.getElementById("s")).whiteSpace,' +
              'sw:document.scrollingElement.scrollWidth,' +
              'cw:document.scrollingElement.clientWidth,' +
              'clip:Array.from(document.querySelectorAll("*"))' +
              '.filter(function(x){{return x.scrollWidth>x.clientWidth+1}}).length}}';

  function render(m) {{
    mode = m;
    PD.sandbox(frame, {{ id: '{p}', css: css(m), html: html, probe: probe, onData: onData }});
  }}
  // The verdict must depend on the MODE as well as the measurement. A verdict keyed on
  // the measurement alone announces "fixed!" whenever the current width happens not to
  // trigger the defect — which is how a demo ends up contradicting its own switch.
  function onData(d) {{
    var over = d.sw > d.cw + 1, broken = over || d.clip > 0;
    PD.ro(PD.$('#{p}-ro-a'), d.ws, mode === 0 ? 'bad' : 'good');
    PD.ro(PD.$('#{p}-ro-b'), d.sw + ' px (可用 ' + d.cw + ')', over ? 'bad' : 'good');
    PD.ro(PD.$('#{p}-ro-c'), d.clip + ' 個', d.clip ? 'bad' : 'good');
    if (mode === 0) {{
      PD.verdict(PD.$('#{p}-verdict'), broken ? 'bad' : '', broken
        ? '<b>現況：在這個寬度下被裁切／撐開。</b>TODO 說明後果。'
        : '<b>現況：這個寬度剛好沒觸發。</b>把滑桿往左拉，缺陷就會出現——'
          + '這正是為什麼要用可調寬度的 demo，而不是一張截圖。');
    }} else {{
      PD.verdict(PD.$('#{p}-verdict'), broken ? 'bad' : 'good', broken
        ? '<b>修法後仍然壞掉——這個修法無效。</b>TODO：真正勝出的是哪一條？'
        : '<b>修法後：任何寬度都不再裁切或撐破。</b>TODO 說明機制。');
    }}
  }}
  PD.modes(PD.$('#{p}-modes'), render);
  PD.sizer(PD.$('#{p}-range'), frame, PD.$('#{p}-w'));
}})();

/* ---- self-checks: an offline recomputation must agree with the REAL engine. ----
   Replace the constant with a figure produced by the app (an /api/* response, a
   pytest fixture, a stress-audit oracle line) — never with a number you derived here. */
PD.checkEq('scaffold self-check (replace me)', PD.group('1234567.89'), '1,234,567.89',
           'thousands grouping of a Decimal string, no float involved');
PD.checkPanel(PD.$('#pd-selfcheck'));
"""


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")


def _assemble_authored(args: argparse.Namespace, shell: str, css: str, js: str,
                       out: Path, eyebrow: str) -> int:
    """Wrap a hand-authored body in the kit's chrome, deriving the TOC from it."""
    body = Path(args.body).read_text(encoding="utf-8")
    demo_js = Path(args.demo_js).read_text(encoding="utf-8") if args.demo_js else ""
    if "PD.checkPanel" not in demo_js:
        demo_js += "\nPD.checkPanel(PD.$('#pd-selfcheck'));\n"

    toc = []
    for anchor, h2 in re.findall(r'<section id="([^"]+)">\s*<h2[^>]*>(.*?)</h2>', body, re.S):
        label = re.sub(r"<span class=\"n\">.*?</span>", "", h2, flags=re.S)
        label = re.sub(r"<[^>]+>", "", label)
        toc.append(f'  <a href="#{anchor}">{" ".join(label.split())}</a>')
    toc.append('  <a href="#selfcheck">✓ 自我校驗</a>')

    html = (shell
            .replace("{{CSS}}", css).replace("{{JS}}", js).replace("{{DEMO_JS}}", demo_js)
            .replace("{{TOC}}", "\n".join(toc)).replace("{{SECTIONS}}", body)
            .replace("{{EYEBROW}}", f"portfolio-dash · {eyebrow}")
            .replace("{{TITLE}}", args.title).replace("{{SUB}}", SUB[args.kind])
            .replace("{{HOWTO}}", HOWTO[args.kind])
            .replace("{{FOOTER}}", f"portfolio-dash · {eyebrow} · {args.date} · "
                                   "頁內 demo 於 iframe 執行真實機制，非模擬。"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"  -> {out}  ({len(html):,} bytes, self-contained, {len(toc) - 1} sections)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", required=True, choices=sorted(KINDS))
    ap.add_argument("--slug", required=True, help="file-name slug, e.g. drip-preview")
    ap.add_argument("--title", required=True)
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--out", help="explicit output path (overrides the kind's folder)")
    ap.add_argument("--body", help="hand-authored <section> body; replaces the skeleton. "
                                   "The TOC is derived from each section's id + h2.")
    ap.add_argument("--demo-js", help="script for a --body document. It runs AFTER lab.js, "
                                      "so PD.* is available (a <script> inside --body "
                                      "would run before it).")
    ap.add_argument("--no-format-js", action="store_true",
                    help="do not inline web/format.js (default: inline it, so a demo "
                         "formats money with the app's own exact-decimal formatter)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = ap.parse_args(argv)

    folder, eyebrow = KINDS[args.kind]
    out = Path(args.out) if args.out else REPO_ROOT / folder / f"{args.date}-{_slugify(args.slug)}.html"
    if out.exists() and not args.force:
        print(f"!! {out} exists (use --force)", file=sys.stderr)
        return 2

    shell = (KIT / "shell.html").read_text(encoding="utf-8")
    css = (KIT / "lab.css").read_text(encoding="utf-8")
    js = (KIT / "lab.js").read_text(encoding="utf-8")

    fmt_js = ""
    if not args.no_format_js:
        fmt_path = REPO_ROOT / "web" / "format.js"
        if fmt_path.exists():
            fmt_js = ("/* ---- inlined from web/format.js: the APP's own exact "
                      "Decimal-string formatter. A demo must format money the way the "
                      "app does, or it is demonstrating something else. ---- */\n"
                      + fmt_path.read_text(encoding="utf-8") + "\n")

    if args.body:
        return _assemble_authored(args, shell, css, fmt_js + js, out, eyebrow)

    toc_parts, section_parts, demo_js = [], [], []
    for i, (anchor, label, heading, body) in enumerate(SECTIONS[args.kind], start=1):
        toc_parts.append(f'  <a href="#{anchor}">{label}</a>')
        if "{{LAB}}" in body:
            prefix = f"lab{i}"
            body = body.replace("{{LAB}}", LAB_HTML.format(p=prefix))
            demo_js.append(LAB_JS.format(p=prefix))
        section_parts.append(
            f'<section id="{anchor}">\n  <h2><span class="n">{i:02d}</span>{heading}</h2>\n'
            f'  {body}\n</section>')
    toc_parts.append('  <a href="#selfcheck">✓ 自我校驗</a>')

    if not demo_js:                       # every report gets the self-check strip
        demo_js.append("PD.checkPanel(PD.$('#pd-selfcheck'));\n")

    html = (shell
            .replace("{{CSS}}", css)
            .replace("{{JS}}", fmt_js + js)
            .replace("{{DEMO_JS}}", "\n".join(demo_js))
            .replace("{{TOC}}", "\n".join(toc_parts))
            .replace("{{SECTIONS}}", "\n\n".join(section_parts))
            .replace("{{EYEBROW}}", f"portfolio-dash · {eyebrow}")
            .replace("{{TITLE}}", args.title)
            .replace("{{SUB}}", SUB[args.kind])
            .replace("{{HOWTO}}", HOWTO[args.kind])
            .replace("{{FOOTER}}",
                     f"portfolio-dash · {eyebrow} · {args.date} · "
                     "頁內 demo 於 iframe 執行真實機制，非模擬。"))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"  -> {out}  ({len(html):,} bytes, self-contained)")
    print(f"  next: verify_report.py \"{out}\"  then edit the TODOs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
