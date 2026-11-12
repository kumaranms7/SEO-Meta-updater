import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import seo_core as C  # noqa: E402
import seo_score as S  # noqa: E402


def png(w, h):
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" * (h * (1 + w * 3)))) + chunk(b"IEND", b""))


BODY = ("<p>" + "Our team provides professional roof repair for homes across Miami, fixing leaks, storm damage and "
        "worn shingles quickly with clear pricing. " * 8 + "</p>")
GOOD = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Roof Repair in Miami | Acme Roofing</title>
<meta name="description" content="Fast roof repair in Miami for leaks, storm damage and worn shingles, with clear pricing from Acme Roofing. Call today for a free quote.">
<link rel="canonical" href="https://site.com/roof-repair/">
<meta name="robots" content="index, follow">
<meta property="og:title" content="Roof Repair in Miami | Acme Roofing">
<meta property="og:description" content="Fast roof repair in Miami for leaks, storm damage and worn shingles, with clear pricing from Acme Roofing. Call today for a free quote.">
<meta property="og:url" content="https://site.com/roof-repair/">
<meta property="og:site_name" content="Acme Roofing">
<meta property="og:image" content="https://site.com/images/share.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Roof Repair in Miami | Acme Roofing">
<meta name="twitter:description" content="Fast roof repair in Miami for leaks, storm damage and worn shingles, with clear pricing from Acme Roofing. Call today for a free quote.">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Service","name":"Roof Repair in Miami","description":"Fast roof repair in Miami for leaks, storm damage and worn shingles, with clear pricing from Acme Roofing. Call today for a free quote.","url":"https://site.com/roof-repair/","provider":{"@type":"LocalBusiness","name":"Acme Roofing"}}</script>
</head><body><main><h1>Roof Repair in Miami</h1><h2>Why choose us</h2>""" + BODY + """</main></body></html>"""

BAD = "<html><head></head><body><h1>Welcome</h1><h1>Home</h1><p>Hi</p></body></html>"


class Env(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        (self.root / "images").mkdir()
        (self.root / "images" / "share.png").write_bytes(png(1200, 630))

    def tearDown(self):
        self.td.cleanup()

    def score(self, html, **kw):
        kw.setdefault("path", self.root / "roof-repair.html")
        kw.setdefault("root", self.root)
        kw.setdefault("site_url", "https://site.com")
        kw.setdefault("brand", "Acme Roofing")
        kw.setdefault("page_name", "Roof Repair")
        return S.score_page(html, **kw)

    def with_site(self, blob, title, desc):
        item = {"rel": "p.html", "before": blob, "old_title": title, "old_description": desc,
                "new_title": title, "new_description": desc}
        S.apply_site_checks([item])
        return blob


class TestScoring(Env):
    def test_model_totals(self):
        self.assertEqual(sum(m for _, _, m in S.CATS), 100)
        for cat, _, mx in S.CATS:
            self.assertEqual(sum(c[3] for c in S.CHECKS if c[1] == cat), mx, cat)
        auto = sum(c[3] for c in S.CHECKS if c[4] == "A")
        self.assertEqual(auto, 83)

    def test_good_page_scores_high_and_explains(self):
        b = self.with_site(self.score(GOOD), "Roof Repair in Miami | Acme Roofing", "x")
        self.assertGreaterEqual(b["t"], 94, b["r"])
        self.assertEqual(b["l"] in ("Very good", "Excellent"), True)
        self.assertEqual(b["crit"], [])
        rows = S.explain(b)
        self.assertEqual(len(rows), len(S.CHECKS))
        self.assertTrue(all(r["earned"] is None for r in rows if r["kind"] == "AI"))   # not judged in free mode

    def test_bad_page_scores_low_with_critical_cap(self):
        b = self.with_site(self.score(BAD), "", "")
        self.assertLess(b["t"], 35)
        self.assertIn("no_title", b["crit"])
        self.assertEqual(b["cap"], 79)
        self.assertEqual(b["l"], "Poor")
        self.assertIn("title.exists", b["r"])
        self.assertIn("canon.present", b["man"])                 # canonical must be fixed by the user

    def test_noindex_intended_vs_unintended(self):
        html = GOOD.replace('content="index, follow"', 'content="noindex, follow"')
        un = self.score(html, intended_noindex=False)
        ok = self.score(html, intended_noindex=True)
        self.assertIn("noindex", un["crit"])
        self.assertEqual(un["cap"], 69)
        self.assertLessEqual(un["t"], 69)
        self.assertNotIn("noindex", ok["crit"])
        self.assertGreater(ok["t"], un["t"])

    def test_foreign_canonical_and_broken_schema(self):
        html = GOOD.replace("https://site.com/roof-repair/\">", "https://other.com/x/\">", 1)
        b = self.score(html)
        self.assertIn("canonical_foreign", b["crit"])
        self.assertLessEqual(b["t"], 79)
        broken = GOOD.replace('"provider"', '"provider",,')
        b2 = self.score(broken)
        self.assertIn("schema_broken", b2["crit"])
        self.assertEqual(b2["f"][S.CHECK_INDEX["schema.valid"]], 0)

    def test_og_image_missing_file_flagged(self):
        html = GOOD.replace("https://site.com/images/share.png", "https://site.com/images/nope.png")
        b = self.score(html)
        self.assertIn("og_image_bad", b["crit"])
        self.assertIn("social.image", b["man"])

    def test_ai_checks_rescaled_when_off_and_used_when_judged(self):
        b = self.with_site(self.score(GOOD), "Roof Repair in Miami | Acme Roofing", "x")
        free = b["t"]
        judged = {c[0]: 1.0 for c in S.CHECKS if c[4] == "AI"}
        S.ai_fill(b, judged)
        self.assertTrue(b["use_ai"])
        self.assertGreaterEqual(b["t"], free - 0.5)               # perfect AI marks keep a good page ~equal
        zero = self.with_site(self.score(GOOD), "Roof Repair in Miami | Acme Roofing", "x")
        S.ai_fill(zero, {c[0]: 0.0 for c in S.CHECKS if c[4] == "AI"})
        self.assertLess(zero["t"], free - 10)                     # AI marks of 0 pull the same page down
        # recompute without AI ignores the judged values again
        self.assertEqual(S.recompute(zero, use_ai=False)["t"], free)

    def test_na_checks_are_rescaled_not_zeroed(self):
        no_brand = self.with_site(self.score(GOOD, brand=""), "Roof Repair in Miami", "x")
        self.assertEqual(no_brand["f"][S.CHECK_INDEX["content.brand"]], -1)
        self.assertGreaterEqual(no_brand["t"], 93)

    def test_uniqueness_is_exact_and_flags_duplicates(self):
        a = self.score(GOOD)
        b = self.score(GOOD)
        c = self.score(GOOD)
        items = [{"rel": "a", "before": a, "old_title": "Same Title", "old_description": "d1",
                  "new_title": "Unique A", "new_description": "dA"},
                 {"rel": "b", "before": b, "old_title": "Same Title", "old_description": "d2",
                  "new_title": "Unique B", "new_description": "dB"},
                 {"rel": "c", "before": c, "old_title": "Other", "old_description": "d3",
                  "new_title": "Unique C", "new_description": "dC"}]
        S.apply_site_checks(items)
        self.assertEqual(a["f"][S.CHECK_INDEX["title.unique"]], 0)
        self.assertIn("dup_title", a["crit"])
        self.assertEqual(c["f"][S.CHECK_INDEX["title.unique"]], 100)
        S.apply_site_checks(items)                                # idempotent
        self.assertEqual(a["crit"].count("dup_title"), 1)

    def test_stuffed_title_penalised(self):
        stuffed = GOOD.replace("<title>Roof Repair in Miami | Acme Roofing</title>",
                               "<title>ROOF REPAIR | ROOF REPAIR | ROOF REPAIR | Roof Repair Miami</title>")
        b = self.score(stuffed)
        self.assertLess(b["f"][S.CHECK_INDEX["title.natural"]], 60)

    def test_labels(self):
        self.assertEqual([S.label_for(x) for x in (100, 97, 96.4, 90, 89, 75, 74, 50, 49, 0)],
                         ["Excellent", "Excellent", "Very good", "Very good", "Good", "Good",
                          "Needs improvement", "Needs improvement", "Poor", "Poor"])


class TestAggregate(Env):
    def pair(self, rel, before_html, after_html, bt="T1", at="T1b"):
        b, a = self.score(before_html), self.score(after_html)
        return {"rel": rel, "before": b, "after": a, "old_title": bt, "new_title": at,
                "old_description": "d" + rel, "new_description": "e" + rel}

    def test_improvement_maths_and_decreased(self):
        items = [self.pair("p1.html", BAD, GOOD, "", "Roof Repair in Miami | Acme Roofing"),
                 self.pair("p2.html", GOOD, BAD, "T2", ""),
                 self.pair("p3.html", GOOD, GOOD, "T3", "T3b")]
        S.apply_site_checks(items)
        agg = S.aggregate(items, target=90)
        self.assertEqual(agg["pages"], 3)
        b = [it["before"]["t"] for it in items]
        a = [it["after"]["t"] for it in items]
        self.assertAlmostEqual(agg["avg_before"], round(sum(b) / 3, 1), places=1)
        self.assertAlmostEqual(agg["avg_after"], round(sum(a) / 3, 1), places=1)
        self.assertAlmostEqual(agg["delta"], round(sum(a) / 3 - sum(b) / 3, 1), places=1)
        self.assertEqual([d[0] for d in agg["decreased"]], ["p2.html"])
        self.assertIn("decreased", items[1]["after"]["crit"])
        self.assertEqual(agg["target_after"], sum(1 for x in a if x >= 90))
        self.assertEqual(len(agg["categories"]), 7)
        self.assertEqual(sum(agg["dist_after"].values()), 3)
        self.assertTrue(agg["top_issues"])

    def test_unchanged_page_is_not_decreased_and_review_pages_excluded(self):
        same = self.pair("p.html", GOOD, GOOD, "Same", "Same")
        review = {"rel": "r.html", "before": self.score(BAD), "after": None, "old_title": "", "new_title": "",
                  "old_description": "", "new_description": ""}
        items = [same, review]
        S.apply_site_checks(items)
        agg = S.aggregate(items)
        self.assertEqual(agg["pages"], 1)
        self.assertEqual(agg["pages_before_only"], 1)
        self.assertEqual(agg["decreased"], [])
        self.assertEqual(agg["delta"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
