import functools
import http.server
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import seo_ai as A  # noqa: E402
import seo_core as C  # noqa: E402

HOME = """<!doctype html><html><head><title>Acme Plumbing | Miami</title>
<meta property="og:site_name" content="Acme Plumbing">
<meta name="description" content="Acme Plumbing fixes pipes fast across Miami. Call anytime.">
<script type="application/ld+json">{"@type":"Plumber","name":"Acme Plumbing","telephone":"+1 305-555-0142"}</script></head>
<body><header><a href="tel:+13055550142">Call 305-555-0142</a><nav><a href="/contact.html">Contact</a><a href="/about.html">About</a><a href="/services.html">Our Services</a><a href="/service-areas.html">Service Areas</a><a href="/water-heater-repair.html">Water Heater Repair</a><a href="/drain-cleaning.html">Drain Cleaning</a></nav></header>
<main><h1>Plumbing</h1></main>
<footer><address>12 Ocean Drive, Miami, FL 33101</address>
<a href="https://www.facebook.com/acmeplumbing">FB</a><a href="https://twitter.com/intent/tweet?x">share</a>
<a href="mailto:info@acmeplumbing.com">Email</a></footer></body></html>"""
SERVICES = "<html><head><title>Services</title></head><body><main><h1>Plumbing services</h1><h2>Water heaters</h2><p>" + "We install and repair tankless water heaters, fix leaks and clear drains. " * 6 + "</p></main></body></html>"
AREAS = "<html><head><title>Areas</title></head><body><main><h1>Where we work</h1><p>" + "We serve Miami, Coral Gables, Hialeah and Doral. " * 8 + "</p></main></body></html>"
SITEMAP = "<urlset><url><loc>http://x/water-heater-repair.html</loc></url><url><loc>http://x/drain-cleaning.html</loc></url><url><loc>http://x/areas/coral-gables.html</loc></url></urlset>"
def _svc(h1):
    return f"<html><head><title>{h1} | Acme</title></head><body><main><h1>{h1}</h1><p>Details about {h1.lower()}.</p></main></body></html>"


CONTACT = "<html><head><title>Contact</title></head><body><p>Open Monday to Friday 8am-5pm</p></body></html>"


class Scripted:
    def __init__(self, payload):
        self.payload = payload
        self.prompts = []
        self.messages = self

    def stream(self, model, max_tokens, messages):
        self.prompts.append(messages[0]["content"])
        text = json.dumps(self.payload)

        class Ctx:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def get_final_message(s):
                return SimpleNamespace(content=[SimpleNamespace(text=text)],
                                       usage=SimpleNamespace(input_tokens=2000, output_tokens=200))
        return Ctx()


