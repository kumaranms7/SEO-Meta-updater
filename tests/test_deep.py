import json
import re
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
from test_pipeline import ScriptedClient, make_site  # noqa: E402


class JudgeClient(ScriptedClient):
    """Normal generation replies + a judge that scores 'after' pages (contain 'Pros in Miami') higher."""

    def __init__(self, mode="ok"):
        super().__init__()
        self.mode = mode
        self.judge_prompts = []

    def stream(self, model, max_tokens, messages):
        prompt = messages[0]["content"]
        if "SEO quality reviewer" not in prompt:
            return super().stream(model, max_tokens, messages)
        self.judge_prompts.append(prompt)
        if self.mode == "boom":
            raise RuntimeError("judge exploded")
        if self.mode == "fatal":
            class AuthErr(Exception):
                status_code = 401
            raise AuthErr("invalid key")
        blocks = re.split(r"ITEM_\d+\n", prompt)[1:]
        out = []
        for i, b in enumerate(blocks, 1):
            good = "Experts in Miami" in b          # the scripted AI's new titles
            v = 9 if good else 3
            out.append({"n": i, "title_intent": v, "desc_accurate": v, "desc_intent": v, "h1_title": v,
                        "schema_type": v, "content_purpose": v, "content_wording": v,
                        "issue": "" if good else "Vague title"})
        text = json.dumps(out)

        class Ctx:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def get_final_message(s):
                return SimpleNamespace(content=[SimpleNamespace(text=text)],
                                       usage=SimpleNamespace(input_tokens=2000, output_tokens=300))
        return Ctx()


class TestDeep(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        C.STATE_DIR = Path(self.td.name) / "state"

    def tearDown(self):
        self.td.cleanup()

    def run_deep(self, client, names=("Roof Repair", "Gutter Cleaning"), **kw):
        root = make_site(self.td.name, list(names))
        logs, got = [], {}
        opts = P.RunOptions(folder=str(root), site_url="https://site.com", brand="Acme", mode="per_page",
                            deep_check=True, **kw)
        pipe = P.Pipeline(opts, A.AI(client), C.collect_html_files(root), lambda m, t="": logs.append(m),
                          lambda r: None, lambda d, t: None, threading.Event(),
                          on_scores=lambda items, summ: got.update(items=items, summ=summ))
        pipe.run()
        return root, pipe, got, logs

    def test_deep_check_adds_ai_points_blind_and_persists(self):
        client = JudgeClient()
        root, pipe, got, logs = self.run_deep(client)
        self.assertTrue(client.judge_prompts)
        for p in client.judge_prompts:                              # blind: nothing says which is the new version
            self.assertNotRegex(p.lower(), r"\bbefore\b|\bafter\b|optimi[sz]ed|original")
        self.assertTrue(got["summ"]["use_ai"])
        for it in got["items"]:
            self.assertTrue(it["before"]["ai"] and it["after"]["ai"])
            self.assertTrue(it["before"]["use_ai"])
            self.assertGreater(it["after"]["t"], it["before"]["t"] + 20)
        self.assertTrue(any("Deep quality check done" in m for m in logs))
        st = C.RunState(root)
        e = st.files["roof-repair.html"]
        self.assertTrue(e["sb"]["ai"] and e["sa"]["ai"])
        pipe.state.save()

    def test_free_only_when_deep_check_off(self):
        root = make_site(self.td.name, ["Roof Repair"])
        client = JudgeClient()
        got = {}
        opts = P.RunOptions(folder=str(root), site_url="https://site.com", brand="Acme", mode="per_page")
        P.Pipeline(opts, A.AI(client), C.collect_html_files(root), lambda m, t="": None, lambda r: None,
                   lambda d, t: None, threading.Event(), on_scores=lambda i, s: got.update(s=s)).run()
        self.assertEqual(client.judge_prompts, [])
        self.assertFalse(got["s"]["use_ai"])

    def test_judge_failure_keeps_free_scores(self):
        client = JudgeClient("boom")
        root, pipe, got, logs = self.run_deep(client)
        self.assertFalse(got["summ"]["use_ai"])
        self.assertEqual(got["summ"]["pages"], 2)
        self.assertTrue(all(not it["before"]["ai"] for it in got["items"]))
        self.assertTrue(any("deep check batch failed" in m for m in logs))

    def test_fatal_error_in_judge_keeps_report_usable(self):
        client = JudgeClient("fatal")
        root, pipe, got, logs = self.run_deep(client)
        self.assertEqual(got["summ"]["pages"], 2)
        self.assertFalse(got["summ"]["use_ai"])
        self.assertTrue(any("Deep check stopped" in m for m in logs))

    def test_resume_does_not_rebill_judged_pages(self):
        client = JudgeClient()
        root, pipe, got, logs = self.run_deep(client)
        first_calls = len(client.judge_prompts)
        client2 = JudgeClient()
        opts = P.RunOptions(folder=str(root), site_url="https://site.com", brand="Acme", mode="per_page",
                            deep_check=True, run_kind="resume")
        got2 = {}
        P.Pipeline(opts, A.AI(client2), C.collect_html_files(root), lambda m, t="": None, lambda r: None,
                   lambda d, t: None, threading.Event(), on_scores=lambda i, s: got2.update(s=s, i=i)).run()
        self.assertGreater(first_calls, 0)
        self.assertEqual(client2.judge_prompts, [])                  # nothing new to judge
        self.assertEqual(got2["s"]["pages"], 2)
        self.assertTrue(got2["s"]["use_ai"])                          # AI marks restored from the saved state

    def test_judge_parser_handles_bad_output(self):
        class Bad:
            messages = None

            def stream(self, *a, **k):
                class Ctx:
                    def __enter__(s): return s
                    def __exit__(s, *a): return False
                    def get_final_message(s):
                        return SimpleNamespace(content=[SimpleNamespace(text='[{"n":1,"title_intent":"x"},{"n":2,"title_intent":12,"desc_accurate":5}]')],
                                               usage=SimpleNamespace(input_tokens=1, output_tokens=1))
                return Ctx()
        c = Bad()
        c.messages = c
        views = [{"page": "a", "title": "t", "desc": "d", "h1": "h", "schema": "s", "excerpt": "e"}] * 3
        res = A.judge_pages(A.AI(c), A.GenConfig(model="m"), views)
        self.assertIsNone(res[0])                                    # unusable values -> not judged
        self.assertEqual(res[1]["marks"]["title.intent"], 1.0)         # clamped to 10/10
        self.assertEqual(res[1]["marks"]["desc.accurate"], 0.5)
        self.assertIsNone(res[2])                                    # missing item


if __name__ == "__main__":
    unittest.main(verbosity=2)
