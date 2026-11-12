"""Internal links to redirected / gone pages."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import seo_canonical as K  # noqa: E402
import seo_links as L  # noqa: E402

SITE = "https://www.example.com"
HT = """RewriteEngine On
Redirect 301 /old.html https://www.example.com/new/
Redirect 301 /chain-a.html /chain-b.html
Redirect 301 /chain-b.html https://www.example.com/final.html
Redirect 301 /to-gone.html /gone.html
Redirect 410 /gone.html
Redirect 302 /temp.html /elsewhere.html
Redirect 301 /away.html https://other.com/page
RedirectMatch 301 ^/blog/(.*)\.html$ https://www.example.com/news/$1/
Redirect 301 /loop-a.html /loop-b.html
Redirect 301 /loop-b.html /loop-a.html
"""
RULES = K.parse_htaccess(HT)


def page(*links):
    return "<html><head><title>t</title></head><body>" + "".join(links) + "</body></html>"


class TestFixLinks(unittest.TestCase):
    def go(self, html, rel="services/page.html"):
        return L.fix_links(html, rel, SITE, RULES)

    def test_absolute_root_and_relative_links_keep_their_style(self):
        html = page('<a href="https://www.example.com/old.html">a</a>',
                    '<a href="/old.html">b</a>',
                    '<a href="../old.html">c</a>')
        out, ch, fi = self.go(html)
        self.assertEqual([c.new for c in ch], ["https://www.example.com/new/", "/new/", "../new/"])
        self.assertIn('href="https://www.example.com/new/"', out)
        self.assertIn('href="/new/"', out)
        self.assertIn('href="../new/"', out)
        self.assertEqual(fi, [])

    def test_query_and_fragment_are_kept_and_chains_are_followed(self):
        out, ch, _ = self.go(page('<a href="/chain-a.html?x=1#top">x</a>'))
        self.assertEqual(ch[0].new, "/final.html?x=1#top")

    def test_regex_rules_expand_back_references(self):
        out, ch, _ = self.go(page('<a href="/blog/my-post.html">x</a>'))
        self.assertEqual(ch[0].new, "/news/my-post/")

    def test_gone_temp_external_and_loops_are_only_reported(self):
        html = page('<a href="/gone.html">g</a><a href="/to-gone.html">tg</a><a href="/temp.html">t</a>'
                    '<a href="/away.html">e</a><a href="/loop-a.html">l</a>')
        out, ch, fi = self.go(html)
        self.assertEqual(out, html)
        self.assertEqual(ch, [])
        issues = " | ".join(f.issue for f in fi)
        self.assertEqual(len(fi), 5)
        self.assertIn("gone (410)", issues)
        self.assertIn("temporary (302)", issues)
        self.assertIn("another website", issues)
        self.assertIn("loop", issues)

    def test_other_links_are_left_alone(self):
        html = page('<a href="/fine.html">a</a><a href="https://other.com/old.html">b</a><a href="mailto:a@b.c">c</a>'
                    '<a href="#top">d</a><a href="tel:123">e</a><a href="">f</a>')
        self.assertEqual(self.go(html), (html, [], []))

    def test_quotes_comments_and_scripts(self):
        html = page("<a class='x' href='/old.html'>a</a>", "<!-- <a href=\"/old.html\">c</a> -->",
                    "<script>var s='<a href=\"/old.html\">';</script>", "<a href=/old.html>bare</a>")
        out, ch, _ = self.go(html)
        self.assertEqual(len(ch), 2)
        self.assertIn("href='/new/'", out)
        self.assertIn('href="/new/"', out)                                 # the bare one gets quotes
        self.assertIn("<!-- <a href=\"/old.html\">c</a> -->", out)         # comment untouched
        self.assertIn("var s='<a href=\"/old.html\">';", out)              # script untouched

    def test_root_page_relative_links(self):
        out, ch, _ = self.go(page('<a href="old.html">a</a>'), rel="index.html")
        self.assertEqual(ch[0].new, "new/")

    def test_base_href_pages_are_not_touched(self):
        html = '<html><head><base href="https://www.example.com/x/"></head><body><a href="old.html">a</a></body></html>'
        out, ch, fi = self.go(html)
        self.assertEqual((out, ch), (html, []))
        self.assertTrue(fi)

    def test_is_idempotent(self):
        out, _, _ = self.go(page('<a href="/old.html">a</a>'))
        self.assertEqual(self.go(out)[1], [])


if __name__ == "__main__":
    unittest.main()
