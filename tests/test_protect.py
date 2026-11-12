"""Score protection: a page is never written with a LOWER score than it had."""
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
import seo_score as S  # noqa: E402
import test_score as T  # noqa: E402

ORIG_DESC = ("Fast roof repair in Miami for leaks, storm damage and worn shingles, with clear pricing from "
             "Acme Roofing. Call today for a free quote.")
GOOD_TITLE = "Roof Repair in Miami, Florida"                # covers the page topic (roof, repair)
BAD_TITLE = "Dependable Services Across Greater Miami"      # says nothing about the topic -> lower score


class FakeAI:
    """Generation returns the given values; every 'LOWERED its SEO score' prompt returns the next regen reply."""

    def __init__(self, gen: dict, regen=None):
        self.gen, self.regen = gen, list(regen or [])
        self.messages = self
        self.gen_calls = 0
        self.regen_prompts = []

    def stream(self, model, max_tokens, messages):
        prompt = messages[0]["content"]
        if "PAGE_1:" in prompt:
            self.gen_calls += 1
            text = json.dumps([{"page": "PAGE_1", "schema_type": "WebPage", **self.gen}])
        elif "LOWERED its SEO score" in prompt:
            self.regen_prompts.append(prompt)
            idx = min(len(self.regen_prompts) - 1, len(self.regen) - 1)
            text = json.dumps(self.regen[idx] if self.regen else {})
        else:
            text = "{}"

        class Ctx:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def get_final_message(s):
                return SimpleNamespace(content=[SimpleNamespace(text=text)],
                                       usage=SimpleNamespace(input_tokens=100, output_tokens=40))
        return Ctx()


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        C.STATE_DIR = Path(self.td.name) / "state"
        self.root = Path(self.td.name) / "site"
        (self.root / "images").mkdir(parents=True)
        (self.root / "images" / "share.png").write_bytes(T.png(1200, 630))
        self.page = self.root / "roof-repair.html"

    def tearDown(self):
        self.td.cleanup()

    def write_page(self, html=None):
        self.page.write_text(html or T.GOOD, encoding="utf-8")
        return self.page.read_text(encoding="utf-8")

    def run_it(self, ai, mode="per_page", **kw):
        results, logs = [], []
        kw.setdefault("ai_alt", False)
        kw.setdefault("backup", False)
        opts = P.RunOptions(folder=str(self.root), site_url="https://site.com", brand="Acme Roofing", mode=mode, **kw)
        pipe = P.Pipeline(opts, A.AI(ai), C.collect_html_files(self.root), lambda m, t="": logs.append(m),
                          results.append, lambda d, t: None, threading.Event())
        pipe.run()
        return results[0], logs, pipe


