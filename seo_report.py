"""
seo_report.py — builds the client-ready, self-contained HTML report (no external files, works offline,
prints cleanly to PDF).  The layout lives in report_template.html and can be replaced without touching code:
placeholders are written {{NAME}}.
"""
from __future__ import annotations

import html as _html
import re
import sys
from datetime import datetime
from pathlib import Path

import seo_score as S

TEMPLATE_NAME = "report_template.html"


def _tpl_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    p = base / TEMPLATE_NAME
    return p if p.exists() else Path(__file__).resolve().parent / TEMPLATE_NAME


def e(x) -> str:
    return _html.escape(str(x if x is not None else ""), quote=True)


LABEL_COLOR = {"Excellent": "#0b7a4b", "Very good": "#2f9e63", "Good": "#7ab648",
               "Needs improvement": "#d98a00", "Poor": "#c0262d"}

CSS = """
:root{--ink:#1b2333;--muted:#566079;--line:#dfe4ee;--bg:#f3f5f9;--card:#fff;--blue:#1f5fd1;--grey:#aab3c9;
--good:#0b7a4b;--warn:#b26a00;--bad:#c0262d}
*{box-sizing:border-box}body{margin:0;font:16px/1.55 "Segoe UI",system-ui,Arial,sans-serif;color:var(--ink);background:var(--bg)}
.wrap{max-width:1080px;margin:0 auto;padding:0 24px}
.hero{background:linear-gradient(135deg,#1f2f66,#1f5fd1);color:#fff;padding:36px 0 30px}
.hero h1{margin:.2em 0 .1em;font-size:2rem}.eyebrow{letter-spacing:.14em;text-transform:uppercase;font-size:.8rem;opacity:.85}
.sub{margin:.2em 0 .8em;opacity:.92}.tags span{display:inline-block;background:rgba(255,255,255,.16);border-radius:99px;
padding:2px 12px;margin-right:8px;font-size:.82rem}
section{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px 24px;margin:22px 0}
section.two{display:grid;grid-template-columns:1fr 1fr;gap:28px}section.two>div{min-width:0}
h2{margin:0 0 14px;font-size:1.2rem}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}
.card{border:1px solid var(--line);border-radius:12px;padding:14px 16px;background:#fafbfe}
.card .k{font-size:.8rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.card .v{font-size:1.9rem;font-weight:700;line-height:1.2}.card .s{font-size:.85rem;color:var(--muted)}
.up{color:var(--good)}.down{color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:.92rem}th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:#eef1f7;font-weight:600}td.n,th.n{text-align:right;white-space:nowrap}
.pill{display:inline-block;border-radius:99px;padding:1px 10px;font-size:.78rem;color:#fff;white-space:nowrap}
.bar{height:10px;border-radius:6px;background:#e6eaf3;overflow:hidden;min-width:90px}.bar i{display:block;height:100%}
details summary{cursor:pointer;color:var(--blue)}details ul{margin:.4em 0 .2em 1.1em;padding:0;color:var(--muted);font-size:.86rem}
.badge{display:inline-block;font-size:.72rem;border:1px solid var(--warn);color:var(--warn);border-radius:6px;padding:0 6px;margin-left:6px}
.note{color:var(--muted);font-size:.9rem}.ok{color:var(--good);font-weight:600}
.method p,.method li{color:#2f3a55;font-size:.93rem}.foot{color:var(--muted);font-size:.85rem;padding:6px 24px 36px}
svg text{font-family:inherit}
@media(max-width:760px){section.two{grid-template-columns:1fr}}
@media print{body{background:#fff}.hero{-webkit-print-color-adjust:exact;print-color-adjust:exact}
section{break-inside:avoid;border-color:#ccd3e2}details{display:block}details summary{display:none}
details ul{display:block}.pages-table details{break-inside:avoid}}
"""


def _pill(label: str) -> str:
    return f'<span class="pill" style="background:{LABEL_COLOR.get(label, "#566079")}">{e(label)}</span>'


def _delta_html(d: float, suffix: str = "") -> str:
    cls = "up" if d > 0.005 else ("down" if d < -0.005 else "")
    sign = "+" if d > 0.005 else ""
    return f'<span class="{cls}">{sign}{d:.2f}{suffix}</span>'


