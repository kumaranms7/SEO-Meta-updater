"""Step 1: canonical tags + .htaccess 301 / 302 / 410 detection."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import seo_canonical as K  # noqa: E402

SITE = "https://www.example.com"

HT = r"""
# comment
RewriteEngine On
RewriteCond %{HTTP_HOST} !^www\. [OR]
RewriteCond %{HTTPS} off
RewriteRule ^ https://www.example.com%{REQUEST_URI} [R=301,L]

Redirect 410 /gone-page.html
Redirect gone /old-folder/
Redirect 301 /moved.html https://www.example.com/new/
Redirect permanent /moved2.html https://www.example.com/new2/
Redirect /temp.html https://www.example.com/elsewhere/
RedirectMatch 301 ^/blog/(.*)\.html$ https://www.example.com/news/$1/
RedirectMatch 410 ^/discontinued-.*\.html$
RewriteRule ^rw-old\.html$ /rw-new.html [R=301,L]
RewriteRule ^rw-gone\.html$ - [G]
RewriteRule ^cond\.html$ /x.html [R=301,L]
RewriteRule ^internal\.html$ /real.html [L]
RedirectMatch 301 ^/(.*)\.html$ https://www.example.com/
"""


class TestHtaccess(unittest.TestCase):
    def setUp(self):
        self.r = K.parse_htaccess(HT)

    def kind(self, rel, **kw):
        m = self.r.match(rel, **kw)
        return m.kind if m else None

    def test_410_gone_rules_are_read(self):
        self.assertEqual(self.kind("gone-page.html"), "410")
        self.assertEqual(self.kind("old-folder/page.html"), "410")          # Redirect matches the whole folder
        self.assertEqual(self.kind("discontinued-service.html"), "410")
        self.assertEqual(self.kind("rw-gone.html"), "410")

    def test_301_and_302(self):
        self.assertEqual(self.kind("moved.html"), "301")
        self.assertEqual(self.kind("moved2.html"), "301")
        self.assertEqual(self.kind("blog/post-1.html"), "301")
        self.assertEqual(self.kind("rw-old.html"), "301")
        self.assertEqual(self.kind("temp.html"), "302")                     # no status = temporary

    def test_normal_pages_are_not_redirected(self):
        for rel in ("about.html", "index.html", "services/roofing.html", "internal.html"):
            self.assertIsNone(self.r.match(rel), rel)

    def test_catch_all_and_sitewide_rules_are_ignored(self):
        why = {i.raw: i.why for i in self.r.ignored}
        self.assertTrue(any("catch-all" in w for raw, w in why.items() if raw.startswith("RedirectMatch 301 ^/(.*)")))
        self.assertTrue(any("RewriteCond" in w for w in why.values()))       # the www/https rule
        self.assertIsNone(self.r.match("anything.html"))                    # so ordinary pages are not skipped

    def test_rewrite_with_conditions_is_not_a_page_rule(self):
        t = "RewriteEngine On\nRewriteCond %{QUERY_STRING} x\nRewriteRule ^a\\.html$ /b.html [R=301,L]\n"
        r = K.parse_htaccess(t)
        self.assertEqual(r.counts()["301"], 0)
        self.assertEqual(len(r.ignored), 1)

    def test_rewrite_rules_need_the_engine_on(self):
        r = K.parse_htaccess("RewriteRule ^a\\.html$ /b.html [R=301]\n")
        self.assertEqual(r.counts()["301"], 0)

    def test_first_match_wins_in_file_order(self):
        r = K.parse_htaccess("Redirect 410 /a.html\nRedirect 301 /a.html https://x.com/\n")
        self.assertEqual(r.match("a.html").kind, "410")

    def test_line_continuation_and_quotes(self):
        r = K.parse_htaccess('Redirect 301 "/my page.html" \\\n   https://x.com/\n')
        self.assertEqual(r.match("my page.html").kind, "301")

    def test_index_pages_use_folder_urls(self):
        r = K.parse_htaccess("Redirect 301 /old/ https://x.com/new/\n")
        self.assertEqual(r.match("old/index.html").kind, "301")
        self.assertIsNone(r.match("index.html"))

    def test_clean_url_variants_only_when_asked(self):
        r = K.parse_htaccess("Redirect 410 /boca/\n")
        self.assertIsNone(r.match("boca.html"))
        self.assertEqual(r.match("boca.html", clean_urls=True).kind, "410")

    def test_counts(self):
        c = self.r.counts()
        self.assertGreaterEqual(c["410"], 4)
        self.assertGreaterEqual(c["301"], 4)
        self.assertEqual(c["302"], 1)


PAGE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Roof Repair</title>
</head>
<body><h1>Roof Repair</h1></body>
</html>
"""