class TestScoreProtection(Base):
    def test_good_first_fetch_needs_no_retry(self):
        self.write_page()
        ai = FakeAI({"meta_title": GOOD_TITLE, "meta_description": ORIG_DESC, "h1": "Roof Repair in Miami"})
        r, logs, _ = self.run_it(ai)
        self.assertEqual(ai.regen_prompts, [])
        self.assertEqual(r["attempts"], 1)
        self.assertGreaterEqual(r["score_after"]["t"], r["score_before"]["t"])
        self.assertEqual(r["status"], "updated")
        self.assertIn(GOOD_TITLE, self.page.read_text(encoding="utf-8"))

    def test_bad_title_is_refetched_alone_and_then_accepted(self):
        self.write_page()
        ai = FakeAI({"meta_title": BAD_TITLE, "meta_description": ORIG_DESC, "h1": "Roof Repair in Miami"},
                    regen=[{"meta_title": "Roof Repair in Miami Florida"}])
        r, logs, _ = self.run_it(ai)
        self.assertEqual(len(ai.regen_prompts), 1)
        prompt = ai.regen_prompts[0]
        self.assertIn("rewrite ONLY these fields: meta_title", prompt)             # only the bad field is asked for
        self.assertNotIn("rewrite ONLY these fields: meta_title, meta_description", prompt)
        self.assertIn("WHY IT SCORED LOWER", prompt)
        self.assertIn("Title covers the page topic", prompt)                        # the real reason is passed on
        self.assertIn(BAD_TITLE, prompt)                                            # ...together with the failed attempt
        self.assertEqual(r["attempts"], 2)
        self.assertGreaterEqual(r["score_after"]["t"], r["score_before"]["t"])
        self.assertTrue(r["new_title"].startswith("Roof Repair in Miami Florida"))
        self.assertNotIn(BAD_TITLE, self.page.read_text(encoding="utf-8"))          # the bad version never reached the file

    def test_stops_as_soon_as_the_score_is_good_enough(self):
        self.write_page()
        ai = FakeAI({"meta_title": BAD_TITLE, "meta_description": ORIG_DESC, "h1": "Roof Repair in Miami"},
                    regen=[{"meta_title": "Roof Repair in Miami Florida"}, {"meta_title": "should never be asked for"}])
        r, _, _ = self.run_it(ai, max_retries=3)
        self.assertEqual(len(ai.regen_prompts), 1)                                  # 3 retries are a limit, not a quota

    def test_every_retry_bad_puts_the_original_back_and_never_lowers_the_score(self):
        original = self.write_page()
        ai = FakeAI({"meta_title": BAD_TITLE, "meta_description": ORIG_DESC, "h1": "Roof Repair in Miami"},
                    regen=[{"meta_title": "Trusted Local Experts You Can Call"},
                           {"meta_title": "Quality Workmanship Since Forever"},
                           {"meta_title": "Honest Help With Your Home"}])
        r, logs, _ = self.run_it(ai, max_retries=3, mode="dry_run")
        self.assertEqual(len(ai.regen_prompts), 3)                                  # first fetch + 3 retries
        self.assertEqual(r["attempts"], 4)
        self.assertGreaterEqual(r["score_after"]["t"], r["score_before"]["t"])      # never negative
        self.assertEqual(r["new_title"], r["old_title"])                            # the original title is back
        self.assertTrue(r["protected"])
        self.assertIn("kept the original title", r["retry_note"])

    def test_max_retries_is_respected(self):
        self.write_page()
        ai = FakeAI({"meta_title": BAD_TITLE, "meta_description": ORIG_DESC, "h1": "Roof Repair in Miami"},
                    regen=[{"meta_title": "Trusted Local Experts You Can Call"}] * 5)
        r, _, _ = self.run_it(ai, max_retries=1, mode="dry_run")
        self.assertEqual(len(ai.regen_prompts), 1)
        self.assertEqual(r["attempts"], 2)
        self.assertGreaterEqual(r["score_after"]["t"], r["score_before"]["t"])

    def test_page_is_left_untouched_when_no_version_can_match_the_old_score(self):
        original = self.write_page()
        ai = FakeAI({"meta_title": BAD_TITLE, "meta_description": ORIG_DESC, "h1": "Roof Repair in Miami"},
                    regen=[{"meta_title": "Trusted Local Experts You Can Call"}] * 3)
        # only the AI fields may change — so putting the original title back leaves the page identical
        r, logs, _ = self.run_it(ai, do_schema=False, do_og=False, do_robots=False, do_lazy=False, do_alt=False)
        self.assertEqual(self.page.read_text(encoding="utf-8"), original)           # file not touched
        self.assertEqual(r["status"], "kept")
        self.assertEqual(r["new_title"], r["old_title"])
        self.assertEqual(r["score_after"]["t"], r["score_before"]["t"])
        self.assertTrue(r["protected"])
        st = C.RunState(self.root)
        self.assertEqual(st.files["roof-repair.html"]["status"], "done")             # resume will not redo it
        self.assertEqual(st.files["roof-repair.html"].get("kept"), 1)

    def test_protection_can_be_switched_off(self):
        self.write_page()
        ai = FakeAI({"meta_title": BAD_TITLE, "meta_description": ORIG_DESC, "h1": "Roof Repair in Miami"},
                    regen=[{"meta_title": "Roof Repair in Miami Florida"}])
        r, _, _ = self.run_it(ai, protect_score=False)
        self.assertEqual(ai.regen_prompts, [])
        self.assertLess(r["score_after"]["t"], r["score_before"]["t"])              # the old behaviour: the drop is written
        self.assertIn(BAD_TITLE, self.page.read_text(encoding="utf-8"))

    def test_no_protection_without_scoring(self):
        self.write_page()
        ai = FakeAI({"meta_title": BAD_TITLE, "meta_description": ORIG_DESC, "h1": "Roof Repair in Miami"})
        r, _, _ = self.run_it(ai, do_score=False)
        self.assertEqual(ai.regen_prompts, [])
        self.assertIsNone(r["score_after"])

    def test_dry_run_gives_the_same_answer_and_writes_nothing(self):
        original = self.write_page()
        ai = FakeAI({"meta_title": BAD_TITLE, "meta_description": ORIG_DESC, "h1": "Roof Repair in Miami"},
                    regen=[{"meta_title": "Roof Repair in Miami Florida"}])
        r, _, pipe = self.run_it(ai, mode="dry_run")
        self.assertEqual(self.page.read_text(encoding="utf-8"), original)
        self.assertEqual(r["status"], "preview")
        self.assertEqual(r["attempts"], 2)
        self.assertGreaterEqual(r["score_after"]["t"], r["score_before"]["t"])

    def test_invalid_regenerated_text_is_rejected_not_used(self):
        self.write_page()
        too_long = "Roof Repair in Miami " + "and Surrounding Neighbourhoods " * 3
        ai = FakeAI({"meta_title": BAD_TITLE, "meta_description": ORIG_DESC, "h1": "Roof Repair in Miami"},
                    regen=[{"meta_title": too_long}, {"meta_title": "Roof Repair in Miami Florida"}])
        r, _, _ = self.run_it(ai, mode="dry_run")
        self.assertNotIn(too_long.strip(), r["new_title"])
        self.assertLessEqual(len(r["new_title"]), 60)
        self.assertGreaterEqual(r["score_after"]["t"], r["score_before"]["t"])