def cards_html(sm: dict, cost=None) -> str:
    if not sm or not sm.get("pages"):
        return '<p class="note">No pages had both a before and an after score in this run.</p>'
    rel = f' &middot; {sm["relative_pct"]:+.1f}% relative' if sm.get("relative_pct") is not None else ""
    def card(k, v, s=""):
        return f'<div class="card"><div class="k">{e(k)}</div><div class="v">{v}</div><div class="s">{s}</div></div>'
    n = sm["pages"]
    cards = [
        card("Average score before", f'{sm["avg_before"]:.2f}', S.label_for(sm["avg_before"])),
        card("Average score after", f'{sm["avg_after"]:.2f}', S.label_for(sm["avg_after"])),
        card("Improvement", _delta_html(sm["delta"], " pts"), f"points{rel}"),
        card(f'Pages at {sm["target"]:.0f}+', f'{sm["target_after"]} <span class="s">of {n}</span>',
             f'was {sm["target_before"]} before'),
        card("Rated Excellent (97+)", f'{sm["excellent_after"]}', f"of {n} pages"),
        card("Pages with critical issues", f'{len(sm["critical_pages"])}',
             "see the critical issues section"),
        card("Scores that went down", f'{len(sm["decreased"])}', "should be reviewed" if sm["decreased"] else "none"),
    ]
    if cost is not None:                                   # only when "Show cost in the report" is switched on
        total, pages = cost
        per = f"about ${total / pages:.4f} per page" if pages else "this run"
        cards.append(card("AI cost of this run", f"${total:,.2f}", per))
    return '<div class="cards">' + "".join(cards) + "</div>"


def category_table(sm: dict) -> str:
    if not sm or not sm.get("pages"):
        return ""
    rows = []
    for c in sm["categories"]:
        pb, pa = c["before"] / c["max"] * 100, c["after"] / c["max"] * 100
        rows.append(f'<tr><td>{e(c["name"])}</td><td class="n">{c["before"]:.2f} / {c["max"]}</td>'
                    f'<td class="n">{c["after"]:.2f} / {c["max"]}</td><td class="n">{_delta_html(c["change"])}</td>'
                    f'<td><div class="bar"><i style="width:{pa:.0f}%;background:var(--blue)"></i></div></td></tr>')
    tot = (f'<tr><th>Total</th><th class="n">{sm["avg_before"]:.2f} / 100</th>'
           f'<th class="n">{sm["avg_after"]:.2f} / 100</th><th class="n">{_delta_html(sm["delta"])}</th><th></th></tr>')
    return ('<table><thead><tr><th>Category</th><th class="n">Before</th><th class="n">After</th>'
            '<th class="n">Change</th><th>After (share of maximum)</th></tr></thead><tbody>'
            + "".join(rows) + tot + "</tbody></table>")


def category_chart(sm: dict) -> str:
    if not sm or not sm.get("pages"):
        return ""
    rows = sm["categories"]
    w, h_row, left = 520, 38, 205
    h = len(rows) * h_row + 30
    bw = w - left - 50
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Score by category, before and after">']
    for i, c in enumerate(rows):
        y = i * h_row + 6
        pb, pa = c["before"] / c["max"], c["after"] / c["max"]
        name = c["name"].replace(" & ", " &amp; ")
        out.append(f'<text x="0" y="{y + 15}" font-size="12" fill="#1b2333">{e(c["name"])}</text>')
        out.append(f'<rect x="{left}" y="{y}" width="{bw * pb:.0f}" height="12" rx="3" fill="#aab3c9"/>')
        out.append(f'<text x="{left + bw * pb + 5:.0f}" y="{y + 10}" font-size="10" fill="#566079">{c["before"]:.2f}</text>')
        out.append(f'<rect x="{left}" y="{y + 15}" width="{bw * pa:.0f}" height="12" rx="3" fill="#1f5fd1"/>')
        out.append(f'<text x="{left + bw * pa + 5:.0f}" y="{y + 25}" font-size="10" fill="#1f5fd1">{c["after"]:.2f}</text>')
    ly = h - 14
    out.append(f'<rect x="{left}" y="{ly}" width="10" height="10" fill="#aab3c9"/><text x="{left + 14}" y="{ly + 9}" font-size="11">Before</text>')
    out.append(f'<rect x="{left + 70}" y="{ly}" width="10" height="10" fill="#1f5fd1"/><text x="{left + 84}" y="{ly + 9}" font-size="11">After</text>')
    out.append("</svg>")
    return "".join(out)