class TestDecision(unittest.TestCase):
    def go(self, html, rel="roof-repair.html"):
        d = K.decide(html, rel, SITE)
        return d, K.apply_decision(html, d)

    def test_missing_canonical_is_inserted_on_its_own_line(self):
        d, out = self.go(PAGE)
        self.assertEqual(d.action, "insert")
        self.assertIn('  <link rel="canonical" href="https://www.example.com/roof-repair.html">\n</head>', out)
        self.assertEqual(K.decide(out, "roof-repair.html", SITE).action, "ok")           # idempotent

    def test_index_pages_get_the_folder_url(self):
        self.assertEqual(K.canonical_url(SITE, "index.html"), SITE + "/")
        self.assertEqual(K.canonical_url(SITE, "areas/index.html"), SITE + "/areas/")
        self.assertEqual(K.canonical_url(SITE + "/", "a b.html"), SITE + "/a%20b.html")

    def test_correct_canonical_is_left_alone(self):
        h = PAGE.replace("</head>", '  <link rel="canonical" href="https://www.example.com/roof-repair.html">\n</head>')
        d, out = self.go(h)
        self.assertEqual((d.action, out), ("ok", h))

    def test_clean_url_form_of_the_same_page_is_kept(self):
        for href in ("https://www.example.com/roof-repair/", "https://www.example.com/roof-repair"):
            h = PAGE.replace("</head>", f'<link rel="canonical" href="{href}">\n</head>')
            self.assertEqual(self.go(h)[0].action, "ok", href)

    def test_other_page_of_this_site_is_replaced(self):
        h = PAGE.replace("</head>", '<link rel="canonical" href="https://www.example.com/other.html">\n</head>')
        d, out = self.go(h)
        self.assertEqual(d.action, "replace")
        self.assertIn("roof-repair.html", out)
        self.assertNotIn("other.html", out)
        self.assertEqual(out.count('rel="canonical"'), 1)

    def test_wrong_host_scheme_relative_and_empty_are_fixed(self):
        for href in ("http://www.example.com/roof-repair.html", "https://example.com/roof-repair.html",
                     "/roof-repair.html", ""):
            h = PAGE.replace("</head>", f'<link rel="canonical" href="{href}">\n</head>')
            d, out = self.go(h)
            self.assertEqual(d.action, "replace", href)
            self.assertIn('href="https://www.example.com/roof-repair.html"', out)

    def test_another_domain_is_kept_and_flagged(self):
        h = PAGE.replace("</head>", '<link rel="canonical" href="https://other.com/roof-repair.html">\n</head>')
        d, out = self.go(h)
        self.assertEqual(d.action, "keep_foreign")
        self.assertEqual(out, h)
        self.assertIn("another domain", d.note)

    def test_duplicate_canonicals_are_cleaned_up(self):
        h = PAGE.replace("</head>", '<link rel="canonical" href="https://www.example.com/roof-repair.html">\n'
                                    '<link rel="canonical" href="https://www.example.com/x.html">\n</head>')
        d, out = self.go(h)
        self.assertEqual(d.action, "replace")
        self.assertEqual(out.count('rel="canonical"'), 1)

    def test_comments_and_scripts_are_not_touched(self):
        h = PAGE.replace("</head>", '<!-- <link rel="canonical" href="https://old.com/"> -->\n</head>')
        d, out = self.go(h)
        self.assertEqual(d.action, "insert")
        self.assertIn('<!-- <link rel="canonical" href="https://old.com/"> -->', out)

    def test_page_without_head(self):
        self.assertEqual(K.decide("<p>no head</p>", "a.html", SITE).action, "nohead")

    def test_undo_restores_the_original(self):
        d, out = self.go(PAGE)
        self.assertEqual(K.undo(out, K.entry_of(d)), PAGE)
        old = PAGE.replace("</head>", '  <link rel="canonical" href="https://www.example.com/other.html">\n</head>')
        d, out = self.go(old)
        self.assertEqual(K.undo(out, K.entry_of(d)), old)

    def test_remove_all_for_redirected_pages(self):
        h = PAGE.replace("</head>", '  <link rel="canonical" href="https://www.example.com/x.html">\n</head>')
        out, n = K.remove_all(h)
        self.assertEqual(n, 1)
        self.assertEqual(out, PAGE)
        self.assertEqual(K.remove_all(PAGE)[1], 0)


class TestSchemaUrl(unittest.TestCase):
    def page(self, node_type, url):
        return ('<html><head><script type="application/ld+json">{"@context":"https://schema.org","@type":"%s",'
                '"name":"x","url":"%s"}</script></head><body></body></html>' % (node_type, url))

    def test_page_level_schema_url_follows_the_canonical(self):
        import seo_core as C
        out, n = C.align_schema_urls(self.page("Service", "https://www.example.com/roof/"), SITE + "/roof.html")
        self.assertEqual(n, 1)
        self.assertIn('"url": "https://www.example.com/roof.html"', out)

    def test_business_nodes_and_other_domains_keep_their_url(self):
        import seo_core as C
        for t, u in (("LocalBusiness", "https://www.example.com/"), ("Service", "https://other.com/x")):
            html = self.page(t, u)
            self.assertEqual(C.align_schema_urls(html, SITE + "/roof.html"), (html, 0))


class TestJunk(unittest.TestCase):
    def test_copies_backups_and_reports_are_not_tagged(self):
        for rel in ("financing - Copy.html", "index - Copy.html", "backup/a.html", "_notes/x.html",
                    "seo_report_20261020_104234.html", "page (2).html", "page-old.html", "cgi-bin/x.html"):
            self.assertTrue(K.is_junk(rel), rel)
        for rel in ("copyright.html", "services/roof-repair.html", "index.html", "oldtown-plumbing.html"):
            self.assertFalse(K.is_junk(rel), rel)


if __name__ == "__main__":
    unittest.main()
