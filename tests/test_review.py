"""Review before writing: pages that failed the checks can be edited / regenerated / approved one by one."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seo_ai as A  # noqa: E402
import seo_core as C  # noqa: E402
import seo_pipeline as P  # noqa: E402
import test_protect as TP  # noqa: E402
import test_score as T  # noqa: E402

TOO_LONG = "Roof Repair in Miami " + "and Surrounding Neighbourhoods " * 3
GOOD_TITLE = "Roof Repair in Miami, Florida"
NEW_DESC = ("Licensed roofers fixing leaks, storm damage and worn shingles for Miami homes, with free inspections and "
            "honest pricing. Call Acme Roofing today.")


class ReviewAI(TP.FakeAI):
    """Generation returns an over-long title; review prompts return the next scripted reply."""

    def __init__(self, *a, review=None, **k):
        super().__init__(*a, **k)
        self.review_replies = list(review or [])
        self.review_prompts = []

    def stream(self, model, max_tokens, messages):
        prompt = messages[0]["content"]
        if "The editor reviewing this page asked for a NEW version" in prompt:
            self.review_prompts.append(prompt)
            text = json.dumps(self.review_replies[min(len(self.review_prompts) - 1, len(self.review_replies) - 1)])

            class Ctx:
                def __enter__(s): return s
                def __exit__(s, *a): return False
                def get_final_message(s):
                    return SimpleNamespace(content=[SimpleNamespace(text=text)],
                                           usage=SimpleNamespace(input_tokens=100, output_tokens=40))
            return Ctx()
        return super().stream(model, max_tokens, messages)


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        C.STATE_DIR = Path(self.td.name) / "state"
        self._real = C.default_backup_dir
        C.default_backup_dir = lambda root: Path(self.td.name) / "bk"
        self.root = Path(self.td.name) / "site"
        (self.root / "images").mkdir(parents=True)
        (self.root / "images" / "share.png").write_bytes(T.png(1200, 630))
        self.page = self.root / "roof-repair.html"
        self.page.write_text(T.GOOD, encoding="utf-8")
        self.orig = self.page.read_text(encoding="utf-8")

    def tearDown(self):
        C.default_backup_dir = self._real
        self.td.cleanup()

    def opts(self, **kw):
        kw.setdefault("mode", "per_page")
        return P.RunOptions(folder=str(self.root), site_url="https://site.com", brand="Acme Roofing", ai_alt=False,
                            backup=True, protect_score=False, **kw)

    def first_run(self, ai, **kw):
        results = []
        pipe = P.Pipeline(self.opts(**kw), A.AI(ai), C.collect_html_files(self.root), lambda m, t="": None,
                          results.append, lambda d, t: None, threading.Event())
        pipe.run()
        return results[0], pipe

    def review_pipe(self, ai, **kw):
        pipe = P.Pipeline(self.opts(**kw), A.AI(ai), [], lambda m, t="": None, lambda r: None, lambda d, t: None,
                          threading.Event())
        pipe.review_session()
        return pipe

    def gen_ai(self, **kw):
        return ReviewAI({"meta_title": TOO_LONG, "meta_description": TP.ORIG_DESC, "h1": "Roof Repair in Miami"}, **kw)

    def failed_page(self):
        r, pipe = self.first_run(self.gen_ai(regen=[{"meta_title": TOO_LONG}] * 3))
        self.assertEqual(r["status"], "review")
        return r, pipe


class TestReview(Base):
    def test_failed_page_keeps_its_candidate_for_review_and_is_not_written(self):
        r, pipe = self.failed_page()
        self.assertEqual(r["review"]["core"].strip(), TOO_LONG.strip())
        self.assertTrue(r["review"]["problems"])
        self.assertEqual(self.page.read_text(encoding="utf-8"), self.orig)
        saved = pipe.state.files["roof-repair.html"]
        self.assertEqual(saved["status"], "review")
        self.assertIn("core", saved["rv"])                                  # survives into a later session

    def test_edit_check_and_approve(self):
        r, _ = self.failed_page()
        pipe = self.review_pipe(self.gen_ai())
        p, err = pipe.review_open("roof-repair.html")
        self.assertEqual(err, "")
        p["res"]["review"] = r["review"]                                    # (the window gets it from the saved result)
        vals, probs = pipe.review_check(p, TOO_LONG, NEW_DESC, "Roof Repair in Miami")
        self.assertTrue(probs["title"])                                     # still too long
        vals, probs = pipe.review_check(p, GOOD_TITLE + " | Acme Roofing", NEW_DESC, "Roof Repair in Miami")
        self.assertEqual(probs, {"title": [], "desc": [], "h1": []})
        self.assertEqual(vals["title"], GOOD_TITLE)                         # the brand part is handled by the app
        html, info, blob = pipe.review_preview(p, vals)
        self.assertEqual(self.page.read_text(encoding="utf-8"), self.orig)  # a preview never writes
        self.assertIsNotNone(blob)
        res = pipe.review_apply(p, vals)
        written = self.page.read_text(encoding="utf-8")
        self.assertIn(f"<title>{GOOD_TITLE} | Acme Roofing</title>", written)
        self.assertIn(NEW_DESC, written)
        self.assertEqual(res["status"], "updated")
        self.assertIn("approved after review", res["notes"])
        backup = Path(self.td.name) / "bk" / "roof-repair.html"
        self.assertEqual(backup.read_text(encoding="utf-8"), self.orig)
        self.assertEqual(pipe.state.files["roof-repair.html"]["status"], "done")     # no longer waiting for review

    def test_regenerate_one_field_with_the_reviewers_instruction(self):
        r, _ = self.failed_page()
        ai = self.gen_ai(review=[{"meta_description": NEW_DESC}, {"meta_title": "x" * 90}])
        pipe = self.review_pipe(ai)
        p, _ = pipe.review_open("roof-repair.html")
        p["res"]["review"] = r["review"]
        vals, probs = pipe.review_check(p, GOOD_TITLE, TP.ORIG_DESC, "Roof Repair in Miami")
        text, dprobs = pipe.review_regen(p, "desc", vals, probs["desc"], "mention free inspections")
        self.assertEqual((text, dprobs), (NEW_DESC, []))
        self.assertIn("mention free inspections", ai.review_prompts[0])
        self.assertIn("meta_description", ai.review_prompts[0])
        self.assertIn(GOOD_TITLE, ai.review_prompts[0])                    # the other field is passed as "already decided"
        bad, bprobs = pipe.review_regen(p, "title", vals, [], "")           # a text that fails the checks is still returned
        self.assertEqual(bad, "x" * 90)
        self.assertTrue(bprobs)

    def test_dry_run_approve_writes_nothing(self):
        r, _ = self.failed_page()
        pipe = self.review_pipe(self.gen_ai(), mode="dry_run")
        p, _ = pipe.review_open("roof-repair.html")
        p["res"]["review"] = r["review"]
        vals, _ = pipe.review_check(p, GOOD_TITLE, NEW_DESC, "")
        res = pipe.review_apply(p, vals)
        self.assertEqual(res["status"], "preview")
        self.assertEqual(self.page.read_text(encoding="utf-8"), self.orig)

    def test_refresh_scores_includes_the_approved_page(self):
        r, _ = self.failed_page()
        pipe = self.review_pipe(self.gen_ai())
        p, _ = pipe.review_open("roof-repair.html")
        p["res"]["review"] = r["review"]
        vals, _ = pipe.review_check(p, GOOD_TITLE, NEW_DESC, "Roof Repair in Miami")
        pipe.review_apply(p, vals)
        items, summary = pipe.refresh_scores()
        self.assertEqual(summary["pages"], 1)
        self.assertGreater(summary["avg_after"], 0)

    def test_pending_reviews_come_from_the_progress_file_in_a_later_session(self):
        self.failed_page()
        pend = P.pending_reviews([], C.RunState(str(self.root)))
        self.assertEqual([x["rel"] for x in pend], ["roof-repair.html"])
        self.assertIn("core", pend[0]["review"])


if __name__ == "__main__":
    unittest.main()