def distribution_chart(sm: dict) -> str:
    if not sm or not sm.get("pages"):
        return ""
    labels = [n for _, n in S.LABELS]
    mx = max([1] + list(sm["dist_before"].values()) + list(sm["dist_after"].values()))
    w, h, base, colw = 480, 230, 190, 480 // 5
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Pages per rating, before and after">']
    for i, name in enumerate(labels):
        x = i * colw + 12
        for j, (key, fill) in enumerate((("dist_before", "#aab3c9"), ("dist_after", LABEL_COLOR[name]))):
            v = sm[key][name]
            bh = (base - 20) * v / mx
            bx = x + j * 36
            out.append(f'<rect x="{bx}" y="{base - bh:.0f}" width="30" height="{bh:.0f}" rx="3" fill="{fill}"/>')
            out.append(f'<text x="{bx + 15}" y="{base - bh - 4:.0f}" font-size="11" text-anchor="middle" fill="#1b2333">{v}</text>')
        out.append(f'<text x="{x + 33}" y="{base + 16}" font-size="10.5" text-anchor="middle" fill="#1b2333">{e(name)}</text>')
    out.append(f'<text x="12" y="{h - 4}" font-size="10.5" fill="#566079">grey = before &nbsp; coloured = after</text></svg>')
    return "".join(out)


def critical_html(sm: dict, items: list) -> str:
    rows = []
    for rel, crit in (sm or {}).get("critical_pages", []):
        rows.append(f'<tr><td>{e(rel)}</td><td>{"; ".join(e(S.CRIT_TEXT.get(c, c)) for c in crit)}</td></tr>')
    if not rows:
        return '<p class="ok">No critical issues were found on the scored pages.</p>'
    return ('<table><thead><tr><th>Page</th><th>Issue</th></tr></thead><tbody>' + "".join(rows[:300]) + "</tbody></table>"
            + ('<p class="note">… and more (see the CSV export).</p>' if len(rows) > 300 else ""))


def top_issues_html(sm: dict) -> str:
    rows = []
    for t in (sm or {}).get("top_issues", []):
        badge = '<span class="badge">fix on the page</span>' if t["manual"] else ""
        rows.append(f'<tr><td>{e(t["label"])}{badge}</td><td class="n">{t["pages"]}</td></tr>')
    if not rows:
        return '<p class="ok">Nothing left to fix on the scored checks.</p>'
    return ('<table><thead><tr><th>Check</th><th class="n">Pages</th></tr></thead><tbody>' + "".join(rows)
            + '</tbody></table><p class="note">“Fix on the page” = outside what this tool can change '
              '(for example the canonical tag or a missing image file).</p>')


def best_gains_html(sm: dict) -> str:
    g = (sm or {}).get("best_gains", [])
    if not g:
        return '<p class="note">No data.</p>'
    rows = "".join(f'<tr><td>{e(r)}</td><td class="n">{b:.0f} → {a:.0f}</td><td class="n">{_delta_html(d)}</td></tr>'
                   for d, r, b, a in g)
    return '<table><thead><tr><th>Page</th><th class="n">Score</th><th class="n">Change</th></tr></thead><tbody>' + rows + "</tbody></table>"


