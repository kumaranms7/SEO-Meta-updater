"""Step 1 inside a run: canonical tags first, redirected / gone pages left out, honest before/after scores."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seo_ai as A  # noqa: E402
import seo_canonical as K  # noqa: E402
import seo_core as C  # noqa: E402
import seo_pipeline as P  # noqa: E402
import test_protect as TP  # noqa: E402
import test_score as T  # noqa: E402

_REAL_BACKUP_DIR = C.default_backup_dir
NO_CANON = T.GOOD.replace('<link rel="canonical" href="https://site.com/roof-repair/">\n', "")
GOOD_TITLE = "Roof Repair in Miami, Florida"
HTACCESS = "Redirect 301 /moved.html https://site.com/roof-repair.html\nRedirect 410 /gone.html\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        C.STATE_DIR = Path(self.td.name) / "state"
        C.default_backup_dir = lambda root: Path(self.td.name) / "bk"       # never share backups between tests
        self.root = Path(self.td.name) / "site"
        (self.root / "images").mkdir(parents=True)
        (self.root / "images" / "share.png").write_bytes(T.png(1200, 630))
        self.page = self.root / "roof-repair.html"
        self.page.write_text(NO_CANON, encoding="utf-8")
        (self.root / "moved.html").write_text(
            NO_CANON.replace("</head>", '<link rel="canonical" href="https://site.com/moved.html">\n</head>'),
            encoding="utf-8")
        (self.root / "gone.html").write_text(NO_CANON, encoding="utf-8")
        (self.root / "roof-repair - Copy.html").write_text(NO_CANON, encoding="utf-8")
        (self.root / ".htaccess").write_text(HTACCESS, encoding="utf-8")
        self.orig = {f.name: f.read_bytes() for f in self.root.glob("*.html")}

    def tearDown(self):
        C.default_backup_dir = _REAL_BACKUP_DIR
        self.td.cleanup()

    def run_it(self, mode="per_page", **kw):
        results, logs = [], []
        kw.setdefault("ai_alt", False)
        kw.setdefault("backup", True)
        kw.setdefault("do_canonical", True)
        kw.setdefault("skip_htaccess", True)
        kw.setdefault("exclude_patterns", ["* - Copy.html"])           # (step 2 only — step 1 has its own rule)
        ai = TP.FakeAI({"meta_title": GOOD_TITLE, "meta_description": TP.ORIG_DESC, "h1": "Roof Repair in Miami"})
        opts = P.RunOptions(folder=str(self.root), site_url="https://site.com", brand="Acme Roofing", mode=mode, **kw)
        pipe = P.Pipeline(opts, A.AI(ai), C.collect_html_files(self.root), lambda m, t="": logs.append(m),
                          results.append, lambda d, t: None, threading.Event())
        pipe.run()
        return {r["rel"]: r for r in results}, logs, pipe

    def disk(self, name):
        return (self.root / name).read_text(encoding="utf-8")


class TestLive(Base):
    def test_canonical_is_added_first_and_redirected_pages_are_left_out(self):
        res, logs, pipe = self.run_it()
        self.assertIn('<link rel="canonical" href="https://site.com/roof-repair.html">', self.disk("roof-repair.html"))
        r = res["roof-repair.html"]
        self.assertEqual(r["status"], "updated")
        self.assertEqual(r["canonical_action"], "added")
        self.assertEqual(r["canonical"], "https://site.com/roof-repair.html")
        self.assertNotIn("no canonical tag", " ".join(r["notes"]))        # og:url / schema url can now be built
        for name, kind in (("moved.html", "301"), ("gone.html", "410")):
            self.assertEqual(res[name]["status"], "skipped", name)
            self.assertIn(K.KIND_TEXT[kind], res[name]["reason"])
        self.assertEqual(res["moved.html"]["canonical_action"], "removed")   # a canonical on a redirected page is removed
        self.assertNotIn("canonical", self.disk("moved.html"))
        self.assertEqual((self.root / "gone.html").read_bytes(), self.orig["gone.html"])     # nothing to remove
        self.assertEqual((self.root / "roof-repair - Copy.html").read_bytes(), self.orig["roof-repair - Copy.html"])
        self.assertTrue(any("Step 1 of 2" in m for m in logs))

    def test_before_score_is_the_original_page_so_the_canonical_gain_shows(self):
        res, _, _ = self.run_it()
        r = res["roof-repair.html"]
        b, a = r["score_before"], r["score_after"]
        self.assertGreater(a["cats"]["canon"], b["cats"]["canon"])
        self.assertGreaterEqual(a["t"], b["t"])

    def test_backups_and_progress_file_keep_the_originals(self):
        res, _, pipe = self.run_it()
        bdir = Path(pipe.state.backup_dir)
        self.assertEqual((bdir / "roof-repair.html").read_bytes(), self.orig["roof-repair.html"])
        self.assertEqual((bdir / "moved.html").read_bytes(), self.orig["moved.html"])
        self.assertIn("roof-repair.html", pipe.state.data["canon"])

    def test_resume_keeps_the_page_done_and_does_not_redo_step_1(self):
        self.run_it()
        before = self.disk("roof-repair.html")
        res, logs, _ = self.run_it(run_kind="resume")
        self.assertNotIn("roof-repair.html", res)                       # already finished
        self.assertEqual(self.disk("roof-repair.html"), before)

    def test_without_a_site_address_step_1_is_skipped(self):
        results, logs = [], []
        ai = TP.FakeAI({"meta_title": GOOD_TITLE, "meta_description": TP.ORIG_DESC, "h1": "Roof Repair in Miami"})
        opts = P.RunOptions(folder=str(self.root), site_url="", brand="Acme Roofing", mode="per_page", ai_alt=False,
                            backup=False, do_canonical=True, skip_htaccess=True)
        P.Pipeline(opts, A.AI(ai), [str(self.page)], lambda m, t="": logs.append(m), results.append,
                   lambda d, t: None, threading.Event()).run()
        self.assertTrue(any("set the website address" in m for m in logs))
        self.assertNotIn('rel="canonical"', self.disk("roof-repair.html"))


class TestPopup(Base):
    def run_with(self, answer, **kw):
        seen = []
        results, logs = [], []
        ai = TP.FakeAI({"meta_title": GOOD_TITLE, "meta_description": TP.ORIG_DESC, "h1": "Roof Repair in Miami"})
        opts = P.RunOptions(folder=str(self.root), site_url="https://site.com", brand="Acme Roofing", mode="per_page",
                            ai_alt=False, backup=True, do_canonical=True, skip_htaccess=True,
                            exclude_patterns=["* - Copy.html"], **kw)
        pipe = P.Pipeline(opts, A.AI(ai), C.collect_html_files(self.root), lambda m, t="": logs.append(m),
                          results.append, lambda d, t: None, threading.Event(),
                          on_canonical_done=lambda st: (seen.append(st), answer)[1])
        pipe.run()
        return seen, results, ai, pipe

    def test_continue_runs_the_seo_update_after_the_stats(self):
        seen, results, ai, pipe = self.run_with(True)
        self.assertEqual(len(seen), 1)
        st = seen[0]
        self.assertEqual((st["insert"], st["removed"], st["redirected"], st["301"], st["410"]), (1, 1, 2, 1, 1))
        self.assertEqual(st["next_pages"], st["checked"] - 2)
        self.assertGreater(ai.gen_calls, 0)
        self.assertTrue(results)
        self.assertFalse(pipe.canonical_only)

    def test_cancel_keeps_the_canonical_tags_and_does_no_seo_update(self):
        seen, results, ai, pipe = self.run_with(False)
        self.assertIn('<link rel="canonical" href="https://site.com/roof-repair.html">', self.disk("roof-repair.html"))
        self.assertEqual(ai.gen_calls, 0)                       # no AI call, no cost
        self.assertEqual(results, [])
        self.assertTrue(pipe.canonical_only)
        self.assertNotIn("<title>Roof Repair in Miami, Florida", self.disk("roof-repair.html"))

    def test_popup_is_not_asked_when_step_1_is_off(self):
        results, seen = [], []
        ai = TP.FakeAI({"meta_title": GOOD_TITLE, "meta_description": TP.ORIG_DESC, "h1": "Roof Repair in Miami"})
        opts = P.RunOptions(folder=str(self.root), site_url="https://site.com", brand="Acme Roofing", mode="per_page",
                            ai_alt=False, backup=False)
        P.Pipeline(opts, A.AI(ai), [str(self.page)], lambda m, t="": None, results.append, lambda d, t: None,
                   threading.Event(), on_canonical_done=lambda st: seen.append(st) or False).run()
        self.assertEqual(seen, [])
        self.assertTrue(results)

    def test_dry_run_popup_says_nothing_was_written(self):
        results, seen = [], []
        ai = TP.FakeAI({"meta_title": GOOD_TITLE, "meta_description": TP.ORIG_DESC, "h1": "Roof Repair in Miami"})
        opts = P.RunOptions(folder=str(self.root), site_url="https://site.com", brand="Acme Roofing", mode="dry_run",
                            ai_alt=False, do_canonical=True, skip_htaccess=True, exclude_patterns=["* - Copy.html"])
        P.Pipeline(opts, A.AI(ai), C.collect_html_files(self.root), lambda m, t="": None, results.append,
                   lambda d, t: None, threading.Event(), on_canonical_done=lambda st: seen.append(st) or False).run()
        self.assertTrue(seen[0]["dry"])
        for name, data in self.orig.items():
            self.assertEqual((self.root / name).read_bytes(), data, name)


class TestLinks(Base):
    def setUp(self):
        super().setUp()
        link_html = '<a href="/moved.html">m</a> <a href="gone.html">g</a> <a href="/fine.html">f</a>'
        self.page.write_text(NO_CANON.replace("<main>", "<main>" + link_html), encoding="utf-8")
        (self.root / "menu.html").write_text('<ul><li><a href="/moved.html">Moved</a></li></ul>', encoding="utf-8")
        (self.root / "roof-repair - Copy.html").write_text(NO_CANON.replace("<main>", '<main><a href="/moved.html">c</a>'),
                                                           encoding="utf-8")
        self.orig = {f.name: f.read_bytes() for f in self.root.glob("*.html")}

    def run_links(self, mode="per_page", **kw):
        seen = []
        results, logs = [], []
        ai = TP.FakeAI({"meta_title": GOOD_TITLE, "meta_description": TP.ORIG_DESC, "h1": "Roof Repair in Miami"})
        kw.setdefault("fix_links", True)
        opts = P.RunOptions(folder=str(self.root), site_url="https://site.com", brand="Acme Roofing", mode=mode,
                            ai_alt=False, backup=True, do_canonical=True, skip_htaccess=True,
                            exclude_patterns=["* - Copy.html"], **kw)
        pipe = P.Pipeline(opts, A.AI(ai), C.collect_html_files(self.root), lambda m, t="": logs.append(m),
                          results.append, lambda d, t: None, threading.Event(),
                          on_canonical_done=lambda st: seen.append(st) or False)
        pipe.run()
        return seen, pipe

    def test_links_are_fixed_in_pages_and_partials_but_not_in_copies_or_redirected_pages(self):
        self.run_links()
        page = self.disk("roof-repair.html")
        self.assertIn('href="/roof-repair.html">m</a>', page)                 # 301 link now goes straight to the page
        self.assertIn('href="gone.html"', page)                              # 410 link only reported
        self.assertIn('href="/fine.html"', page)
        self.assertIn('href="/roof-repair.html"', self.disk("menu.html"))     # a partial / menu include is fixed too
        self.assertEqual((self.root / "roof-repair - Copy.html").read_bytes(), self.orig["roof-repair - Copy.html"])

    def test_originals_are_backed_up_and_default_is_off(self):
        seen, pipe = self.run_links()
        self.assertEqual((Path(pipe.state.backup_dir) / "menu.html").read_bytes(), self.orig["menu.html"])
        self.td.cleanup()
        self.setUp()
        seen, pipe = self.run_links(fix_links=False)
        self.assertFalse(seen[0]["links"])
        self.assertEqual((self.root / "menu.html").read_bytes(), self.orig["menu.html"])

    def test_dry_run_only_lists_the_changes(self):
        seen, pipe = self.run_links(mode="dry_run")
        for name, data in self.orig.items():
            self.assertEqual((self.root / name).read_bytes(), data, name)
        self.assertIn("would change", Path(pipe.links_csv).read_text(encoding="utf-8-sig"))

    def test_stats_and_csv(self):
        seen, pipe = self.run_links()
        st = seen[0]
        self.assertTrue(st["links"])
        self.assertGreaterEqual(st["links_fixed"], 1)
        self.assertEqual(st["links_gone"], 1)
        self.assertTrue(pipe.links_csv)
        text = Path(pipe.links_csv).read_text(encoding="utf-8-sig")
        self.assertIn("gone (410)", text)
        self.assertIn("/roof-repair.html", text)


class TestDry(Base):
    def test_dry_run_changes_nothing_but_reports_the_same_result(self):
        res, logs, _ = self.run_it(mode="dry_run")
        for name, data in self.orig.items():
            self.assertEqual((self.root / name).read_bytes(), data, name)
        r = res["roof-repair.html"]
        self.assertEqual((r["status"], r["canonical_action"]), ("preview", "added"))
        self.assertEqual(r["canonical"], "https://site.com/roof-repair.html")
        self.assertGreater(r["score_after"]["cats"]["canon"], r["score_before"]["cats"]["canon"])
        self.assertEqual(res["gone.html"]["status"], "skipped")

    def test_dry_matches_live(self):
        dry, _, _ = self.run_it(mode="dry_run")
        self.td.cleanup()
        self.setUp()
        live, _, _ = self.run_it()
        d, l = dry["roof-repair.html"], live["roof-repair.html"]
        self.assertEqual(d["score_before"]["t"], l["score_before"]["t"])
        self.assertEqual(d["score_after"]["t"], l["score_after"]["t"])
        self.assertEqual(d["canonical"], l["canonical"])


class TestOptions(Base):
    def test_only_skipping_redirected_pages(self):
        res, _, _ = self.run_it(do_canonical=False)
        self.assertEqual(res["moved.html"]["status"], "skipped")
        self.assertEqual(res["roof-repair.html"]["canonical_action"], "")
        self.assertNotIn('rel="canonical"', self.disk("roof-repair.html"))
        self.assertEqual((self.root / "moved.html").read_bytes(), self.orig["moved.html"])   # canonical NOT removed

    def test_redirected_pages_are_still_processed_when_skipping_is_off(self):
        res, _, _ = self.run_it(skip_htaccess=False)
        self.assertNotEqual(res["moved.html"]["status"], "skipped")

    def test_foreign_canonical_is_kept_and_flagged(self):
        self.page.write_text(NO_CANON.replace("</head>", '<link rel="canonical" href="https://other.com/x.html">\n</head>'),
                             encoding="utf-8")
        res, _, _ = self.run_it()
        r = res["roof-repair.html"]
        self.assertEqual(r["canonical_action"], "other domain")
        self.assertIn("https://other.com/x.html", self.disk("roof-repair.html"))
        self.assertTrue(any("another domain" in n for n in r["notes"]))

    def test_canonical_never_makes_the_score_lower(self):
        res, _, _ = self.run_it()
        r = res["roof-repair.html"]
        for c, v in r["score_before"]["cats"].items():
            self.assertGreaterEqual(r["score_after"]["cats"][c], v - 0.005, c)


if __name__ == "__main__":
    unittest.main()