class TestFieldLocking(Base):
    """Two fields change; only the one that hurts is fetched again, the other stays."""

    def page_with_weak_description(self):
        html = T.GOOD.replace(ORIG_DESC, "Roof repair in Miami from Acme Roofing.")        # short, weaker description
        html = html.replace('<meta property="og:description" content="%s">' % ORIG_DESC,
                            '<meta property="og:description" content="Roof repair in Miami from Acme Roofing.">')
        return self.write_page(html)

    def test_good_description_is_locked_while_the_title_is_refetched(self):
        self.page_with_weak_description()
        better_desc = ("Licensed roofers fixing leaks, storm damage and worn shingles for Miami homes, with free "
                       "inspections and honest pricing. Call Acme Roofing today.")
        ai = FakeAI({"meta_title": BAD_TITLE, "meta_description": better_desc, "h1": "Roof Repair in Miami"},
                    regen=[{"meta_title": "Roof Repair in Miami Florida"}])
        r, logs, _ = self.run_it(ai, mode="dry_run")
        self.assertGreaterEqual(r["score_after"]["t"], r["score_before"]["t"])
        for c, v in r["score_before"]["cats"].items():                               # no category may go down
            self.assertGreaterEqual(r["score_after"]["cats"][c], v - 0.005)
        if ai.regen_prompts and "rewrite ONLY these fields: meta_title." in ai.regen_prompts[0]:
            self.assertIn("ALREADY GOOD", ai.regen_prompts[0])


class TestFeedbackAndHelpers(unittest.TestCase):
    def test_feedback_lists_only_checks_that_got_worse(self):
        names = [c[0] for c in S.CHECKS]
        before = {"f": [100] * len(names), "r": {}}
        after = {"f": [100] * len(names), "r": {"title.topic": "missing topic words: roof, repair"}}
        after["f"][names.index("title.topic")] = 40
        after["f"][names.index("desc.length")] = 50          # a description check is irrelevant when only the title is asked for
        lines = P.Pipeline._feedback(before, after, ["title"])
        self.assertEqual(len(lines), 1)
        self.assertIn("Title covers the page topic: 100% → 40%", lines[0])
        self.assertIn("missing topic words: roof, repair", lines[0])

    def test_topic_no_longer_locks_in_the_original_h1(self):
        topic = S.topic_terms("Backflow Prevention", "", "Totally Different Heading Words", [])
        self.assertEqual(topic[:2], ["backflow", "prevention"])
        self.assertNotIn("totally", topic)                   # the H1 is only used when the URL name says too little
        sparse = S.topic_terms("Contact Us", "", "Contact Example Plumbing Today", [])
        self.assertIn("exampl", " ".join(sparse))             # ...but it fills the gap when it does
        self.assertEqual(S.SCORING_VERSION, "1.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
