"""Heading fix + breadcrumb + FAQ markup."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import seo_core as C  # noqa: E402
import seo_structure as ST  # noqa: E402

MULTI = """<!DOCTYPE html><html><head><title>Roof Repair in Miami | Acme</title></head><body>
<header><a href="/"><h1 class="logo">Acme Roofing</h1></a><nav>menu</nav></header>
<main><H1 id="main-title">Roof Repair in Miami</H1><p>Text</p><h1>Another</h1></main>
</body></html>"""


class TestH1(unittest.TestCase):
    def test_keeps_the_h1_that_matches_the_page_not_the_logo(self):
        keep, n = ST.choose_h1(MULTI, "Roof Repair in Miami | Acme", "Roof Repair")
        self.assertEqual((keep, n), (1, 3))
        out, k = ST.demote_extra_h1(MULTI, keep)
        self.assertEqual(k, 2)
        self.assertEqual(len(C.find_h1s(out, C.Mask(out))), 1)
        self.assertIn('<h2 class="logo">Acme Roofing</h2>', out)            # attributes kept
        self.assertIn("<H1 id=\"main-title\">Roof Repair in Miami</H1>", out)
        self.assertIn("<h2>Another</h2>", out)

    def test_single_h1_is_left_alone(self):
        html = "<html><head></head><body><h1>Only</h1></body></html>"
        self.assertEqual(ST.choose_h1(html, "t", "p"), (0, 1))
        self.assertEqual(ST.demote_extra_h1(html, 0), (html, 0))

    def test_ties_keep_the_first(self):
        html = "<html><head></head><body><h1>Alpha</h1><h1>Beta</h1></body></html>"
        self.assertEqual(ST.choose_h1(html, "", "")[0], 0)


FAQ = """<html><head></head><body><nav><h3>Why us?</h3><p>This sits in the navigation and is never used here.</p></nav>
<main><h1>Plumbing FAQ</h1>
<h2>How fast can you arrive?</h2><p>We usually arrive within two hours for emergency calls anywhere in Broward.</p><p>Same day otherwise.</p>
<h2>Do you charge for estimates?</h2><ul><li>Estimates are always free for repairs and installations of every kind.</li></ul>
<h2>Our story</h2><p>Not a question so it is ignored by the parser completely.</p>
<details><summary>Are you licensed and insured?</summary><p>Yes, every technician is licensed and the company carries full insurance.</p></details>
</main></body></html>"""


class TestFaq(unittest.TestCase):
    def test_pairs_come_from_the_visible_content_only(self):
        pairs = ST.extract_faq(FAQ)
        qs = [q for q, _ in pairs]
        self.assertEqual(qs, ["Are you licensed and insured?", "How fast can you arrive?", "Do you charge for estimates?"])
        self.assertIn("Same day otherwise.", dict(pairs)["How fast can you arrive?"])
        self.assertNotIn("Why us?", qs)                                   # navigation is skipped

    def test_needs_two_questions(self):
        one = "<html><head></head><body><main><h2>Why?</h2><p>" + "x" * 60 + "</p></main></body></html>"
        self.assertEqual(ST.extract_faq(one), [])
        self.assertIsNone(ST.build_faq([("a?", "b" * 50)]))

    def test_node_shape(self):
        node = ST.build_faq(ST.extract_faq(FAQ))
        self.assertEqual(node["@type"], "FAQPage")
        self.assertEqual(node["mainEntity"][0]["@type"], "Question")
        self.assertIn("text", node["mainEntity"][0]["acceptedAnswer"])


class TestBreadcrumb(unittest.TestCase):
    info = staticmethod(lambda d: {"areas": ("Service Areas", "https://x.com/areas/")}.get(d))

    def test_home_section_page(self):
        node = ST.build_breadcrumb("https://x.com", "areas/miami.html", "https://x.com/areas/miami.html",
                                   "Miami Plumber", "Acme", self.info)
        names = [i["name"] for i in node["itemListElement"]]
        self.assertEqual(names, ["Acme", "Service Areas", "Miami Plumber"])
        self.assertEqual([i["position"] for i in node["itemListElement"]], [1, 2, 3])
        self.assertEqual(node["itemListElement"][-1]["item"], "https://x.com/areas/miami.html")

    def test_sections_without_an_index_page_are_left_out(self):
        node = ST.build_breadcrumb("https://x.com", "blog/post.html", "https://x.com/blog/post.html", "Post", "", self.info)
        self.assertEqual([i["name"] for i in node["itemListElement"]], ["Home", "Post"])

    def test_home_page_gets_none_and_index_pages_are_their_own_folder(self):
        self.assertIsNone(ST.build_breadcrumb("https://x.com", "index.html", "https://x.com/", "Home", "", self.info))
        node = ST.build_breadcrumb("https://x.com", "areas/index.html", "https://x.com/areas/", "Areas", "", self.info)
        self.assertEqual([i["name"] for i in node["itemListElement"]], ["Home", "Areas"])


class TestAddNode(unittest.TestCase):
    PAGE = '<html><head>\n  <title>t</title>\n</head><body></body></html>'

    def test_adds_once(self):
        node = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": []}
        out, a = ST.add_node(self.PAGE, node)
        self.assertEqual(a, "added")
        self.assertEqual(json.loads(C.parse_ld_blocks(out)[0].body)["@type"], "FAQPage")
        self.assertEqual(ST.add_node(out, node)[1], "present")

    def test_goes_after_existing_json_ld_and_ignores_a_webpage_node(self):
        html = self.PAGE.replace("</head>", '<script type="application/ld+json">{"@type":"WebPage"}</script>\n</head>')
        out, a = ST.add_node(html, {"@type": "FAQPage"})
        self.assertEqual(a, "added")
        self.assertEqual(len(C.parse_ld_blocks(out)), 2)

    def test_no_head(self):
        self.assertEqual(ST.add_node("<p>x</p>", {"@type": "FAQPage"})[1], "no_head")


if __name__ == "__main__":
    unittest.main()
