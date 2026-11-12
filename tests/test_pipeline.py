import json
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import seo_ai as A  # noqa: E402
import seo_core as C  # noqa: E402
import seo_score as S  # noqa: E402
import seo_pipeline as P  # noqa: E402

DESCS = {
    "roof repair": "Leaking roof? Our licensed crew patches storm damage within a day and backs every repair with a written warranty. Book your inspection now.",
    "gutter cleaning": "Clear gutters protect your foundation, so we flush debris, test downspouts and photograph the results for you. Schedule a spring visit today.",
    "window washing": "Streak-free glass inside and out, from single-storey homes to office towers, using safe pure-water equipment. Request a fast, no-obligation quote.",
}


class _D:
    def format(self, x):
        return DESCS[x]


DESC = _D()
BODY = "<p>" + ("We provide {x} services for homes and businesses across the area with careful, tidy work. " * 6) + "</p>"

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Old {x}</title>
  <link rel="canonical" href="https://site.com/{slug}/">
  <script type="application/ld+json">{{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[]}}</script>
</head>
<body>
<header><nav><a href="/">Home</a></nav><img src="images/logo.png"></header>
<main><h1>{x}</h1>{body}
<img src="images/{slug}-crew.jpg"><img src="images/IMG_0042.jpg"></main>
<footer>Copyright</footer>
</body></html>"""


class ScriptedClient:
    """Fake Anthropic client that answers generation / rewrite / alt prompts."""

    def __init__(self, long_first=False):
        self.calls = []
        self.messages = self
        self.long_first = long_first
        self.seen_long = set()

    def stream(self, model, max_tokens, messages):
        prompt = messages[0]["content"]
        self.calls.append(prompt)
        if "PAGE_1:" in prompt:
            names = re.findall(r"page_name: (.+)", prompt)
            out = []
            for i, n in enumerate(names):
                title = f"{n} Experts in Miami Florida"
                if self.long_first and n not in self.seen_long:
                    title = f"Professional and Affordable {n} Experts Serving All of Miami Florida"
                    self.seen_long.add(n)
                out.append({"page": f"PAGE_{i + 1}", "meta_title": title,
                            "meta_description": DESC.format(x=n.lower()), "h1": f"{n} in Miami",
                            "schema_type": "Service", "service_type": n, "area_served": "Miami"})
            text = json.dumps(out)
        elif "Rewrite the fields below" in prompt:
            n = re.search(r"Page: (.+)", prompt).group(1)
            text = json.dumps({"meta_title": f"{n} Pros in Miami", "meta_description": DESC.format(x=n.lower()),
                               "h1": f"{n} in Miami"})
        elif "image alt text" in prompt:
            text = json.dumps({"1": "Crew at work"})
        else:
            text = "{}"

        class Ctx:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def get_final_message(s):
                return SimpleNamespace(content=[SimpleNamespace(text=text)],
                                       usage=SimpleNamespace(input_tokens=500, output_tokens=100))
        return Ctx()


def make_site(td, names):
    root = Path(td) / "site"
    (root / "images").mkdir(parents=True)
    (root / "images" / "logo.png").write_bytes(b"x")
    for n in names:
        slug = n.lower().replace(" ", "-")
        (root / f"{slug}.html").write_text(TEMPLATE.format(x=n, slug=slug, body=BODY.format(x=n.lower())), encoding="utf-8")
    return root


def run(root, client, **kw):
    results = []
    opts = P.RunOptions(folder=str(root), site_url="https://site.com", brand="Acme", sep="|",
                        facts={"name": "Acme", "business_type": "LocalBusiness", "phone": "305-555-0100",
                               "street": "1 Main St", "city": "Miami", "region": "FL", "country": "US"},
                        **kw)
    files = C.collect_html_files(root)
    pipe = P.Pipeline(opts, A.AI(client), files, lambda m, t="": None, results.append,
                      lambda d, t: None, threading.Event())
    pipe.run()
    return results, pipe


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        C.STATE_DIR = Path(self.td.name) / "state"

    def tearDown(self):
        self.td.cleanup()

    def test_batch_run_writes_unique_safe_output(self):
        root = make_site(self.td.name, ["Roof Repair", "Gutter Cleaning", "Window Washing"])
        results, pipe = run(root, ScriptedClient(), mode="batch")
        self.assertEqual([r["status"] for r in results], ["updated"] * 3)
        html = (root / "gutter-cleaning.html").read_text(encoding="utf-8")
        self.assertIn("<title>Gutter Cleaning Experts in Miami Florida | Acme</title>", html)
        self.assertEqual(C.read_meta(html, "description"), DESC.format(x="gutter cleaning"))
        self.assertIn("<h1>Gutter Cleaning in Miami</h1>", html)
        blocks = C.parse_ld_blocks(html)
        types = sorted(C._types(n)[0] for b in blocks for n in C._iter_nodes(b.data))
        self.assertEqual(types, ["BreadcrumbList", "Service"])          # existing schema kept
        svc = [n for b in blocks for n in C._iter_nodes(b.data) if C._types(n) == ["Service"]][0]
        self.assertEqual(svc["provider"]["telephone"], "305-555-0100")
        self.assertEqual(svc["url"], "https://site.com/gutter-cleaning/")
        self.assertIn('property="og:url" content="https://site.com/gutter-cleaning/"', html)
        self.assertNotIn("og:image", html)                              # no usable image on disk -> skipped
        self.assertIn('<img src="images/logo.png" alt=', html)
        self.assertNotRegex(html, r'logo.png"[^>]*loading')             # header logo never lazy
        self.assertIn("Copyright", html)
        # backup holds the untouched original, outside the site folder
        bdir = Path(pipe.state.backup_dir)
        self.assertFalse(str(bdir).startswith(str(root) + "\\"))
        self.assertIn("Old Gutter Cleaning", (bdir / "gutter-cleaning.html").read_text(encoding="utf-8"))
        titles = [r["new_title"] for r in results]
        self.assertEqual(len(set(titles)), 3)
        self.assertTrue(all(len(t) <= 60 for t in titles))

    def test_too_long_titles_are_rewritten_not_trimmed(self):
        root = make_site(self.td.name, ["Roof Repair"])
        client = ScriptedClient(long_first=True)
        results, _ = run(root, client, mode="per_page")
        r = results[0]
        self.assertEqual(r["status"], "updated")
        self.assertEqual(r["new_title"], "Roof Repair Pros in Miami | Acme")   # came from the rewrite call
        self.assertTrue(any("Rewrite the fields below" in c for c in client.calls))

    def test_dry_run_writes_nothing(self):
        root = make_site(self.td.name, ["Roof Repair"])
        before = (root / "roof-repair.html").read_text(encoding="utf-8")
        results, pipe = run(root, ScriptedClient(), mode="dry_run")
        self.assertEqual(results[0]["status"], "preview")
        self.assertEqual((root / "roof-repair.html").read_text(encoding="utf-8"), before)
        self.assertIsNone(pipe.state)

    def test_resume_skips_done_and_keeps_uniqueness(self):
        root = make_site(self.td.name, ["Roof Repair", "Gutter Cleaning"])
        files = C.collect_html_files(root)
        stop = threading.Event()
        results = []

        def on_result(r):
            results.append(r)
            stop.set()                                       # stop after the first page

        opts = P.RunOptions(folder=str(root), site_url="https://site.com", brand="Acme", mode="per_page")
        pipe = P.Pipeline(opts, A.AI(ScriptedClient()), files, lambda m, t="": None, on_result,
                          lambda d, t: None, stop)
        pipe.run()
        self.assertEqual(len(results), 1)
        # resume: only the second page is processed
        results2 = []
        opts2 = P.RunOptions(folder=str(root), site_url="https://site.com", brand="Acme", mode="per_page",
                             run_kind="resume")
        client2 = ScriptedClient()
        pipe2 = P.Pipeline(opts2, A.AI(client2), files, lambda m, t="": None, results2.append,
                           lambda d, t: None, threading.Event())
        pipe2.run()
        self.assertEqual([r["rel"] for r in results2], ["roof-repair.html"])
        self.assertEqual(len(pipe2.reg), 2)                  # first page reloaded from state
        counts = C.RunState(root).counts()
        self.assertEqual(counts, {"done": 2})

    def test_skips_and_exclusions(self):
        root = make_site(self.td.name, ["Roof Repair", "Thank You"])
        (root / "partial.html").write_text("<div>just a fragment</div>")
        (root / "hidden.html").write_text('<html><head><meta name="robots" content="noindex"></head><body></body></html>')
        (root / "404.html").write_text("<html><head><title>x</title></head><body>nf</body></html>")
        results, _ = run(root, ScriptedClient(), mode="per_page", exclude_patterns=["thank-you.html"])
        by = {r["rel"]: r for r in results}
        self.assertEqual(by["roof-repair.html"]["status"], "updated")
        self.assertEqual(by["thank-you.html"]["reason"], "excluded by you")
        self.assertIn("partial", by["partial.html"]["reason"])
        self.assertIn("noindex", by["hidden.html"]["reason"])
        self.assertIn("404", by["404.html"]["reason"])
        self.assertEqual((root / "404.html").read_text(), "<html><head><title>x</title></head><body>nf</body></html>")

    def test_review_when_never_valid(self):
        root = make_site(self.td.name, ["Roof Repair"])

        class Bad(ScriptedClient):
            def stream(self, model, max_tokens, messages):
                self.calls.append(messages[0]["content"])
                text = json.dumps([{"page": "PAGE_1", "meta_title": "X" * 90, "meta_description": "short",
                                    "h1": "H", "schema_type": "WebPage"}]) if "PAGE_1:" in messages[0]["content"] \
                    else json.dumps({"meta_title": "X" * 90, "meta_description": "short", "h1": "H"})

                class Ctx:
                    def __enter__(s): return s
                    def __exit__(s, *a): return False
                    def get_final_message(s):
                        return SimpleNamespace(content=[SimpleNamespace(text=text)],
                                               usage=SimpleNamespace(input_tokens=1, output_tokens=1))
                return Ctx()
        before = (root / "roof-repair.html").read_text(encoding="utf-8")
        results, pipe = run(root, Bad(), mode="per_page")
        self.assertEqual(results[0]["status"], "review")
        self.assertEqual((root / "roof-repair.html").read_text(encoding="utf-8"), before)
        self.assertEqual(C.RunState(root).files["roof-repair.html"]["status"], "review")

    def test_fatal_api_error_stops_run_and_saves(self):
        root = make_site(self.td.name, ["Roof Repair", "Gutter Cleaning"])

        class AuthErr(Exception):
            status_code = 401

        class Boom(ScriptedClient):
            def stream(self, model, max_tokens, messages):
                raise AuthErr("invalid x-api-key")
        logs = []
        results = []
        opts = P.RunOptions(folder=str(root), site_url="https://site.com", brand="Acme", mode="per_page")
        stop = threading.Event()
        pipe = P.Pipeline(opts, A.AI(Boom()), C.collect_html_files(root), lambda m, t="": logs.append(m),
                          results.append, lambda d, t: None, stop)
        pipe.run()
        self.assertTrue(stop.is_set())
        self.assertEqual(results, [])                                    # no per-page failure spam
        self.assertTrue(any("Run stopped" in m for m in logs))


class TestPipelineScoring(TestPipeline):
    """Scoring wired into the run (inherits the setUp that isolates the state folder)."""

    def test_scores_before_after_summary_and_state(self):
        root = make_site(self.td.name, ["Roof Repair", "Gutter Cleaning", "Window Washing"])
        got = {}
        results = []
        opts = P.RunOptions(folder=str(root), site_url="https://site.com", brand="Acme", mode="batch")
        pipe = P.Pipeline(opts, A.AI(ScriptedClient()), C.collect_html_files(root), lambda m, t="": None,
                          results.append, lambda d, t: None, threading.Event(),
                          on_scores=lambda items, summ: got.update(items=items, summ=summ))
        pipe.run()
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertTrue(r["score_before"] and r["score_after"])
            self.assertGreater(r["score_after"]["t"], r["score_before"]["t"])      # the tool measurably improves the page
        summ = got["summ"]
        self.assertEqual(summ["pages"], 3)
        self.assertGreater(summ["delta"], 5)
        self.assertEqual(summ["decreased"], [])
        # compact scores were persisted for resume
        st = C.RunState(root)
        e = st.files["roof-repair.html"]
        self.assertIn("sb", e)
        self.assertIn("sa", e)
        self.assertLess(len(json.dumps(e["sb"])) + len(json.dumps(e["sa"])), 2500)
        # site-wide uniqueness check ran on the final titles
        self.assertTrue(all(it["after"]["f"][S.CHECK_INDEX["title.unique"]] == 100 for it in got["items"]))

    def test_resume_summary_covers_pages_from_earlier_session(self):
        root = make_site(self.td.name, ["Roof Repair", "Gutter Cleaning"])
        files = C.collect_html_files(root)
        stop = threading.Event()
        first = []

        def on_result(r):
            first.append(r)
            stop.set()
        p1 = P.Pipeline(P.RunOptions(folder=str(root), site_url="https://site.com", brand="Acme", mode="per_page"),
                        A.AI(ScriptedClient()), files, lambda m, t="": None, on_result, lambda d, t: None, stop)
        p1.run()
        self.assertEqual(len(first), 1)
        got = {}
        p2 = P.Pipeline(P.RunOptions(folder=str(root), site_url="https://site.com", brand="Acme",
                                     mode="per_page", run_kind="resume"),
                        A.AI(ScriptedClient()), files, lambda m, t="": None, lambda r: None, lambda d, t: None,
                        threading.Event(), on_scores=lambda items, summ: got.update(summ=summ, items=items))
        p2.run()
        self.assertEqual(got["summ"]["pages"], 2)                       # 1 from the earlier session + 1 now
        self.assertEqual(sorted(it["rel"] for it in got["items"]), ["gutter-cleaning.html", "roof-repair.html"])

    def test_stale_state_scores_are_ignored_when_file_changed(self):
        root = make_site(self.td.name, ["Roof Repair", "Gutter Cleaning"])
        results, pipe = run(root, ScriptedClient(), mode="per_page")
        page = root / "roof-repair.html"
        page.write_text(page.read_text(encoding="utf-8") + "\n<!-- edited -->", encoding="utf-8")
        p = P.Pipeline(P.RunOptions(folder=str(root), mode="per_page", run_kind="retry"), A.AI(ScriptedClient()), [],
                       lambda m, t="": None, lambda r: None, lambda d, t: None, threading.Event())
        items = p._collect_score_items()
        self.assertEqual([it["rel"] for it in items], ["gutter-cleaning.html"])

    def test_dry_run_scores_in_memory_and_scoring_can_be_switched_off(self):
        root = make_site(self.td.name, ["Roof Repair"])
        results, pipe = run(root, ScriptedClient(), mode="dry_run")
        self.assertTrue(results[0]["score_after"]["t"] > results[0]["score_before"]["t"])
        self.assertIsNone(pipe.state)
        results2, _ = run(root, ScriptedClient(), mode="dry_run", do_score=False)
        self.assertIsNone(results2[0]["score_before"])
        self.assertIsNone(results2[0]["score_after"])



if __name__ == "__main__":
    unittest.main(verbosity=2)