def pages_table(items: list, limit_details: int = 8) -> str:
    rows = []
    for it in sorted([i for i in items if i.get("before") and i.get("after")],
                     key=lambda i: i["after"]["t"] - i["before"]["t"]):
        b, a = it["before"], it["after"]
        d = a["t"] - b["t"]
        fails = [r for r in S.explain(a) if r["ok"] is False][:limit_details]
        detail = ""
        if fails:
            lis = "".join(f'<li>{e(r["label"])} — {r["earned"]:g}/{r["max"]}'
                          + (f' ({e(r["reason"])})' if r["reason"] else "") + ("  [fix on the page]" if r["manual"] else "") + "</li>"
                          for r in fails)
            detail = f'<details><summary>{len(fails)} remaining</summary><ul>{lis}</ul></details>'
        crit = "; ".join(e(S.CRIT_TEXT.get(c, c)) for c in a.get("crit", []))
        rows.append(f'<tr><td>{e(it["rel"])}</td><td class="n">{b["t"]:.0f}</td><td class="n">{a["t"]:.0f}</td>'
                    f'<td class="n">{_delta_html(d)}</td><td>{_pill(a["l"])}</td><td>{crit}</td><td>{detail}</td></tr>')
    if not rows:
        return '<p class="note">No pages.</p>'
    return ('<div class="pages-table"><table><thead><tr><th>Page</th><th class="n">Before</th><th class="n">After</th>'
            '<th class="n">Change</th><th>Rating</th><th>Critical</th><th>What is left</th></tr></thead><tbody>'
            + "".join(rows) + "</tbody></table></div>")


def not_scored_html(not_scored: list, review_items: list) -> str:
    rows = []
    for rel, status, reason in not_scored:
        rows.append(f'<tr><td>{e(rel)}</td><td>{e(status)}</td><td>{e(reason)}</td></tr>')
    for it in review_items:
        rows.append(f'<tr><td>{e(it["rel"])}</td><td>needs review</td><td>Not changed — before score only '
                    f'({it["before"]["t"]:.0f}/100)</td></tr>')
    if not rows:
        return '<p class="ok">Every page was scored.</p>'
    return ('<table><thead><tr><th>Page</th><th>Status</th><th>Reason</th></tr></thead><tbody>'
            + "".join(rows[:1000]) + "</tbody></table>"
            + ('<p class="note">… list shortened.</p>' if len(rows) > 1000 else ""))


METHOD = """
<p>The score is an <strong>internal benchmark</strong> created by Top Ten Website Marketing. It is
<strong>not a Google score</strong> — Google does not publish a universal 100-point on-page score.</p>
<p>Every page is scored out of 100 with the <em>same rules before and after</em> optimisation, using seven categories:
meta title (15), meta description (15), H1 and on-page relevance (20), canonical and robots (10),
Open Graph and social tags (10), structured data (15) and content relevance and consistency (15).
Each category is made of small checks. Most are verified by code; {AI_NOTE}</p>
<p>Ratings: 97–100 Excellent · 90–96 Very good · 75–89 Good · 50–74 Needs improvement · below 50 Poor.
Serious problems (for example an accidental noindex or a canonical pointing to another website) also cap the score.
The site figures are the arithmetic mean of the individual page scores.</p>
"""


def build_report(summary: dict, items: list, not_scored: list, meta: dict, template_path=None) -> str:
    tpl = Path(template_path or _tpl_path()).read_text(encoding="utf-8")
    if "{{REPORT_JSON}}" in tpl:                       # the dynamic template: one JSON block drives everything
        return build_dynamic(tpl, summary, items, not_scored, meta)
    tpl = re.sub(r"<!--\s*DEFAULT REPORT TEMPLATE.*?-->", "", tpl, flags=re.S)       # author notes stay out of the output
    review = [i for i in items if i.get("before") and not i.get("after")]
    deep = bool(summary and summary.get("use_ai"))
    ai_note = (f"the search-intent and wording checks were judged by AI on {summary.get('ai_pages', 0)} of "
               f"{summary.get('pages', 0)} pages (Deep quality check ON; pages without AI marks are rescaled)."
               if deep else "the AI-judged checks (search intent, wording quality) were NOT used in this run, so the "
                            "available points were rescaled to 100.")
    values = {
        "TITLE": f"SEO Content Quality Report — {meta.get('site') or meta.get('brand') or ''}",
        "SITE": meta.get("site") or meta.get("brand") or "",
        "BRAND": meta.get("brand", ""),
        "DATE": meta.get("date") or datetime.now().strftime("%d %B %Y"),
        "RUN_MODE": "Preview (dry run — nothing was changed)" if meta.get("dry") else "Pages updated",
        "SCORE_MODE": "Deep AI quality check" if deep else "Free checks only",
        "SCORING_VERSION": S.SCORING_VERSION,
        "TARGET": f'{(summary or {}).get("target", 90):.0f}',
        "SUMMARY_CARDS": cards_html(summary, (meta["cost"], max(1, len(items))) if meta.get("show_cost") and meta.get("cost") is not None else None),
        "CATEGORY_TABLE": category_table(summary),
        "CATEGORY_CHART": category_chart(summary),
        "DISTRIBUTION_CHART": distribution_chart(summary),
        "CRITICAL": critical_html(summary, items),
        "TOP_ISSUES": top_issues_html(summary),
        "BEST_GAINS": best_gains_html(summary),
        "PAGES_TABLE": pages_table(items),
        "NOT_SCORED": not_scored_html(not_scored, review),
        "METHOD": METHOD.replace("{AI_NOTE}", ai_note),
        "CSS": CSS,
    }
    out = tpl
    for k, v in values.items():
        out = out.replace("{{" + k + "}}", v if k in _RAW else e(v))
    return out


