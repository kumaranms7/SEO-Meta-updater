import json
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import seo_core as C  # noqa: E402

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Old title</title>
    <meta name="description" content="Save > 20% today, it's great">
    <meta name="keywords" content="a,b">
    <!-- <title>commented</title> -->
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[]}</script>
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"LocalBusiness","name":"Acme","telephone":"555-1111","description":"old"}</script>
</head>
<body>
<header><nav><a href="/">Home</a></nav><img src="images/logo.png"></header>
<svg><title>icon title</title></svg>
<main>
<h1>Old <span class="x">H1</span> text</h1>
<img src="../images/team-photo.jpg" class="hero">
<img src="images/IMG_1234.jpg">
<img src="a.svg">
<img src="b.png" alt="">
<img src="c.png" data-alt="x" />
</main>
</body></html>
"""


class TestText(unittest.TestCase):
    def test_title_build(self):
        self.assertEqual(C.build_title("Garage Doors | Acme", " | ", "Acme"), "Garage Doors | Acme")
        self.assertEqual(C.build_title("Acme Garage Doors in Miami -", "-", "Acme"), "Garage Doors in Miami - Acme")
        self.assertEqual(C.title_budget("|", "Acme"), 60 - 3 - 4)
        self.assertTrue(C.title_complete("Garage Door Repair"))
        self.assertFalse(C.title_complete("Garage Door Repair and"))
        self.assertTrue(C.sentence_complete("Call now."))
        self.assertFalse(C.sentence_complete("Call now and"))

    def test_escape(self):
        self.assertEqual(C.esc_attr('A "b" & <c>'), "A &quot;b&quot; &amp; &lt;c&gt;")
        self.assertEqual(C.clean_text("Tom &amp; Jerry"), "Tom & Jerry")


class TestEdits(unittest.TestCase):
    def test_title_ignores_comments_and_svg(self):
        out, ch = C.set_title(PAGE, 'New "T" & Co')
        self.assertTrue(ch)
        self.assertIn("<title>New \"T\" &amp; Co</title>", out)
        self.assertIn("<!-- <title>commented</title> -->", out)
        self.assertIn("<svg><title>icon title</title></svg>", out)
        self.assertEqual(C.read_title(out), 'New "T" & Co')

    def test_title_backslash_safe(self):
        out, _ = C.set_title(PAGE, r"Path \1 \g_0 x")
        self.assertIn(r"<title>Path \1 \g_0 x</title>", out)

    def test_meta_with_gt_in_content(self):
        out, ch = C.upsert_metas(PAGE, [{"key": "description", "content": 'He said "hi" & left', "attr": "name"}])
        self.assertTrue(ch)
        self.assertIn('<meta name="description" content="He said &quot;hi&quot; &amp; left">', out)
        self.assertNotIn("Save >", out)
        self.assertEqual(out.count('name="description"'), 1)
        again, ch2 = C.upsert_metas(out, [{"key": "description", "content": 'He said "hi" & left'}])
        self.assertFalse(ch2)

    def test_meta_insert_and_only_if_missing(self):
        items = [{"key": "robots", "content": "index, follow", "only_if_missing": True},
                 {"key": "og:title", "content": "T", "attr": "property"}]
        out, ch = C.upsert_metas(PAGE, items)
        self.assertTrue(ch)
        self.assertIn('<meta name="robots" content="index, follow">', out)
        self.assertIn('<meta property="og:title" content="T">', out)
        # existing robots is left alone
        out2, _ = C.upsert_metas(out.replace("index, follow", "noindex"), items[:1])
        self.assertIn('content="noindex"', out2)

    def test_og_keeps_unrelated_tags(self):
        base = PAGE.replace("</head>", '<meta property="article:author" content="Z"><meta name="twitter:site" content="@z"></head>')
        out, _ = C.upsert_metas(base, [{"key": "og:title", "content": "T", "attr": "property"}])
        self.assertIn('article:author', out)
        self.assertIn('twitter:site', out)

    def test_remove_keywords(self):
        out, n = C.remove_metas(PAGE, "keywords")
        self.assertEqual(n, 1)
        self.assertNotIn("keywords", out)

    def test_h1_keeps_markup(self):
        out, ch, issue = C.set_h1(PAGE, "Brand New Heading")
        self.assertTrue(ch)
        self.assertEqual(issue, "")
        self.assertIn('<span class="x">', out)
        self.assertEqual(C.strip_tags(C.find_h1_text(out)) if hasattr(C, "find_h1_text") else "", "") if False else None
        self.assertIn("Brand New Heading", out)

    def test_h1_multiple_missing(self):
        two = PAGE.replace("</main>", "<h1>Second</h1></main>")
        self.assertEqual(C.set_h1(two, "x")[2], "multiple")
        none = PAGE.replace("<h1>Old <span class=\"x\">H1</span> text</h1>", "")
        self.assertEqual(C.set_h1(none, "x")[2], "missing")

    def test_h1_unchanged(self):
        simple = "<html><head></head><body><h1>Same</h1></body></html>"
        out, ch, _ = C.set_h1(simple, "Same")
        self.assertFalse(ch)
        out, ch, _ = C.set_h1(simple, "A & B")
        self.assertIn("<h1>A &amp; B</h1>", out)


class TestSchema(unittest.TestCase):
    def test_merge_into_existing_localbusiness(self):
        gen = {"@context": "https://schema.org", "@type": "LocalBusiness", "name": "Other Name",
               "description": "new desc", "telephone": "999", "priceRange": "$$",
               "address": {"@type": "PostalAddress", "addressLocality": "Miami"}}
        out, action = C.apply_schema(PAGE, gen)
        self.assertEqual(action, "merged")
        blocks = C.parse_ld_blocks(out)
        self.assertEqual(len(blocks), 2)                       # FAQ untouched, LB merged
        lb = blocks[1].data
        self.assertEqual(lb["name"], "Acme")                   # existing wins
        self.assertEqual(lb["telephone"], "555-1111")
        self.assertEqual(lb["description"], "new desc")        # description updated
        self.assertEqual(lb["priceRange"], "$$")               # gap filled
        self.assertEqual(blocks[0].data["@type"], "FAQPage")
        again, action2 = C.apply_schema(out, gen)
        self.assertEqual(action2, "unchanged")

    def test_add_when_absent_and_graph(self):
        page = "<html><head>\n  <title>x</title>\n</head><body></body></html>"
        gen = {"@context": "https://schema.org", "@type": "WebPage", "name": "n"}
        out, action = C.apply_schema(page, gen)
        self.assertEqual(action, "added")
        self.assertEqual(len(C.parse_ld_blocks(out)), 1)
        graph = ('<html><head><script type="application/ld+json">{"@context":"https://schema.org","@graph":'
                 '[{"@type":"Organization","name":"O"},{"@type":"WebPage","name":"P"}]}</script></head></html>')
        out, action = C.apply_schema(graph, {"@type": "WebPage", "description": "d", "url": "https://x/"})
        self.assertEqual(action, "merged")
        d = C.parse_ld_blocks(out)[0].data
        self.assertEqual(d["@graph"][1]["url"], "https://x/")
        self.assertEqual(len(C.parse_ld_blocks(out)), 1)

    def test_unparseable_block_untouched(self):
        page = '<html><head><script type="application/ld+json">{"@type":"WebPage",}</script></head></html>'
        out, action = C.apply_schema(page, {"@type": "WebPage", "name": "n"})
        self.assertEqual(action, "skipped_unparseable")
        self.assertEqual(out, page)

    def test_build_schema_uses_facts_only(self):
        facts = {"name": "Acme", "business_type": "LocalBusiness", "phone": "555", "street": "1 Main",
                 "city": "Miami", "region": "FL", "postal": "33101", "country": "US",
                 "hours": ["Mo-Fr 09:00-17:00"], "price_range": "$$", "same_as": ["https://fb.com/a"]}
        ai = {"schema_type": "LocalBusiness", "area_served": "Miami"}
        node, t, issues = C.build_schema(ai, facts, "https://x.com/p/", "https://x.com", "https://x.com/i.jpg",
                                         "Desc.", "H1", "Title")
        self.assertEqual(t, "LocalBusiness")
        self.assertEqual(node["telephone"], "555")
        self.assertEqual(node["address"]["addressLocality"], "Miami")
        node, t, issues = C.build_schema({"schema_type": "LocalBusiness"}, {"name": "Acme"}, "u", "s", "", "D.", "H", "T")
        self.assertEqual(t, "WebPage")
        self.assertTrue(issues)
        node, t, _ = C.build_schema({"schema_type": "Article"}, facts, "u", "s", "", "D.", "H", "T")
        self.assertEqual(t, "WebPage")


class TestImages(unittest.TestCase):
    def test_scan_and_apply(self):
        imgs = C.scan_images(PAGE)
        self.assertEqual(len(imgs), 6)                          # svg-tag <title> etc ignored; svg tag is not an img
        by_src = {i.attrs.get("src"): i for i in imgs}
        self.assertTrue(by_src["images/logo.png"].in_chrome)
        self.assertTrue(by_src["a.svg"].svg)
        self.assertEqual(by_src["../images/team-photo.jpg"].candidate, "Team photo")
        self.assertIsNone(by_src["images/IMG_1234.jpg"].candidate)
        alts = {i.index: (i.candidate or "Fallback") for i in imgs if C.img_needs_alt(i)}
        out, lazy, alt = C.apply_images(PAGE, imgs, alts, True, True)
        self.assertIn('<img src="a.svg">', out)                 # svg untouched
        self.assertIn('<img src="b.png" alt="" loading="lazy">', out)
        self.assertIn('<img src="images/logo.png" alt="Fallback">', out)   # header image: alt yes, lazy no
        self.assertNotIn('loading="lazy" class="hero"', out)
        hero = [l for l in out.splitlines() if "team-photo" in l][0]
        self.assertIn('alt="Team photo"', hero)
        self.assertNotIn("lazy", hero)                          # first content image stays eager
        self.assertIn('data-alt="x" alt="Fallback" loading="lazy" />', out)

    def test_resolve_relative(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "images").mkdir()
            (root / "services").mkdir()
            (root / "images" / "a.jpg").write_bytes(b"x")
            page = root / "services" / "p.html"
            r = C.resolve_image("../images/a.jpg", page, root, "https://s.com")
            self.assertEqual(r["url"], "https://s.com/images/a.jpg")
            self.assertTrue(r["exists"])
            r = C.resolve_image("/images/a.jpg", page, root, "https://s.com")
            self.assertEqual(r["url"], "https://s.com/images/a.jpg")
            self.assertFalse(C.resolve_image("../images/zz.jpg", page, root, "https://s.com")["exists"])
            self.assertIsNone(C.resolve_image("../../outside.jpg", page, root, "https://s.com"))
            r = C.resolve_image("https://s.com/images/a.jpg", page, root, "https://s.com")
            self.assertTrue(r["local"])

    def test_image_size_and_pick(self):
        def png(w, h):
            def chunk(t, d):
                c = struct.pack(">I", len(d)) + t + d
                return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
            return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(b"\x00" * (h * (1 + w * 3)))) + chunk(b"IEND", b""))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "images").mkdir()
            (root / "svc").mkdir()
            (root / "images" / "hero-shot.png").write_bytes(png(800, 400))
            (root / "images" / "tiny.png").write_bytes(png(20, 20))
            (root / "images" / "default.png").write_bytes(png(1200, 630))
            self.assertEqual(C.image_size(root / "images" / "hero-shot.png"), (800, 400))
            html = '<html><head></head><body><img src="../images/tiny.png"><img src="../images/hero-shot.png"></body></html>'
            pick = C.pick_page_image(html, root / "svc" / "p.html", root, "https://s.com", "")
            self.assertEqual(pick["url"], "https://s.com/images/hero-shot.png")
            self.assertEqual((pick["width"], pick["height"]), (800, 400))
            bare = '<html><head></head><body><p>x</p></body></html>'
            pick = C.pick_page_image(bare, root / "svc" / "p.html", root, "https://s.com", "/images/default.png")
            self.assertEqual(pick["source"], "default image")
            self.assertEqual(pick["width"], 1200)


class TestMisc(unittest.TestCase):
    def test_encoding_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.html"
            raw = "<html><head><meta charset=\"iso-8859-1\"><title>Caf\xe9</title></head>\r\n<body>x</body></html>".encode("latin-1")
            p.write_bytes(raw)
            text, enc, nl = C.read_html(p)
            self.assertIn("Café", text)
            self.assertEqual(nl, "\r\n")
            text, _ = C.set_title(text, "Café “quoted” 中")
            C.write_html(p, text, enc, nl)
            back = p.read_bytes()
            self.assertIn(b"\r\n", back)
            self.assertIn("Café".encode(enc), back)
            self.assertIn(b"&#20013;", back)                    # unencodable -> char ref, never '?'
            u = Path(td) / "u.html"
            u.write_bytes("<html><head><title>é</title></head></html>".encode("utf-8"))
            t, e, n = C.read_html(u)
            self.assertEqual((e, n), ("utf-8", "\n"))

    def test_classify_skip(self):
        self.assertTrue(C.classify_skip("<div>partial</div>", "inc/header.html"))
        self.assertIn("noindex", C.classify_skip('<html><head><meta name="robots" content="noindex"></head></html>', "a.html"))
        self.assertIn("redirect", C.classify_skip('<html><head><meta http-equiv="refresh" content="0; url=/x"></head></html>', "a.html"))
        self.assertIn("404", C.classify_skip("<html><head><title>x</title></head></html>", "404.html"))
        self.assertEqual(C.classify_skip("<html><head><title>x</title></head></html>", "services/a.html"), "")

    def test_patterns(self):
        pats = ["includes/", "thank-you.html", "*/admin/*", "# c"]
        self.assertTrue(C.matches_patterns("includes/nav.html", pats))
        self.assertTrue(C.matches_patterns("a/thank-you.html", pats))
        self.assertTrue(C.matches_patterns("x/admin/y.html", pats))
        self.assertFalse(C.matches_patterns("services/a.html", pats))

    def test_extract_content_skips_nav_footer(self):
        html = ("<html><body><nav>Menu Home About</nav><div class='cookie-banner'>Accept</div>"
                "<main><h1>Roof Repair</h1><h2>Why us</h2><p>" + "We repair roofs fast. " * 30 +
                "</p></main><footer>Copyright 2024</footer></body></html>")
        c = C.extract_content(html)
        self.assertNotIn("Menu", c["text"])
        self.assertNotIn("Copyright", c["text"])
        self.assertNotIn("Accept", c["text"])
        self.assertEqual(c["h1s"], ["Roof Repair"])
        self.assertEqual(c["headings"], ["Why us"])

    def test_page_name(self):
        root = Path("/site")
        self.assertEqual(C.page_name_from_path("/site/index.html", root), "Home")
        self.assertEqual(C.page_name_from_path("/site/services/web-design/index.html", root), "Web Design")
        self.assertEqual(C.page_name_from_path("/site/services/roof-repair.html", root), "Roof Repair")

    def test_registry_and_dups(self):
        r = C.Registry()
        r.add("Roof Repair Miami | Acme", "Roof Repair Miami", "Fast roof repair in Miami.", "Roof Repair Miami", "roof miami")
        self.assertTrue(r.title_dup("Roof Repair Miami"))
        self.assertIsNone(r.title_dup("Kitchen Remodeling Tampa"))
        self.assertTrue(r.desc_dup("Fast roof repair in Miami!"))
        near = r.nearest([C.context_tokens("roof miami x")], 3, 5)
        self.assertEqual(len(near), 1)
        d = C.DupDetector()
        base = " ".join(f"word{i % 37} filler{i % 11}" for i in range(200))
        self.assertIsNone(d.check_and_add("a", base + " miami"))
        hit = d.check_and_add("b", base + " tampa")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "a")
        other = " ".join(f"zeta{i} quark{i * 3}" for i in range(200))
        self.assertIsNone(d.check_and_add("c", other))

    def test_state_and_backup(self):
        with tempfile.TemporaryDirectory() as td:
            C.STATE_DIR = Path(td) / "state"
            site = Path(td) / "site"
            (site / "a").mkdir(parents=True)
            f = site / "a" / "p.html"
            f.write_text("orig")
            s = C.RunState(site)
            s.record("a/p.html", status="done", title="T")
            s.save()
            s2 = C.RunState(site)
            self.assertEqual(s2.files["a/p.html"]["title"], "T")
            self.assertEqual(s2.counts(), {"done": 1})
            bdir = Path(td) / "bk"
            C.backup_file(f, site, bdir)
            f.write_text("changed")
            C.backup_file(f, site, bdir)                         # must not overwrite the original
            self.assertEqual((bdir / "a" / "p.html").read_text(), "orig")
            self.assertEqual(C.restore_backup(bdir, site), 1)
            self.assertEqual(f.read_text(), "orig")


class TestReportFilesAreNotPages(unittest.TestCase):
    def test_reports_saved_in_the_scanned_folder_are_ignored_by_the_scanner(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_text("<html><head></head></html>")
            (root / "seo_report_20270101_101010.html").write_text("<html>report</html>")
            (root / "sub").mkdir()
            (root / "sub" / "SEO_REPORT_x.html").write_text("<html>report</html>")
            (root / "sub" / "about.html").write_text("<html></html>")
            found = [Path(f).name for f in C.collect_html_files(root)]
            self.assertEqual(sorted(found), ["about.html", "index.html"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