class TestFetch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        Path(cls.td.name, "index.html").write_text(HOME, encoding="utf-8")
        Path(cls.td.name, "contact.html").write_text(CONTACT, encoding="utf-8")
        Path(cls.td.name, "services.html").write_text(SERVICES, encoding="utf-8")
        Path(cls.td.name, "service-areas.html").write_text(AREAS, encoding="utf-8")
        Path(cls.td.name, "water-heater-repair.html").write_text(_svc("Water Heater Repair"), encoding="utf-8")
        Path(cls.td.name, "drain-cleaning.html").write_text(_svc("Drain Cleaning"), encoding="utf-8")
        Path(cls.td.name, "areas").mkdir()
        Path(cls.td.name, "areas", "coral-gables.html").write_text(_svc("Plumber in Coral Gables"), encoding="utf-8")
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=cls.td.name)
        handler.log_message = lambda *a, **k: None
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.srv.server_address[1]
        Path(cls.td.name, "sitemap.xml").write_text(SITEMAP.replace("http://x", f"http://127.0.0.1:{cls.port}"), encoding="utf-8")
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.td.cleanup()

    def test_hints_from_html(self):
        h = C.extract_business_hints(HOME, "http://x")
        self.assertEqual(h["phones"], ["+13055550142"])
        self.assertEqual(h["emails"], ["info@acmeplumbing.com"])
        self.assertEqual(h["social"], ["https://www.facebook.com/acmeplumbing"])   # share/intent link ignored
        self.assertEqual(h["jsonld"][0]["name"], "Acme Plumbing")
        self.assertIn("12 Ocean Drive", h["text"])
        links = C.find_info_links(HOME, "http://127.0.0.1/")
        self.assertEqual([l.rsplit("/", 1)[1] for l in links[:3]], ["services.html", "water-heater-repair.html", "service-areas.html"][:1] + [l.rsplit("/", 1)[1] for l in links[1:3]])
        self.assertIn("http://127.0.0.1/service-areas.html", links)
        self.assertIn("http://127.0.0.1/contact.html", links)
        menu = C.extract_menu_links(HOME, "http://127.0.0.1/")
        self.assertIn("Water Heater Repair", menu)
        self.assertIn("Drain Cleaning", menu)

    def test_extract_details_end_to_end_and_hallucination_guard(self):
        payload = {"name": "Acme Plumbing", "context": "Family plumbing company serving Miami.", "business_type": "Plumber", "phone": "(305) 555-0142",
                   "email": "info@acmeplumbing.com", "street": "12 Ocean Drive", "city": "Miami",
                   "region": "FL", "postal": "33101", "country": "US", "hours": ["Mo-Fr 08:00-17:00"],
                   "services": ["Water heater repair", "Drain cleaning", "Fake underwater welding"], "areas": ["Miami", "Coral Gables", "Atlantis"], "price_range": "$$$", "same_as": ["https://www.facebook.com/acmeplumbing",
                                                    "https://instagram.com/madeup"], "logo": ""}
        client = Scripted(payload)
        ai = A.AI(client)
        cfg = A.GenConfig(model="claude-haiku-4-5", brand="Acme")
        res = A.extract_business_details(ai, cfg, f"http://127.0.0.1:{self.port}/")
        d = res["details"]
        self.assertEqual(d["phone"], "(305) 555-0142")
        self.assertEqual(d["street"], "12 Ocean Drive")
        self.assertEqual(d["same_as"], ["https://www.facebook.com/acmeplumbing"])   # invented profile dropped
        self.assertGreaterEqual(len(res["sources"]), 4)                              # home + services + areas + contact...
        self.assertIn("Water Heater Repair | Drain Cleaning", client.prompts[0].replace("Contact | About | Our Services | Service Areas | ", ""))
        self.assertIn("tankless water heaters", client.prompts[0])                     # services page text
        self.assertIn("Coral Gables, Hialeah and Doral", client.prompts[0])           # areas page text
        self.assertIn("PAGE CATALOGUE", client.prompts[0])
        self.assertIn("/water-heater-repair.html | Water Heater Repair", client.prompts[0])   # catalogue read each page
        self.assertIn("/areas/coral-gables.html | Plumber in Coral Gables", client.prompts[0])
        self.assertGreaterEqual(res["pages_read"], 3)
        self.assertEqual(d["services"], ["Water heater repair", "Drain cleaning"])         # invented service dropped
        self.assertEqual(d["areas"], ["Miami", "Coral Gables"])                              # invented area dropped
        self.assertTrue(any("services not found" in w for w in res["warnings"]))
        self.assertTrue(any("areas not found" in w for w in res["warnings"]))
        self.assertIn("Open Monday to Friday", client.prompts[0])
        self.assertIn("ONLY information that literally appears", client.prompts[0])
        # price_range / hours are accepted as typed by the model but shown to the user for verification
        self.assertEqual(d["business_type"], "Plumber")
        self.assertEqual(d["context"], "Family plumbing company serving Miami.")
        self.assertIn("HOMEPAGE TITLE", client.prompts[0])

    def test_invented_address_removed(self):
        payload = {"name": "Acme", "phone": "999-000-1111", "street": "1 Nowhere Lane", "city": "Atlantis",
                   "postal": "00000", "email": "", "hours": [], "same_as": [], "business_type": "", "logo": "",
                   "region": "", "country": "", "price_range": ""}
        res = A.extract_business_details(A.AI(Scripted(payload)), A.GenConfig(model="m"),
                                         f"http://127.0.0.1:{self.port}/")
        d = res["details"]
        self.assertEqual((d["phone"], d["street"], d["city"], d["postal"]), ("305-555-0142"[:0] or d["phone"], "", "", ""))
        self.assertTrue(res["warnings"])
        # falls back to the real tel: link found on the site
        self.assertEqual(d["phone"], "+13055550142")
        self.assertEqual(d["business_type"], "LocalBusiness")
        self.assertEqual(d["name"], "Acme")                       # kept from the AI answer

    def test_name_and_context_fall_back_to_the_site_itself(self):
        payload = {"name": "", "context": "", "business_type": "", "phone": "", "email": "", "street": "",
                   "city": "", "region": "", "postal": "", "country": "", "hours": [], "price_range": "",
                   "same_as": [], "logo": ""}
        res = A.extract_business_details(A.AI(Scripted(payload)), A.GenConfig(model="m"),
                                         f"http://127.0.0.1:{self.port}/")
        d = res["details"]
        self.assertEqual(d["name"], "Acme Plumbing")                                  # from JSON-LD
        self.assertEqual(d["context"], "Acme Plumbing fixes pipes fast across Miami. Call anytime.")   # site's own description

    def test_context_is_cut_at_sentence_boundary(self):
        long = " ".join(f"Sentence number {i} about plumbing services." for i in range(40))
        out = A._fit_sentences(long, 200)
        self.assertLessEqual(len(out), 200)
        self.assertTrue(out.endswith("."))
        self.assertNotIn("Sentence number 39", out)
        self.assertEqual(A._fit_sentences("Short one.", 200), "Short one.")

    def test_compose_context_keeps_every_item(self):
        full = A.compose_context("Family plumber.", ["Drain cleaning", "Water heaters"], ["Miami", "Doral"])
        self.assertEqual(full, "Family plumber.\nServices: Drain cleaning; Water heaters.\nService areas: Miami; Doral.")
        many = [f"Service number {i}" for i in range(400)]
        out = A.compose_context("S.", many, [], limit=300)
        self.assertLessEqual(len(out), 301)
        self.assertTrue(out.endswith("."))
        self.assertTrue(out.rstrip(".").split("; ")[-1].startswith("Service number "))   # last item is whole

    def test_catalogue_picker_spreads_across_folders(self):
        urls = [f"http://s/cities/city-{i}" for i in range(50)] + ["http://s/services/roofing", "http://s/services/gutters",
                "http://s/blog/post-1", "http://s/privacy-policy"]
        picked = C.pick_catalog_urls(urls, 6)
        self.assertIn("http://s/services/roofing", picked)
        self.assertIn("http://s/services/gutters", picked)
        self.assertNotIn("http://s/blog/post-1", picked)
        self.assertNotIn("http://s/privacy-policy", picked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
