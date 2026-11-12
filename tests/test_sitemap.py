"""sitemap.xml check and rebuild."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import seo_canonical as K  # noqa: E402
import seo_core as C  # noqa: E402
import seo_sitemap as SM  # noqa: E402

SITE = "https://www.example.com"
PAGE = '<!DOCTYPE html><html><head><title>%s</title>%s</head><body><h1>%s</h1></body></html>'
SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.example.com/</loc><lastmod>2026-01-01</lastmod></url>
<url><loc>https://www.example.com/about.html</loc></url>
<url><loc>https://www.example.com/old.html</loc></url>
<url><loc>https://www.example.com/gone.html</loc></url>
<url><loc>https://www.example.com/hidden.html</loc></url>
<url><loc>https://www.example.com/missing.html</loc></url>
<url><loc>http://example.com/services/roof.html</loc></url>
<url><loc>https://other.com/x.html</loc></url>
<url><loc>https://www.example.com/about.html</loc></url>
<url><loc>https://www.example.com/areas/index.html</loc></url>
<url><loc>https://www.example.com/moved-canon.html</loc></url>
</urlset>"""


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        (self.root / "services").mkdir()
        (self.root / "areas").mkdir()
        w = lambda name, t, head="", h="x": (self.root / name).write_text(PAGE % (t, head, h), encoding="utf-8")
        w("index.html", "Home", h="Home")
        w("about.html", "About", h="About")
        w("old.html", "Old")
        w("gone.html", "Gone")
        w("hidden.html", "Hidden", '<meta name="robots" content="noindex">')
        w("services/roof.html", "Roof", h="Roof")
        w("areas/index.html", "Areas", h="Areas")
        w("not-listed.html", "Not listed", h="Not listed")
        w("moved-canon.html", "Moved", '<link rel="canonical" href="https://www.example.com/somewhere-else.html">')
        w("about - Copy.html", "About copy")
        (self.root / "partial.html").write_text("<ul><li>menu</li></ul>", encoding="utf-8")
        (self.root / "sitemap.xml").write_text(SITEMAP, encoding="utf-8")
        self.rules = K.parse_htaccess("Redirect 301 /old.html /about.html\nRedirect 410 /gone.html\n")
        self.files = C.collect_html_files(self.root)
        self.pages = SM.read_pages(self.root, self.files, self.rules, SITE)
        self.rep = SM.analyse(self.root, SITE, self.pages)

    def tearDown(self):
        self.td.cleanup()


class TestPages(Base):
    def test_classification(self):
        st = {p.rel: p.status for p in self.pages}
        self.assertEqual(st["about.html"], "live")
        self.assertEqual((st["old.html"], st["gone.html"]), ("301", "410"))
        self.assertEqual(st["hidden.html"], "noindex")
        self.assertEqual(st["partial.html"], "partial")
        self.assertEqual(st["about - Copy.html"], "junk")

    def test_listing_address_is_the_canonical(self):
        url = {p.rel: p.url for p in self.pages}
        self.assertEqual(url["about.html"], SITE + "/about.html")
        self.assertEqual(url["areas/index.html"], SITE + "/areas/")
        st = {p.rel: p.status for p in self.pages}
        self.assertEqual(st["moved-canon.html"], "canon_other")                       # canonical names ANOTHER page: not listed
        self.assertEqual(url["index.html"], SITE + "/")


class TestAnalyse(Base):
    def issues(self):
        out = {}
        for loc, why in self.rep.issues:
            out.setdefault(loc, []).append(why)
        return out

    def test_problems_in_the_existing_sitemap(self):
        iss = self.issues()
        self.assertIn("redirected (301)", iss[SITE + "/old.html"][0])
        self.assertIn("gone (410)", iss[SITE + "/gone.html"][0])
        self.assertIn("noindex", iss[SITE + "/hidden.html"][0])
        self.assertIn("no page file", iss[SITE + "/missing.html"][0])
        self.assertIn("wrong www / http form", " ".join(iss["http://example.com/services/roof.html"]))
        self.assertIn("another website", iss["https://other.com/x.html"][0])
        self.assertIn("more than once", " ".join(iss[SITE + "/about.html"]))
        self.assertIn("folder address", iss[SITE + "/areas/index.html"][0])
        self.assertIn("canonical is", " ".join(iss[SITE + "/moved-canon.html"]))

    def test_good_entries_and_missing_pages(self):
        self.assertGreaterEqual(self.rep.ok, 2)                                   # "/" and about.html
        missing = [rel for rel, _ in self.rep.missing]
        self.assertIn("not-listed.html", missing)
        self.assertNotIn("about.html", missing)
        self.assertNotIn("old.html", missing)                                     # redirected pages are never "missing"

    def test_new_sitemap_lists_only_live_pages_with_lastmod(self):
        xml = self.rep.new_xml
        self.assertIn(f"<loc>{SITE}/</loc>", xml)
        self.assertIn("<lastmod>2026-01-01</lastmod>", xml)                       # kept from the old sitemap
        self.assertIn(f"<loc>{SITE}/not-listed.html</loc>", xml)
        for bad in ("old.html", "gone.html", "hidden.html", "Copy", "partial"):
            self.assertNotIn(bad, xml)
        self.assertEqual(xml.count("<url>"), self.rep.new_count)
        import xml.etree.ElementTree as ET
        ET.fromstring(xml)                                                        # well-formed

    def test_no_sitemap_file(self):
        (self.root / "sitemap.xml").unlink()
        rep = SM.analyse(self.root, SITE, self.pages)
        self.assertFalse(rep.found)
        self.assertGreater(rep.new_count, 0)
        self.assertGreater(len(rep.missing), 0)

    def test_sitemap_index_and_broken_xml(self):
        (self.root / "sitemap.xml").write_text(
            '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<sitemap><loc>https://www.example.com/s1.xml</loc></sitemap></sitemapindex>", encoding="utf-8")
        self.assertEqual(SM.analyse(self.root, SITE, self.pages).kind, "index")
        (self.root / "sitemap.xml").write_text("<urlset><url>", encoding="utf-8")
        self.assertIn("cannot read", SM.analyse(self.root, SITE, self.pages).error)

    def test_extras_are_noticed(self):
        (self.root / "sitemap.xml").write_text(
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">'
            "<url><loc>https://www.example.com/about.html</loc><image:image><image:loc>x</image:loc></image:image></url></urlset>",
            encoding="utf-8")
        self.assertTrue(SM.analyse(self.root, SITE, self.pages).extras)


if __name__ == "__main__":
    unittest.main()
