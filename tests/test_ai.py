import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import seo_ai as A  # noqa: E402
import seo_core as C  # noqa: E402


class FakeClient:
    """Returns queued replies; records prompts."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []
        self.messages = self

    def stream(self, model, max_tokens, messages):
        self.prompts.append(messages[0]["content"])
        reply = self.replies.pop(0)
        text = reply if isinstance(reply, str) else json.dumps(reply)

        class Ctx:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def get_final_message(s):
                return SimpleNamespace(content=[SimpleNamespace(text=text)],
                                       usage=SimpleNamespace(input_tokens=1000, output_tokens=200))
        return Ctx()


def page(name="Roof Repair"):
    return {"page_name": name, "url": "https://x.com/roof/", "folder": "Services", "h1": "Roofing",
            "old_title": "old", "content": "We repair roofs. " * 20, "headings": [], "body_chars": 600}


GOOD_DESC = "Fast, reliable roof repair for Miami homeowners with free inspections and clear pricing. Call today to book."
assert 70 <= len(GOOD_DESC) <= 160


class TestAI(unittest.TestCase):
    def cfg(self):
        return A.GenConfig(model="claude-haiku-4-5", brand="Acme", sep="|", do_h1=True)

    def test_budget_in_prompt_and_title_build(self):
        cfg = self.cfg()
        self.assertEqual(cfg.title_max_core, 60 - 3 - 4)
        prompt = A.build_generation_prompt([page()], cfg, [])
        self.assertIn("DO NOT include the brand", prompt)
        self.assertIn("57", prompt) if False else None
        self.assertIn(f"{cfg.title_max_core}", prompt)

    def test_validate_flags_each_problem(self):
        cfg, reg = self.cfg(), C.Registry()
        cand = {"core": "A" * 70, "desc": "Too short", "h1": "H" * 80}
        probs = A.validate(cand, cfg, reg)
        joined = " | ".join(probs)
        self.assertIn("at most 53", joined)
        self.assertIn("complete sentence", joined)
        self.assertIn("h1 is 80", joined)
        ok = {"core": "Roof Repair in Miami FL with Free Quotes", "desc": GOOD_DESC, "h1": "Miami Roof Repair"}
        self.assertEqual(A.validate(ok, cfg, reg), [])
        dangling = dict(ok, core="Roof Repair Experts in Miami and")
        self.assertTrue(any("dangling" in p for p in A.validate(dangling, cfg, reg)))

    def test_refine_rewrites_instead_of_trimming(self):
        too_long = "Professional Roof Repair and Replacement Services in Miami"
        fixed = "Roof Repair & Replacement in Miami, Florida"
        self.assertGreater(len(too_long), 53)
        client = FakeClient([{"meta_title": fixed, "meta_description": GOOD_DESC, "h1": "Miami Roof Repair"}])
        ai, cfg, reg = A.AI(client), self.cfg(), C.Registry()
        cand = {"core": too_long, "desc": GOOD_DESC, "h1": "Miami Roof Repair"}
        pg = page()
        cand, probs = A.refine(ai, cfg, reg, pg, cand, [C.context_tokens("roof")])
        self.assertEqual(probs, [])
        self.assertEqual(cand["core"], fixed)                      # rewritten, not cut
        self.assertEqual(len(client.prompts), 1)
        self.assertIn("at most 53", client.prompts[0])
        self.assertGreater(ai.cost, 0)

    def test_refine_gives_up_after_3_rounds(self):
        bad = {"meta_title": "X" * 80, "meta_description": GOOD_DESC, "h1": "H"}
        client = FakeClient([bad, bad, bad])
        ai, cfg, reg = A.AI(client), self.cfg(), C.Registry()
        cand = {"core": "Y" * 80, "desc": GOOD_DESC, "h1": "H"}
        cand, probs = A.refine(ai, cfg, reg, page(), cand, [])
        self.assertTrue(probs)
        self.assertEqual(len(client.prompts), 3)

    def test_duplicate_detected_and_neighbours_sent(self):
        cfg, reg = self.cfg(), C.Registry()
        reg.add("Roof Repair Miami FL | Acme", "Roof Repair Miami FL", GOOD_DESC, "Miami Roof Repair", "roof repair miami")
        cand = {"core": "Roof Repair Miami FL", "desc": GOOD_DESC, "h1": "Miami Roof Repair"}
        probs = A.validate(cand, cfg, reg)
        self.assertEqual(len([p for p in probs if "similar" in p]), 3)
        client = FakeClient([{"meta_title": "Tile Roof Fixes for Coral Gables Homes",
                              "meta_description": "Coral Gables tile roofs repaired by licensed local crews, with photo reports and honest quotes. Request a visit.",
                              "h1": "Coral Gables Tile Roof Repair"}])
        ai = A.AI(client)
        cand, probs = A.refine(ai, cfg, reg, page(), cand, [C.context_tokens("roof repair miami")])
        self.assertEqual(probs, [])
        self.assertIn("Roof Repair Miami FL | Acme", client.prompts[0])   # neighbours were shown

    def test_generate_maps_pages_and_handles_missing(self):
        cfg, reg = self.cfg(), C.Registry()
        reply = [{"page": "PAGE_1", "meta_title": "One title here for page", "meta_description": GOOD_DESC, "h1": "One"},
                 {"page": "PAGE_3", "meta_title": "Three title here", "meta_description": GOOD_DESC, "h1": "Three"}]
        ai = A.AI(FakeClient([reply]))
        items = A.generate(ai, cfg, reg, [page("a"), page("b"), page("c")], [frozenset()] * 3)
        self.assertEqual(items[0]["h1"], "One")
        self.assertIsNone(items[1])
        self.assertEqual(items[2]["h1"], "Three")

    def test_candidate_from_item_strips_brand(self):
        cfg = self.cfg()
        c = A.candidate_from_item({"meta_title": "Roof Repair Miami | Acme", "meta_description": " Desc.  ",
                                   "h1": "H", "schema_type": "Service", "area_served": "Miami"}, cfg, "old")
        self.assertEqual(c["core"], "Roof Repair Miami")
        self.assertEqual(c["schema_ai"]["schema_type"], "Service")

    def test_alts(self):
        ai = A.AI(FakeClient([{"1": "Roofing crew on a tile roof.", "2": ""}]))
        res = A.generate_alts(ai, self.cfg(), "Roofing", "Roof", [
            {"i": 4, "file": "crew.jpg", "context": "Our team"}, {"i": 7, "file": "divider.png", "context": ""}])
        self.assertEqual(res, {4: "Roofing crew on a tile roof", 7: ""})

    def test_verify_details_removes_hallucinations(self):
        src = "Call us 305-555-0142 at 12 Ocean Drive Miami FL 33101 info@acme.com https://facebook.com/acme"
        d = {"phone": "(305) 555-0142", "email": "info@acme.com", "street": "12 Ocean Drive", "city": "Miami",
             "postal": "33101", "same_as": ["https://facebook.com/acme", "https://twitter.com/fake"]}
        self.assertEqual(A.verify_details(d, src), [])
        self.assertEqual(d["same_as"], ["https://facebook.com/acme"])
        bad = {"phone": "999-123-4567", "email": "x@y.com", "street": "99 Fake Street", "city": "Atlantis",
               "postal": "00000", "same_as": []}
        warns = A.verify_details(bad, src)
        self.assertEqual(len(warns), 5)
        self.assertEqual(bad["phone"], "")

    def test_safe_json(self):
        self.assertEqual(A.safe_json_parse('```json\n{"a": 1,}\n```'), {"a": 1})
        self.assertEqual(A.safe_json_parse('Here you go: [{"a": "x\ny"}] thanks'), [{"a": "x y"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