# values that already are HTML (everything else is escaped when substituted)
_RAW = {"SUMMARY_CARDS", "CATEGORY_TABLE", "CATEGORY_CHART", "DISTRIBUTION_CHART", "CRITICAL", "TOP_ISSUES",
        "BEST_GAINS", "PAGES_TABLE", "NOT_SCORED", "METHOD", "CSS"}


def write_report(path, summary, items, not_scored, meta, template_path=None) -> None:
    Path(path).write_text(build_report(summary, items, not_scored, meta, template_path), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Dynamic template: everything the page shows comes from one JSON block
# ─────────────────────────────────────────────────────────────────────────────
import json  # noqa: E402

CAT_SHORT = {"title": "Meta Title", "desc": "Meta Desc", "h1": "H1 & On-Page", "canon": "Canonical",
             "social": "Open Graph", "schema": "Schema", "content": "Content"}
AGENCY = "Top Ten Website Marketing"


def _plain(s) -> str:
    return " ".join(_html.unescape(str(s or "")).split())


def _changes_text(it: dict, old_t: str, new_t: str, old_d: str, new_d: str) -> str:
    ch = it.get("chg") or {}
    bits = []
    if old_t != new_t:
        bits.append("title updated")
    if old_d != new_d:
        bits.append("description rewritten")
    if ch.get("h"):
        bits.append("H1 improved")
    if ch.get("c") in ("added", "fixed"):
        bits.append(f"canonical tag {ch['c']}")
    if ch.get("d"):
        bits.append(f"{ch['d']} extra H1 changed to H2")
    if ch.get("b"):
        bits.append("breadcrumb markup added")
    if ch.get("f"):
        bits.append(f"FAQ markup ({ch['f']} questions)")
    if ch.get("s") in ("added", "merged"):
        bits.append(f"schema {ch['s']}")
    if ch.get("o"):
        bits.append("social tags updated")
    if ch.get("i"):
        bits.append(f"{ch['i']} image fix(es)")
    return (", ".join(bits).capitalize() + ".") if bits else "No changes were needed."


def report_data(summary: dict, items: list, not_scored: list, meta: dict) -> dict:
    """Plain, JSON-safe data for the template. Never contains file-system paths or API cost unless asked."""
    sm = summary or {}
    both = [i for i in items if i.get("before") and i.get("after")]
    before_only = [i for i in items if i.get("before") and not i.get("after")]
    n = len(both)
    target = sm.get("target", 90)

    keep = set(meta.get("partial_cats") or [c for c, _, _ in S.CATS])      # estimated reports score only some categories
    pages, titles_b, titles_a, descs_b, descs_a = [], [], [], [], []
    cnt = {"titles": 0, "descriptions": 0, "h1": 0, "schema": 0, "og": 0, "images": 0, "images_total": 0,
           "canonical": 0}
    for it in both:
        b, a = it["before"], it["after"]
        old_t, new_t = _plain(it.get("old_title")), _plain(it.get("new_title"))
        old_d, new_d = _plain(it.get("old_description")), _plain(it.get("new_description"))
        ch = it.get("chg") or {}
        titles_b.append(len(old_t)); titles_a.append(len(new_t))
        descs_b.append(len(old_d)); descs_a.append(len(new_d))
        cnt["titles"] += old_t != new_t
        cnt["descriptions"] += old_d != new_d
        cnt["h1"] += bool(ch.get("h"))
        cnt["schema"] += ch.get("s") in ("added", "merged")
        cnt["canonical"] += ch.get("c") in ("added", "fixed")
        cnt["og"] += bool(ch.get("o"))
        cnt["images"] += 1 if ch.get("i") else 0
        cnt["images_total"] += int(ch.get("i") or 0)
        # "gain" = the points that would be recovered if this item were fixed; biggest recoveries first
        remaining = sorted(
            ({"label": r["label"], "earned": round(r["earned"], 2), "max": r["max"],
              "gain": round((r["max"] - r["earned"]) * a.get("sc", 1.0), 2), "reason": r["reason"], "manual": r["manual"]}
             for r in S.explain(a) if r["ok"] is False),
            key=lambda x: -x["gain"])[:15]
        res = it.get("result") or {}
        pages.append({
            "rel": it["rel"], "name": ch.get("n") or res.get("page_name", ""),
            "before": round(b["t"], 2), "after": round(a["t"], 2), "change": round(a["t"] - b["t"], 2),
            "rating": a["l"], "rating_before": b["l"],
            "critical": [S.CRIT_TEXT.get(c, c) for c in a.get("crit", [])],
            "remaining": remaining,
            "old_title": old_t, "new_title": new_t, "old_desc": old_d, "new_desc": new_d,
            "old_h1": _plain(ch.get("oh") or res.get("old_h1")), "new_h1": _plain(ch.get("nh") or res.get("new_h1")),
            "title_len": len(new_t), "desc_len": len(new_d),
            "notes": [x for x in (res.get("notes") or []) if "file" not in x.lower()][:6],
            "changes_text": _changes_text(it, old_t, new_t, old_d, new_d),
            "cb": [round(b["cats"][c], 2) for c, _, _ in S.CATS if c in keep], "ca": [round(a["cats"][c], 2) for c, _, _ in S.CATS if c in keep],
        })

    pct = lambda k: f"{k / len(both) * 100:.1f}% of scored pages" if both else "no scored pages"
    delta = sm.get("delta", 0.0)
    crit_real = [p for p in pages if any(c != S.CRIT_TEXT["decreased"] for c in p["critical"])]
    cards = [
        {"label": "Meta titles updated", "value": str(cnt["titles"]), "foot": pct(cnt["titles"])},
        {"label": "Descriptions updated", "value": str(cnt["descriptions"]), "foot": pct(cnt["descriptions"])},
        {"label": "H1 tags improved", "value": str(cnt["h1"]), "foot": pct(cnt["h1"])},
        {"label": "Schema added / merged", "value": str(cnt["schema"]), "foot": pct(cnt["schema"])},
        {"label": "Overall score", "value": (f'{sm.get("avg_before", 0):.2f} \u2192 {sm.get("avg_after", 0):.1f}' if n else "\u2013"),
         "foot": (f'{delta:+.2f} point change' if n else "no scored pages"),
         "tone": ("green" if delta > 0.004 else ("red" if delta < -0.004 else ""))},
        {"label": f"Pages at {target:.0f}+", "value": f'{sm.get("target_after", 0)} / {n}',
         "foot": f'was {sm.get("target_before", 0)} before', "tone": "green" if sm.get("target_after", 0) == n and n else ""},
        {"label": "Critical issues", "value": str(len(crit_real)),
         "foot": "pages need attention" if crit_real else "none found", "tone": "red" if crit_real else "green"},
        {"label": "Scores that went down", "value": str(len(sm.get("decreased", []))),
         "foot": "see the table below" if sm.get("decreased") else "none", "tone": "amber" if sm.get("decreased") else "green"},
    ]
    show_cost = bool(meta.get("show_cost")) and meta.get("cost") is not None
    if show_cost:
        total = float(meta["cost"])
        total_pages = len(items) + len(not_scored)
        cards.append({"label": "AI cost of this run", "value": f"${total:,.2f}",
                      "foot": f"about ${total / total_pages:.4f} per page" if total_pages else "this run"})

    not_scored_rows = [{"rel": r, "status": s, "reason": why} for r, s, why in not_scored]
    for it in before_only:
        not_scored_rows.append({"rel": it["rel"], "status": "needs review",
                                "reason": f"Not changed — before score {it['before']['t']:.0f}/100"})

    deep = bool(sm.get("use_ai"))
    ai_note = (f"The search-intent and wording checks were judged by AI on {sm.get('ai_pages', 0)} of {n} pages "
               f"(Deep quality check ON; pages without AI marks are rescaled)." if deep else
               "The AI-judged checks (search intent, wording quality) were not used in this run, so the available "
               "points were rescaled to 100. Most checks are verified by code.")
    now = datetime.now()
    mx = lambda xs: max(xs) if xs else 0
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else 0
    return {
        "meta": {
            "agency": AGENCY, "client": meta.get("brand") or meta.get("site") or "", "site": meta.get("site", ""),
            "date": meta.get("date") or now.strftime("%d %B %Y"), "run_id": meta.get("run_id") or now.strftime("%Y%m%d_%H%M%S"),
            "engine": meta.get("engine") or "SEO Meta Tag Updater", "dry": bool(meta.get("dry")),
            "score_mode": "Deep AI quality check" if deep else "Free checks (code only)",
            "scoring_version": S.SCORING_VERSION, "ai_note": ai_note, "estimate_note": meta.get("estimate_note") or "",
            "canonical_step": bool(meta.get("canonical_step")), "csv_file": meta.get("csv_file") or "",
        },
        "summary": {
            "pages": n, "processed": n + len(before_only), "pages_total": n + len(before_only) + len(not_scored), "not_scored": len(before_only) + len(not_scored),
            "avg_before": sm.get("avg_before", 0.0), "avg_after": sm.get("avg_after", 0.0), "delta": delta,
            "relative_pct": sm.get("relative_pct"), "label_after": S.label_for(sm.get("avg_after", 0.0)) if n else "n/a",
            "target": target, "target_before": sm.get("target_before", 0), "target_after": sm.get("target_after", 0),
            "critical": len(crit_real), "decreased": len(sm.get("decreased", [])),
        },
        "changes": cnt,
        "cards": cards,
        "categories": [{"id": c["id"], "name": c["name"], "short": CAT_SHORT[c["id"]], "max": c["max"],
                        "before": c["before"], "after": c["after"], "change": c["change"]}
                       for c in sm.get("categories", []) if c["id"] in keep],
        "limits": {"pages": n, "title_rec": S.C.MAX_TITLE_LEN, "desc_rec": S.C.MAX_DESC_LEN,
                   "title_before": mx(titles_b), "title_after": mx(titles_a),
                   "title_avg_before": avg(titles_b), "title_avg_after": avg(titles_a),
                   "desc_avg_before": avg(descs_b), "desc_avg_after": avg(descs_a),
                   "desc_before": mx(descs_b), "desc_after": mx(descs_a),
                   "title_over_before": sum(1 for x in titles_b if x > S.C.MAX_TITLE_LEN),
                   "title_over_after": sum(1 for x in titles_a if x > S.C.MAX_TITLE_LEN),
                   "desc_over_before": sum(1 for x in descs_b if x > S.C.MAX_DESC_LEN),
                   "desc_over_after": sum(1 for x in descs_a if x > S.C.MAX_DESC_LEN)},
        "distribution": {"labels": [name for _, name in S.LABELS],
                         "before": [sm.get("dist_before", {}).get(name, 0) for _, name in S.LABELS],
                         "after": [sm.get("dist_after", {}).get(name, 0) for _, name in S.LABELS]},
        "top_issues": sm.get("top_issues", []),
        "best": [{"rel": r, "before": b, "after": a, "change": d} for d, r, b, a in sm.get("best_gains", []) if d > 0.004],
        "not_scored": not_scored_rows,
        "pages": pages,
    }


def _json_for_html(data: dict) -> str:
    """JSON that is safe inside <script type="application/json">."""
    txt = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (txt.replace("</", "<\\/").replace("<!--", "<\\u0021--").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def build_dynamic(tpl: str, summary, items, not_scored, meta) -> str:
    data = report_data(summary, items, not_scored, meta)
    title = f"SEO & AI Optimization Report | {AGENCY}"
    tpl = re.sub(r"<!--\s*DYNAMIC REPORT TEMPLATE.*?-->", "", tpl, flags=re.S)       # author notes stay out of the output
    out = tpl.replace("{{TITLE}}", e(title))
    return out.replace("{{REPORT_JSON}}", _json_for_html(data))
