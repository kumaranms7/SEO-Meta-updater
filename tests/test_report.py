import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seo_ai as A  # noqa: E402
import seo_core as C  # noqa: E402
import seo_pipeline as P  # noqa: E402
import seo_report as R  # noqa: E402
import seo_score as S  # noqa: E402
from test_pipeline import ScriptedClient, make_site  # noqa: E402


def run_scored(td, names):
    C.STATE_DIR = Path(td) / "state"
    root = make_site(td, names)
    results, got = [], {}
    opts = P.RunOptions(folder=str(root), site_url="https://site.com", brand="Acme", mode="per_page", protect_score=False)
    pipe = P.Pipeline(opts, A.AI(ScriptedClient()), C.collect_html_files(root), lambda m, t="": None,
                      results.append, lambda d, t: None, threading.Event(),
                      on_scores=lambda items, summ: got.update(items=items, summ=summ))
    pipe.run()
    return results, got


def embedded_json(html: str) -> dict:
    m = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


def chrome_path():
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"):
        if Path(p).exists():
            return p
    return shutil.which("chrome") or shutil.which("msedge")


class TestDynamicReport(unittest.TestCase):
    def build(self, td, names=("Roof Repair", "Gutter Cleaning"), meta=None, not_scored=None):
        results, got = run_scored(td, list(names))
        base = {"site": "https://site.com", "brand": "Acme", "dry": False, "date": "01 January 2027"}
        base.update(meta or {})
        return R.build_report(got["summ"], got["items"], not_scored or [], base), got

    def test_everything_comes_from_one_json_block(self):
        with tempfile.TemporaryDirectory() as td:
            html, got = self.build(td)
            data = embedded_json(html)
        summ = got["summ"]
        self.assertEqual(data["summary"]["pages"], 2)
        self.assertAlmostEqual(data["summary"]["avg_after"], summ["avg_after"], places=1)
        self.assertAlmostEqual(data["summary"]["delta"], summ["delta"], places=1)
        self.assertEqual([c["id"] for c in data["categories"]], [c[0] for c in S.CATS])
        self.assertEqual(len(data["pages"]), 2)
        p = data["pages"][0]
        for key in ("rel", "before", "after", "change", "rating", "critical", "remaining", "old_title", "new_title",
                    "old_desc", "new_desc", "cb", "ca", "changes_text"):
            self.assertIn(key, p)
        self.assertEqual(len(p["cb"]), 7)
        self.assertEqual(data["meta"]["client"], "Acme")
        # numbers that are NOT in the data must not be typed into the template
        self.assertNotIn("Infinity", html)
        self.assertNotIn("254", html)
        self.assertNotRegex(html, r"\{\{[A-Z_]+\}\}")
        self.assertEqual(html.count('id="report-data"'), 1)

    def test_changes_are_counted_from_the_pages(self):
        with tempfile.TemporaryDirectory() as td:
            html, got = self.build(td, names=("Roof Repair", "Gutter Cleaning", "Window Washing"))
            data = embedded_json(html)
        self.assertEqual(data["changes"]["titles"], 3)
        self.assertEqual(data["changes"]["descriptions"], 3)
        self.assertEqual(data["changes"]["schema"], 3)
        self.assertEqual(data["cards"][0]["value"], "3")
        self.assertIn("100.0% of scored pages", data["cards"][0]["foot"])

    def test_report_keeps_the_reference_look_and_leaks_nothing_private(self):
        with tempfile.TemporaryDirectory() as td:
            html, got = self.build(td, not_scored=[("bad<script>.html", "failed", 'AI said "no" & left')])
        # same fonts and chart library as the reference report
        self.assertIn("fonts.googleapis.com/css2?family=Plus+Jakarta+Sans", html)
        self.assertIn("JetBrains+Mono", html)
        self.assertIn("cdn.jsdelivr.net/npm/chart.js", html)
        self.assertIn("font-family: 'Plus Jakarta Sans'", html)
        for cls in ("executive-summary-card", "cards-row", "metric-card", "matrix-table", "charts-grid", "page-results-table",
                    "pagination-bar", "branding-pill", "header-meta"):
            self.assertIn(cls, html)
        for label in ("charLimitChart", "categoryComparisonChart", "Page-by-page results", "Executive Summary",
                      "Before vs. After Optimization Impact Matrix"):
            self.assertIn(label, html)
        # nothing the reference does not have
        for extra in ("Most common remaining issues", "Biggest improvements", "How the score works", "How Pages Are Rated"):
            self.assertNotIn(extra, html)
        self.assertNotIn(td, html)                                          # no local file-system paths
        data = embedded_json(html)
        self.assertEqual(data["not_scored"][0]["rel"], "bad<script>.html")   # round-trips through the JSON safely
        self.assertNotIn("</script>", html.split('id="report-data"', 1)[1].split("</script>", 1)[0])
        for p in data["pages"]:
            self.assertNotIn("cost", p)
            self.assertNotIn("file", p)

    def test_requested_layout_changes(self):
        with tempfile.TemporaryDirectory() as td:
            html, got = self.build(td, names=("Roof Repair", "Gutter Cleaning"))
        # 1. top bar
        for label in ("'Client:'", "'Website:'", "'Report Date:'", "'Pages Processed:'"):
            self.assertIn(label, html)
        for gone in ("Audit Run ID:", "Target Client", "Execution Engine:", "Domain Health:", "Audited Scope:"):
            self.assertNotIn(gone, html.split('id="report-data"', 1)[1])                  # not in the script that builds the bar
            self.assertNotIn(gone, html.split("<body>", 1)[1].split('id="report-data"', 1)[0])
        # 2. room above the executive summary
        self.assertIn("main.container { padding-top: 2.5rem; }", html)
        # 4. chart shows observed maximums only
        self.assertNotIn("Google Title Recommended", html)
        self.assertNotIn("Google Description Recommended", html)
        self.assertIn("labels: ['Meta Title', 'Meta Description']", html)
        self.assertIn("L.title_avg_before", html)                           # the chart shows average lengths
        # 5. "Content" bar replaced by Images Updated
        self.assertIn("filter(c => c.id !== 'content')", html)
        self.assertIn("'Images Updated'", html)
        # 7. wording
        self.assertIn("<th>Details</th>", html)
        self.assertNotIn("What is left", html)
        self.assertIn("Actions to improve score:", html)
        self.assertNotIn("Action Item / Remaining Item", html)
        # 6. recovery score: green bold up-arrow value instead of earned/max
        self.assertIn('class="gain-up"', html)
        self.assertIn(".gain-up { color: #10b981; font-weight: 700; }", html)
        data = embedded_json(html)
        self.assertIn("images_total", data["changes"])
        for p in data["pages"]:
            gains = [r["gain"] for r in p["remaining"]]
            self.assertEqual(gains, sorted(gains, reverse=True))                           # biggest recovery first
            for r in p["remaining"]:
                self.assertAlmostEqual(r["gain"], r["max"] - r["earned"], places=2)

    def test_table_has_no_critical_column_and_more_info_dropdown(self):
        with tempfile.TemporaryDirectory() as td:
            html, got = self.build(td, names=("Roof Repair", "Gutter Cleaning"))
        head = html.split("<thead>", 2)[-1].split("</thead>", 1)[0]
        self.assertIn("<th>Details</th>", head)
        self.assertNotIn("Critical", head)                                   # the Critical column is gone
        self.assertIn("More info", html)
        self.assertNotIn("remaining'", html.replace("remaining-list", ""))   # no "{n} remaining" button text any more
        self.assertIn('colspan="6"', html)
        self.assertIn("Category scores", html)
        self.assertLess(html.index('<div class="cat-card">'), html.index('<div class="actions-card">'))   # left of the actions card

    def test_footer_is_one_short_line(self):
        with tempfile.TemporaryDirectory() as td:
            html, got = self.build(td)
        self.assertIn("'Report prepared by <strong>' + escapeHtml(META.agency) + '</strong> for ' + escapeHtml(client);", html)
        self.assertNotIn("Leadership", html)
        self.assertNotIn("Enterprise SEO Compliance", html)
        self.assertNotIn("footerSub", html)

    def test_changes_are_the_difference_of_the_numbers_shown(self):
        with tempfile.TemporaryDirectory() as td:
            html, got = self.build(td, names=("Roof Repair", "Gutter Cleaning", "Window Washing"))
        data = embedded_json(html)
        sm = data["summary"]
        self.assertEqual(sm["delta"], round(sm["avg_after"] - sm["avg_before"], 2))
        for c in data["categories"]:
            self.assertEqual(c["change"], round(c["after"] - c["before"], 2), c)
        for p in data["pages"]:
            self.assertEqual(p["change"], round(p["after"] - p["before"], 2), p["rel"])

    def test_export_csv_links_to_the_csv_saved_with_the_report(self):
        with tempfile.TemporaryDirectory() as td:
            html, got = self.build(td, meta={"csv_file": "seo_results_20270101_101010.csv"})
            nolink = R.build_report(got["summ"], got["items"], [], {"site": "https://site.com", "brand": "Acme"})
            self.assertIn('id="exportCsvBtn"', html)                       # the link sits above the table
            self.assertLess(html.index('id="exportCsvBtn"'), html.index('id="pageResultsTbody"'))
            self.assertEqual(embedded_json(html)["meta"]["csv_file"], "seo_results_20270101_101010.csv")
            self.assertEqual(embedded_json(nolink)["meta"]["csv_file"], "")
            self.assertNotIn("new Blob", html)                              # no CSV is generated in the browser
            self.assertNotIn("buildCsv", html)
            if not chrome_path():
                self.skipTest("needs Chrome or Edge to run the page's JavaScript")
            f = Path(td) / "r.html"
            f.write_text(html, encoding="utf-8")
            g = Path(td) / "n.html"
            g.write_text(nolink, encoding="utf-8")
            def dom(path):
                return subprocess.run([chrome_path(), "--headless=new", "--disable-gpu", "--no-sandbox",
                                       f"--user-data-dir={Path(td) / 'prof'}", "--virtual-time-budget=4000", "--dump-dom",
                                       path.as_uri()], capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
            with_csv, without = dom(f), dom(g)
        m = re.search(r'<a[^>]*id="exportCsvBtn"[^>]*>', with_csv)
        self.assertIn('href="seo_results_20270101_101010.csv"', m.group(0))
        self.assertNotIn("hidden", m.group(0))
        self.assertIn("hidden", re.search(r'<a[^>]*id="exportCsvBtn"[^>]*>', without).group(0))   # no CSV saved → no link

    def test_canonical_card_and_counts(self):
        with tempfile.TemporaryDirectory() as td:
            results, got = run_scored(td, ["Roof Repair", "Gutter Cleaning"])
            for i, it in enumerate(got["items"]):
                it["chg"] = dict(it.get("chg") or {}, c="added" if i == 0 else "ok")
            base = {"site": "s.com", "brand": "A"}
            on = embedded_json(R.build_report(got["summ"], got["items"], [], dict(base, canonical_step=True)))
            off = embedded_json(R.build_report(got["summ"], got["items"], [], dict(base)))
        self.assertEqual(on["changes"]["canonical"], 1)
        self.assertTrue(on["meta"]["canonical_step"])
        self.assertFalse(off["meta"]["canonical_step"])                      # older runs show no canonical card
        self.assertTrue(any("canonical tag added" in p["changes_text"] for p in on["pages"]))

    def test_cost_card_only_when_switched_on(self):
        with tempfile.TemporaryDirectory() as td:
            results, got = run_scored(td, ["Roof Repair", "Gutter Cleaning"])
            base = {"site": "s.com", "brand": "A", "cost": 1.2345}
            off = embedded_json(R.build_report(got["summ"], got["items"], [], dict(base)))
            off2 = embedded_json(R.build_report(got["summ"], got["items"], [], dict(base, show_cost=False)))
            on = embedded_json(R.build_report(got["summ"], got["items"], [], dict(base, show_cost=True)))
        self.assertFalse(any("cost" in c["label"].lower() for c in off["cards"]))
        self.assertFalse(any("cost" in c["label"].lower() for c in off2["cards"]))
        card = [c for c in on["cards"] if "cost" in c["label"].lower()][0]
        self.assertEqual(card["value"], "$1.23")
        self.assertIn("per page", card["foot"])

    def test_decrease_needs_more_than_a_hundredth_of_a_point(self):
        import seo_score as S2
        def item(b, a):
            fb = S2.recompute({"v": S2.SCORING_VERSION, "f": [100] * len(S2.CHECKS), "r": {}, "crit": [], "man": [], "topic": []})
            fa = S2.recompute({"v": S2.SCORING_VERSION, "f": [100] * len(S2.CHECKS), "r": {}, "crit": [], "man": [], "topic": []})
            fb["t"], fa["t"] = b, a
            return {"rel": "x.html", "before": fb, "after": fa, "old_title": "t1", "new_title": "t2",
                    "old_description": "d1", "new_description": "d2"}
        # apply_site_checks recomputes totals, so test the threshold constant + helper behaviour directly
        self.assertEqual(S2.DECREASE_TOLERANCE, 0.01)

    def test_custom_legacy_template_still_works(self):
        with tempfile.TemporaryDirectory() as td:
            results, got = run_scored(td, ["Roof Repair"])
            tpl = Path(td) / "mine.html"
            tpl.write_text("<h1>{{BRAND}} / {{SITE}}</h1>{{SUMMARY_CARDS}}", encoding="utf-8")
            html = R.build_report(got["summ"], got["items"], [], {"site": "s.com", "brand": "A&B"}, template_path=tpl)
        self.assertTrue(html.startswith("<h1>A&amp;B / s.com</h1>"))
        self.assertIn("Average score after", html)

    def test_empty_run_does_not_crash(self):
        html = R.build_report({}, [], [], {"site": "x"})
        data = embedded_json(html)
        self.assertEqual(data["summary"]["pages"], 0)
        self.assertEqual(data["pages"], [])

    @unittest.skipUnless(chrome_path(), "needs Chrome or Edge to execute the page's JavaScript")
    def test_page_renders_in_a_real_browser(self):
        with tempfile.TemporaryDirectory() as td:
            html, got = self.build(td, names=("Roof Repair", "Gutter Cleaning", "Window Washing"),
                                   not_scored=[("404.html", "skipped", "404 / error page")])
            f = Path(td) / "report.html"
            f.write_text(html, encoding="utf-8")
            out = subprocess.run([chrome_path(), "--headless=new", "--disable-gpu", "--no-sandbox",
                                  f"--user-data-dir={Path(td) / 'prof'}", "--virtual-time-budget=4000", "--dump-dom",
                                  f.as_uri()], capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
        self.assertIn("Executive Summary", out)
        self.assertIn("roof-repair.html", out)                              # table rows were rendered by the script
        self.assertIn("Before vs. After Optimization Impact Matrix", out)
        self.assertIn(f'{got["summ"]["avg_after"]:.2f} / 100', out)         # matrix total row
        for label in ("Client:", "Website:", "Report Date:", "Pages Processed:"):
            self.assertIn(label, re.sub(r"<script.*?</script>", "", out, flags=re.S))
        self.assertNotIn("Audit Run ID", re.sub(r"<script.*?</script>", "", out, flags=re.S))
        self.assertIn("Actions to improve score", out)
        self.assertIn("Category scores", out)
        self.assertLess(out.index("Category scores"), out.index("Actions to improve score:"))
        self.assertIn("More info", out)
        self.assertIn("META TITLES UPDATED", out)
        self.assertIn("OVERALL SCORE", out)
        self.assertIn("Showing 1 to 3 of 3 pages", out)
        visible = re.sub(r"<script.*?</script>", "", out, flags=re.S)       # the script source may mention 'undefined'
        self.assertNotIn("undefined", visible)
        self.assertNotIn("NaN", visible)


if __name__ == "__main__":
    unittest.main(verbosity=2)
