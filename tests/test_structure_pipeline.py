"""One H1 per page, breadcrumb and FAQ markup inside a real run."""
import json
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
import test_protect as TP  # noqa: E402
import test_score as T  # noqa: E402

FAQ = ("<h2>How fast can you arrive?</h2><p>We usually arrive within two hours for emergency calls anywhere in Miami "
       "and the surrounding towns.</p><h2>Do you charge for estimates?</h2><p>Estimates are always free for repairs "
       "and installations of every kind, with no obligation.</p>")
PAGE = T.GOOD.replace("<body><main><h1>Roof Repair in Miami</h1>",
                      '<body><header><a href="/"><h1 class="logo">Acme Roofing</h1></a></header><main>'
                      "<h1>Roof Repair in Miami</h1>" + FAQ).replace(
    '<link rel="canonical" href="https://site.com/roof-repair/">\n', "")


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        C.STATE_DIR = Path(self.td.name) / "state"
        self.root = Path(self.td.name) / "site"
        (self.root / "images").mkdir(parents=True)
        (self.root / "images" / "share.png").write_bytes(T.png(1200, 630))
        (self.root / "areas").mkdir()
        (self.root / "areas" / "index.html").write_text(
            "<html><head><title>Areas</title></head><body><h1>Service Areas</h1></body></html>", encoding="utf-8")
        self.page = self.root / "areas" / "roof-repair.html"
        self.page.write_text(PAGE, encoding="utf-8")

    def tearDown(self):
        self.td.cleanup()

    def run_it(self, **kw):
        results = []
        ai = TP.FakeAI({"meta_title": "Roof Repair in Miami, Florida", "meta_description": TP.ORIG_DESC,
                        "h1": "Roof Repair in Miami"})
        opts = P.RunOptions(folder=str(self.root), site_url="https://site.com", brand="Acme Roofing", mode="per_page",
                            ai_alt=False, backup=False, exclude_patterns=["areas/index.html"], **kw)
        P.Pipeline(opts, A.AI(ai), C.collect_html_files(self.root), lambda m, t="": None, results.append,
                   lambda d, t: None, threading.Event()).run()
        return next(r for r in results if r["rel"] == "areas/roof-repair.html")

    def nodes(self):
        html = self.page.read_text(encoding="utf-8")
        return [json.loads(b.body) for b in C.parse_ld_blocks(html)], html


class TestStructure(Base):
    def test_everything_off_changes_none_of_it(self):
        r = self.run_it()
        nodes, html = self.nodes()
        self.assertEqual(len(C.find_h1s(html, C.Mask(html))), 2)
        self.assertFalse(any(n.get("@type") in ("BreadcrumbList", "FAQPage") for n in nodes))
        self.assertTrue(any("H1 tags on page" in n for n in r["notes"]))

    def test_one_h1_breadcrumb_and_faq(self):
        r = self.run_it(fix_multi_h1=True, do_breadcrumb=True, do_faq=True)
        nodes, html = self.nodes()
        h1s = C.find_h1s(html, C.Mask(html))
        self.assertEqual(len(h1s), 1)
        self.assertIn("Roof Repair in Miami", C.strip_tags(html[h1s[0][1]:h1s[0][2]]))
        self.assertIn('<h2 class="logo">Acme Roofing</h2>', html)
        crumb = next(n for n in nodes if n.get("@type") == "BreadcrumbList")
        names = [i["name"] for i in crumb["itemListElement"]]
        self.assertEqual(names, ["Acme Roofing", "Service Areas", names[-1]])          # the section uses its own H1
        faq = next(n for n in nodes if n.get("@type") == "FAQPage")
        self.assertEqual(len(faq["mainEntity"]), 2)
        self.assertEqual((r["h1_demoted"], r["breadcrumb_added"], r["faq_items"]), (1, True, 2))
        self.assertFalse(any("H1 tags on page" in n for n in r["notes"]))

    def test_scores_do_not_go_down(self):
        r = self.run_it(fix_multi_h1=True, do_breadcrumb=True, do_faq=True)
        for c, v in r["score_before"]["cats"].items():
            self.assertGreaterEqual(r["score_after"]["cats"][c], v - 0.005, c)

    def test_markup_is_added_once(self):
        self.run_it(do_breadcrumb=True, do_faq=True)
        before = self.page.read_text(encoding="utf-8")
        self.run_it(do_breadcrumb=True, do_faq=True, run_kind="fresh")
        after = self.page.read_text(encoding="utf-8")
        self.assertEqual(after.count('"@type": "FAQPage"'), 1)
        self.assertEqual(after.count('"@type": "BreadcrumbList"'), 1)
        self.assertEqual(before.count("FAQPage"), after.count("FAQPage"))


if __name__ == "__main__":
    unittest.main()
