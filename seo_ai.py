"""
seo_ai.py — everything that talks to Claude for the SEO Meta Tag Updater:
prompts, JSON parsing, the "never trim — rewrite until it fits" refinement loop,
image alt text, and business-detail extraction (Fetch Details).
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field

import seo_core as C

MODELS = {
    "claude-haiku-4-5   ·  Fast & Budget  ($0.80 / $4.00 per M tokens)":  "claude-haiku-4-5",
    "claude-sonnet-4-5  ·  Balanced       ($3.00 / $15.00 per M tokens)": "claude-sonnet-4-5",
    "claude-opus-4-5    ·  Most Capable   ($15.00 / $75.00 per M tokens)": "claude-opus-4-5",
}
PRICING = {
    "claude-haiku-4-5":  {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-5": {"input": 3.00,  "output": 15.00},
    "claude-opus-4-5":   {"input": 15.00, "output": 75.00},
}
MODEL_MAX_TOKENS = {"claude-haiku-4-5": 64000, "claude-sonnet-4-5": 64000, "claude-opus-4-5": 64000}

BATCH_SIZE = 10
MAX_REFINE_ROUNDS = 3
NEIGHBORS_SINGLE = 20
NEIGHBORS_PER_PAGE = 8
NEIGHBORS_BATCH_MAX = 60
SINGLE_BODY_CHARS = 1200
BATCH_BODY_CHARS = 600

OPENING_STYLES = [
    "open with the customer's problem, then the solution",
    "open with the concrete benefit or outcome the customer gets",
    "open with the exact service and location, then a differentiator",
    "open with a question the customer would type into Google",
    "open with a trust signal that is actually stated on the page (experience, licensing, guarantee)",
    "open with what makes this specific page's offer different from similar pages",
]


@dataclass
class GenConfig:
    model: str
    rewrite_model: str = ""
    brand: str = ""
    sep: str = "|"
    site_context: str = ""
    site_url: str = ""
    do_h1: bool = True
    log: object = None                      # callable(msg, tag)

    @property
    def rw_model(self) -> str:
        return self.rewrite_model or self.model

    @property
    def title_max_core(self) -> int:
        return C.title_budget(self.sep, self.brand)

    def say(self, msg, tag=""):
        if self.log:
            self.log(msg, tag)


# ─────────────────────────────────────────────────────────────────────────────
# Low-level API access
# ─────────────────────────────────────────────────────────────────────────────
def cost_of(model: str, tin: int, tout: int) -> float:
    p = PRICING.get(model, {"input": 0.8, "output": 4.0})
    return tin / 1e6 * p["input"] + tout / 1e6 * p["output"]


def safe_json_parse(raw):
    """Parse JSON from model output, recovering from the usual failure modes."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    raw = re.sub(r"^```[a-z]*\s*", "", raw.strip(), flags=re.I)
    raw = re.sub(r"\s*```\s*$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    out, in_str, esc = [], False, False
    for ch in raw:                                   # newlines inside strings → spaces
        if esc:
            out.append(ch); esc = False; continue
        if ch == "\\" and in_str:
            out.append(ch); esc = True; continue
        if ch == '"':
            in_str = not in_str
        if in_str and ch in "\n\r\t":
            out.append(" "); continue
        out.append(ch)
    raw2 = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    try:
        return json.loads(raw2)
    except json.JSONDecodeError:
        pass
    m = re.search(r"[\[{].*[\]}]", raw2, re.S)       # JSON buried in prose
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


class FatalAPIError(Exception):
    """Authentication / billing problem — retrying other pages would only repeat it."""


class AI:
    """Thin wrapper over the Anthropic client: retries, cost tracking, JSON parsing."""

    def __init__(self, client):
        self.client = client
        self.cost = 0.0
        self.tokens_in = 0
        self.tokens_out = 0

    def ask(self, model: str, prompt: str, max_tokens: int = 4000, tries: int = 5):
        """Return (text, cost_usd). Retries on rate-limit / overload / network errors."""
        last = None
        for attempt in range(tries):
            try:
                with self.client.messages.stream(
                        model=model, max_tokens=min(max_tokens, MODEL_MAX_TOKENS.get(model, 8000)),
                        messages=[{"role": "user", "content": prompt}]) as s:
                    msg = s.get_final_message()
                cost = cost_of(model, msg.usage.input_tokens, msg.usage.output_tokens)
                self.cost += cost
                self.tokens_in += msg.usage.input_tokens
                self.tokens_out += msg.usage.output_tokens
                return msg.content[0].text.strip(), cost
            except Exception as e:                     # noqa: BLE001
                last = e
                status = getattr(e, "status_code", None)
                name = type(e).__name__
                retryable = (status in (408, 409, 429) or (status or 0) >= 500
                             or name in ("APIConnectionError", "APITimeoutError", "RateLimitError",
                                         "InternalServerError", "OverloadedError"))
                if status in (401, 403) or "credit balance" in str(e).lower():
                    raise FatalAPIError(f"{name}: {e}") from e
                if not retryable or attempt == tries - 1:
                    raise
                time.sleep(2 ** attempt * 3)
        raise last                                      # pragma: no cover

    def ask_json(self, model, prompt, max_tokens=4000):
        text, cost = self.ask(model, prompt, max_tokens)
        return safe_json_parse(text), cost


# ─────────────────────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────────────────────
WRITING_RULES = """WRITING RULES (apply to every field):
- Write natural, complete, grammatically correct English that reads like a human copywriter wrote it.
- Every sentence must be FINISHED. Never leave a dangling word ("and", "in", "for"…), never use "..." or "…".
- Stay inside the character limits by WRITING TO FIT. Count characters (spaces and punctuation count).
  Your text will NEVER be cut off or trimmed afterwards, so if it is too long it will be rejected and
  you will have to rewrite it. Plan the wording so it fits the first time.
- Use only facts that appear on the page or in the site info. Do not invent prices, years, awards or claims.
- Lead with the main keyword or benefit. No keyword stuffing, no ALL CAPS, no emoji."""

UNIQUENESS_RULE = """UNIQUENESS (CRITICAL): every page on the site needs its own meta_title, meta_description and h1.
Many pages are similar (the same service in different cities, or related services). Differentiate each page
with what is specific to THAT page: its exact service, city/area, audience, selling point, page name, URL and H1.
Never reuse the same opening words, sentence structure or call-to-action wording across pages."""


def _avoid_block(entries) -> str:
    if not entries:
        return ""
    lines = "\n".join(
        f"- T: {t} | D: {d}" + (f" | H1: {h}" if h else "") for t, d, h in entries)
    return ("\n=== ALREADY USED ON SIMILAR PAGES OF THIS SITE (T = title, D = description) ===\n"
            "Your text MUST be clearly different from all of these — do not repeat or closely paraphrase them.\n"
            + lines + "\n")


def _limits_text(cfg: GenConfig) -> str:
    cmax = cfg.title_max_core
    cmin = max(cmax - 12, 18)
    if cfg.brand:
        brand_note = (f'The app automatically appends "{C.norm_separator(cfg.sep)}{cfg.brand}" to your title, so DO NOT '
                      f"include the brand name or any separator in meta_title.")
    else:
        brand_note = "No brand is appended, so meta_title is the whole title."
    return (f"meta_title: {cmin}-{cmax} characters. {brand_note}\n"
            f"meta_description: {C.TARGET_DESC_MIN}-{C.TARGET_DESC_MAX} characters, hard maximum {C.MAX_DESC_LEN}. "
            "One or two complete sentences, ending with a period, with a clear call to action.\n"
            f"h1: at most {C.MAX_H1_LEN} characters, one complete heading.")


def build_generation_prompt(pages: list, cfg: GenConfig, avoid_entries: list) -> str:
    ctx = (f"Business summary (what it offers and where): {cfg.site_context}\n"
           "Use the summary to understand the business. Do NOT list every service or place in one title — "
           "each page should focus on what that page is about.\n" if cfg.site_context else "")
    brand = f"Brand: {cfg.brand}\n" if cfg.brand else ""
    h1_rule = ("h1 (string): a strong, specific heading with the main keyword. If the existing H1 is already "
               "good return it EXACTLY unchanged. Must be unique across the site.\n" if cfg.do_h1 else "")
    h1_json = ',"h1":"..."' if cfg.do_h1 else ""
    ptxt = ""
    for i, p in enumerate(pages):
        body = re.sub(r"\s+", " ", p["content"])[:p["body_chars"]]
        ptxt += (f"\nPAGE_{i + 1}:\n  page_name: {p['page_name']}\n  url: {p['url'] or '(unknown)'}\n"
                 f"  section: {p['folder'] or '(root)'}\n  existing_h1: {p['h1'] or '(none)'}\n"
                 f"  current_title: {p['old_title'] or '(none)'}\n")
        if p.get("headings"):
            ptxt += f"  subheadings: {' / '.join(p['headings'][:6])}\n"
        ptxt += f"  content: {body}\n"
        if p.get("style_hint"):
            ptxt += f"  note: this page is nearly identical to other pages — {p['style_hint']}.\n"
    n = len(pages)
    return f"""You are a senior SEO copywriter. Write metadata for the web pages below.

=== SITE ===
{ctx}{brand}
{WRITING_RULES}

{UNIQUENESS_RULE} This also applies WITHIN this request.
{_avoid_block(avoid_entries)}
=== LIMITS ===
{_limits_text(cfg)}

=== FIELDS PER PAGE ===
meta_title (string), meta_description (string), {h1_rule}schema_type (string): WebPage unless clearly one of:
  Service (page sells a specific service), LocalBusiness (home/about/contact of the business),
  Article (a dated blog post or news item), AboutPage, ContactPage, CollectionPage.
service_type (string, Service only), area_served (string, only a place named on the page, else "").
Do NOT output phone numbers, addresses, hours, prices, emails, URLs, social links or images — those are added separately.

=== OUTPUT ===
Return ONLY a raw JSON array with exactly {n} objects in the SAME ORDER, no markdown, no commentary:
[{{"page":"PAGE_1","meta_title":"...","meta_description":"..."{h1_json},"schema_type":"WebPage","service_type":"","area_served":""}}, ...]
All strings on one line; escape inner double quotes.

=== PAGES ===
{ptxt}"""


def build_rewrite_prompt(page: dict, cand: dict, problems: list, cfg: GenConfig, avoid_entries: list) -> str:
    want = ["meta_title", "meta_description"] + (["h1"] if cfg.do_h1 else [])
    content = re.sub(r"\s+", " ", page["content"])[:900]
    problems_txt = "\n".join(f"- {p}" for p in problems)
    h1_json = ',"h1":"..."' if cfg.do_h1 else ""
    cur_h1 = cand.get("h1", "")
    return f"""You are a senior SEO copywriter. Rewrite the fields below for ONE page because they failed validation.

Page: {page['page_name']}
URL: {page['url'] or '(unknown)'}
Existing H1: {page['h1'] or '(none)'}
Content: {content}

CURRENT VALUES
meta_title ({len(cand['core'])} chars): {cand['core']}
meta_description ({len(cand['desc'])} chars): {cand['desc']}
h1 ({len(cur_h1)} chars): {cur_h1}

PROBLEMS TO FIX
{problems_txt}

{WRITING_RULES}

LIMITS
{_limits_text(cfg)}
{_avoid_block(avoid_entries)}
Do NOT shorten by cutting words off — rewrite the whole sentence so it is complete and fits.
Return ONLY raw JSON with these keys: {', '.join(want)}  (return unchanged values for fields that had no problem).
{{"meta_title":"...","meta_description":"..."{h1_json}}}"""


# ─────────────────────────────────────────────────────────────────────────────
# Candidate validation + refinement
# ─────────────────────────────────────────────────────────────────────────────
def candidate_from_item(item: dict, cfg: GenConfig, old_h1: str) -> dict:
    core = C.strip_brand(item.get("meta_title", ""), cfg.brand)
    h1 = C.clean_text(item.get("h1", "")) if cfg.do_h1 else ""
    ai_schema = {
        "schema_type": C.clean_text(item.get("schema_type", "")) or "WebPage",
        "service_type": C.clean_text(item.get("service_type", "")),
        "area_served": item.get("area_served") if isinstance(item.get("area_served"), list)
        else C.clean_text(item.get("area_served", "")),
    }
    return {"core": core, "desc": C.clean_text(item.get("meta_description", "")),
            "h1": h1 or (old_h1 if cfg.do_h1 else ""), "schema_ai": ai_schema}


def _val_title(core: str, cfg: GenConfig, reg: C.Registry) -> list:
    probs = []
    cmax = cfg.title_max_core
    full = C.build_title(core, cfg.sep, cfg.brand)
    if not core:
        probs.append("meta_title is empty")
        return probs
    if len(core) > cmax:
        probs.append(f"meta_title is {len(core)} characters but must be at most {cmax} (brand is added separately)")
    if len(full) < C.MIN_TITLE_LEN:
        probs.append(f"final title would be only {len(full)} characters; write at least "
                     f"{C.MIN_TITLE_LEN - (len(full) - len(core))} characters")
    if not C.title_complete(core):
        probs.append("meta_title ends on a dangling word or separator — make it a finished phrase")
    d = reg.title_dup(core)
    if d:
        probs.append(f'meta_title is too similar to an existing title: "{d}"')
    return probs


def _val_desc(desc: str, reg: C.Registry) -> list:
    probs = []
    if not desc:
        return ["meta_description is empty"]
    if len(desc) > C.MAX_DESC_LEN:
        probs.append(f"meta_description is {len(desc)} characters but must be at most {C.MAX_DESC_LEN}")
    if len(desc) < C.MIN_DESC_LEN:
        probs.append(f"meta_description is only {len(desc)} characters; write {C.TARGET_DESC_MIN}-{C.TARGET_DESC_MAX}")
    if not C.sentence_complete(desc):
        probs.append("meta_description must end with a complete sentence ending in . ! or ?")
    if "..." in desc or "\u2026" in desc:
        probs.append("meta_description must not contain an ellipsis")
    d = reg.desc_dup(desc)
    if d:
        probs.append(f'meta_description is too similar to an existing description: "{d}"')
    return probs


def _val_h1(h1: str, reg: C.Registry) -> list:
    probs = []
    if len(h1) > C.MAX_H1_LEN:
        probs.append(f"h1 is {len(h1)} characters but must be at most {C.MAX_H1_LEN}")
    d = reg.h1_dup(h1)
    if d:
        probs.append(f'h1 is too similar to an existing h1: "{d}"')
    return probs


def validate(cand: dict, cfg: GenConfig, reg: C.Registry) -> list:
    """List of human-readable problems; empty means the candidate is acceptable."""
    probs = _val_title(cand["core"], cfg, reg) + _val_desc(cand["desc"], reg)
    if cfg.do_h1 and cand.get("h1"):
        probs += _val_h1(cand["h1"], reg)
    return probs


def validate_field(field: str, value: str, cfg: GenConfig, reg: C.Registry) -> list:
    """Problems with ONE field ('title' = the part before the brand, 'desc', 'h1')."""
    if field == "title":
        return _val_title(value, cfg, reg)
    if field == "desc":
        return _val_desc(value, reg)
    return _val_h1(value, reg) if value else ["h1 is empty"]


def refine(ai: AI, cfg: GenConfig, reg: C.Registry, page: dict, cand: dict, token_sets) -> tuple:
    """Rewrite (never trim) until the candidate validates. Returns (cand, problems_left)."""
    probs = validate(cand, cfg, reg)
    rounds = 0
    while probs and rounds < MAX_REFINE_ROUNDS:
        rounds += 1
        cfg.say(f"  {page['page_name']}: rewriting ({rounds}/{MAX_REFINE_ROUNDS}) — {probs[0][:90]}", "warn")
        avoid = reg.nearest(token_sets, NEIGHBORS_SINGLE, NEIGHBORS_SINGLE)
        prompt = build_rewrite_prompt(page, cand, probs, cfg, avoid)
        try:
            data, cost = ai.ask_json(cfg.rw_model, prompt, 1500)
        except FatalAPIError:
            raise
        except Exception as e:                           # noqa: BLE001
            cfg.say(f"  rewrite call failed: {e}", "err")
            break
        page["_cost"] = page.get("_cost", 0.0) + cost
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict):
            continue
        t = C.strip_brand(data.get("meta_title", ""), cfg.brand)
        if t:
            cand["core"] = t
        d = C.clean_text(data.get("meta_description", ""))
        if d:
            cand["desc"] = d
        h = C.clean_text(data.get("h1", ""))
        if cfg.do_h1 and h:
            cand["h1"] = h
        probs = validate(cand, cfg, reg)
    return cand, probs


# ─────────────────────────────────────────────────────────────────────────────
# Batch generation
# ─────────────────────────────────────────────────────────────────────────────
def generate(ai: AI, cfg: GenConfig, reg: C.Registry, pages: list, token_sets: list):
    """One API call for `pages`. Returns list of item dicts (or None per missing page)."""
    per = NEIGHBORS_SINGLE if len(pages) == 1 else NEIGHBORS_PER_PAGE
    cap = NEIGHBORS_SINGLE if len(pages) == 1 else NEIGHBORS_BATCH_MAX
    avoid = reg.nearest(token_sets, per, cap)
    prompt = build_generation_prompt(pages, cfg, avoid)
    data, cost = ai.ask_json(cfg.model, prompt, 1100 * len(pages) + 1500)
    for p in pages:
        p["_cost"] = p.get("_cost", 0.0) + cost / len(pages)
    if isinstance(data, dict):
        lst = next((v for v in data.values() if isinstance(v, list)), None)
        data = lst if lst is not None else ([data] if "meta_title" in data else [])
    if not isinstance(data, list):
        data = []
    by_key = {}
    for idx, item in enumerate(data):
        if isinstance(item, dict):
            by_key.setdefault(item.get("page") or f"PAGE_{idx + 1}", item)
    out = []
    for i in range(len(pages)):
        item = by_key.get(f"PAGE_{i + 1}")
        if item is None and i < len(data) and isinstance(data[i], dict) and not data[i].get("page"):
            item = data[i]                    # position fallback ONLY for unlabeled items
        if item and not (str(item.get("meta_title", "")).strip() and str(item.get("meta_description", "")).strip()):
            item = None
        out.append(item)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Image alt text
# ─────────────────────────────────────────────────────────────────────────────
def generate_alts(ai: AI, cfg: GenConfig, page_name: str, h1: str, items: list) -> dict:
    """items: [{'i': index, 'file': 'x.jpg', 'context': 'nearby heading'}]. Returns {index: alt}."""
    if not items:
        return {}
    lines = "\n".join(f'{k + 1}. file="{it["file"]}" nearby_heading="{it["context"]}"' for k, it in enumerate(items))
    prompt = f"""Write concise, accurate image alt text for a web page.
Page: {page_name} | H1: {h1}
You cannot see the images — base each alt ONLY on the file name and nearby heading, and keep it plausible and generic enough to be true.
Rules: at most 110 characters, one complete phrase, no "image of"/"picture of", no keyword stuffing, no trailing period.
If the file name suggests a decorative element (icon, divider, background pattern) return "".

Images:
{lines}

Return ONLY raw JSON: {{"1":"alt text","2":"alt text"}}"""
    try:
        data, _ = ai.ask_json(cfg.model, prompt, 1500)
    except FatalAPIError:
        raise
    except Exception as e:                               # noqa: BLE001
        cfg.say(f"  alt-text call failed: {e}", "warn")
        return {}
    res = {}
    if isinstance(data, dict):
        for k, it in enumerate(items):
            v = data.get(str(k + 1))
            if isinstance(v, str):
                res[it["i"]] = C.cap_alt(v.rstrip("."))
    return res


# ─────────────────────────────────────────────────────────────────────────────
# Fetch Details — business facts from the live website
# ─────────────────────────────────────────────────────────────────────────────
DETAIL_FIELDS = ("name", "business_type", "phone", "email", "street", "city", "region",
                 "postal", "country", "hours", "price_range", "same_as", "logo", "context", "services", "areas")


SUMMARY_MAX = 450
CONTEXT_FULL_MAX = 3200
CATALOG_PAGES = 150


def _fit_sentences(text: str, limit: int = SUMMARY_MAX) -> str:
    """Shorten to whole sentences — never cut mid-sentence."""
    text = C.clean_text(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", cut)]
    return cut[:ends[-1]].strip() if ends else cut.rsplit(" ", 1)[0].rstrip(",;: ") + "."


def compose_context(summary: str, services: list, areas: list, limit: int = CONTEXT_FULL_MAX) -> str:
    """The text given to the AI on every run: summary + the COMPLETE service and area lists."""
    parts = [C.clean_text(summary)] if C.clean_text(summary) else []
    svc = [C.clean_text(s) for s in (services or []) if C.clean_text(s)]
    ars = [C.clean_text(a) for a in (areas or []) if C.clean_text(a)]
    if svc:
        parts.append("Services: " + "; ".join(svc) + ".")
    if ars:
        parts.append("Service areas: " + "; ".join(ars) + ".")
    text = "\n".join(parts)
    if len(text) > limit:                      # drop whole trailing items, never half an item
        cut = text[:limit]
        text = cut[:max(cut.rfind(";"), cut.rfind("."))].rstrip("; ") + "."
    return text


def extract_business_details(ai: AI, cfg: GenConfig, site_url: str, log=None) -> dict:
    """Read the site (homepage, key pages, sitemap and a spread of up to 150 pages' titles/H1s), then let
    Claude structure ONLY what is there: contact facts, a short summary, ALL services and ALL areas.
    Returns {'details': {...}, 'warnings': [...], 'sources': [...]}"""
    say = log or (lambda m, t="": None)
    pages = []
    home, final_url = C.http_get(site_url)
    pages.append((final_url, home))
    for link in C.find_info_links(home, final_url, per_category=2, limit=6):
        try:
            html, u = C.http_get(link)
            pages.append((u, html))
        except Exception as e:                           # noqa: BLE001
            say(f"  could not fetch {link}: {e}", "warn")
    try:
        sitemap_urls = C.fetch_sitemap_urls(final_url, 600)
    except Exception:                                    # noqa: BLE001
        sitemap_urls = []
    menu = C.extract_menu_links(home, final_url, 120)
    candidates, seen = [], set()
    site_host = urllib.parse.urlparse(final_url).netloc.lower().removeprefix("www.")
    for u in C.extract_menu_urls(home, final_url, 250) + sitemap_urls:
        k = u.rstrip("/").lower()
        if urllib.parse.urlparse(u).netloc.lower().removeprefix("www.") != site_host:
            continue                                     # only this website's own pages
        if k not in seen:
            seen.add(k)
            candidates.append(u)
    chosen = C.pick_catalog_urls(candidates, CATALOG_PAGES)
    say(f"  reading {len(chosen)} pages to list every service and area…", "info")
    catalog = C.fetch_page_catalog(chosen, progress=lambda i, n: say(f"  … {i}/{n} pages", "muted"))
    cat_lines = []
    for e in catalog:
        h = e["h1"] or e["title"]
        cat_lines.append(f"{e['path']} | {h}" + (f" | {e['desc']}" if e["desc"] else ""))
    catalog_blob = "\n".join(cat_lines)[:16000]

    phones, emails, social, jsonld = [], [], [], []
    og_site, home_meta, home_h1 = "", "", ""
    facts_parts, content_parts = [], []
    for n, (url, html) in enumerate(pages):
        h = C.extract_business_hints(html, final_url)
        phones += [p for p in h["phones"] if p not in phones]
        emails += [e for e in h["emails"] if e not in emails]
        social += [s for s in h["social"] if s not in social]
        jsonld += h["jsonld"]
        og_site = og_site or h["site_name"]
        facts_parts.append(f"[{url}] {h['text']}")
        cont = C.extract_content(html)
        if n == 0:
            home_meta = C.read_meta(html, "description")
            home_h1 = " / ".join(cont["h1s"][:2])
        heads = " / ".join(cont["h1s"][:2] + cont["headings"][:12])
        content_parts.append(f"[{url}] title: {h['title']} | headings: {heads} | text: {cont['text'][:1800]}")
    facts_blob = "\n".join(facts_parts)[:6000]
    content_blob = "\n".join(content_parts)[:11000]
    homepage = f"HOMEPAGE TITLE / HEADING / DESCRIPTION: {C.read_title(home)} | {home_h1} | {home_meta}"
    ld_text = json.dumps(jsonld, ensure_ascii=False)[:3500]
    source_text = (facts_blob + " " + content_blob + " " + ld_text + " " + " ".join(phones + emails + social)
                   + " " + homepage + " " + " ".join(menu) + " " + catalog_blob)

    prompt = f"""Extract the business's facts from the website data below.
STRICT RULE: use ONLY information that literally appears in the data. If something is not present, return "" (or [] for lists).
Never guess, infer, or use outside knowledge. Do not invent a street address, phone number, hours, services or locations.

Website: {final_url}
Brand hint: {og_site or cfg.brand or '(none)'}
{homepage}

STRUCTURED DATA FOUND (JSON-LD):
{ld_text}

PHONE LINKS: {phones}
EMAIL LINKS: {emails}
SOCIAL LINKS: {social}

SITE MENU (link texts):
{' | '.join(menu) or '(none found)'}

PAGE CATALOGUE — a spread of the site's pages (path | main heading | description). Every service and every area the business covers is probably listed here:
{catalog_blob or '(none)'}

HEADER / FOOTER / ADDRESS / CONTACT TEXT:
{facts_blob}

KEY PAGES (home, services, areas, about, contact):
{content_blob}

FIELD RULES
"name": the business's own trading name exactly as the site shows it (logo text, footer, JSON-LD) — not a page title with slogans or locations.
"services": EVERY distinct service, product or treatment the business offers — go through the menu, the catalogue and the key pages and do not stop early.
  One short name per item (e.g. "Roof repair", "Tankless water heater installation"). Keep different services separate; merge only true duplicates (singular/plural, same service on several city pages).
  Do NOT include: city/area pages, blog posts, company pages (about, contact, careers, privacy, reviews, gallery, FAQ) or generic words like "Services".
"areas": EVERY city, county, region or neighbourhood the business says it serves (from area pages, the catalogue and the text). Place names only. At most 80, most important first.
"context": 2-3 plain sentences (max {SUMMARY_MAX} characters) saying what the business is, its specialty and the region it mainly serves, plus other stated facts such as years in business, licences, guarantees or opening hours. Do not repeat the full lists here.

Return ONLY raw JSON:
{{"name":"","context":"","services":[],"areas":[],"business_type":"LocalBusiness or Organization or a specific schema.org subtype like Plumber","phone":"","email":"",
"street":"","city":"","region":"state/province","postal":"","country":"2-letter code or name",
"hours":["Mo-Fr 09:00-17:00"],"price_range":"","same_as":[],"logo":""}}
hours must use schema.org openingHours format (Mo,Tu,We,Th,Fr,Sa,Su with 24h times). same_as = social profile URLs only."""
    data, _ = ai.ask_json(cfg.model, prompt, 6000)
    if not isinstance(data, dict):
        data = {}
    list_keys = ("hours", "same_as", "services", "areas")
    details = {k: data.get(k, [] if k in list_keys else "") for k in DETAIL_FIELDS}
    for k in list_keys:
        v = details[k]
        details[k] = [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else ([str(v)] if v else [])
    details["business_type"] = details["business_type"] or "LocalBusiness"
    details["context"] = _fit_sentences(details.get("context"))
    if not details["context"] and home_meta:                # fall back to the site's own description
        details["context"] = _fit_sentences(home_meta)
    if not details["name"]:
        details["name"] = next((n.get("name") for n in jsonld if n.get("name")), "") or og_site

    warnings = verify_details(details, source_text)
    if not details["same_as"]:
        details["same_as"] = social[:8]
    if not details["phone"] and phones:
        details["phone"] = phones[0]
    if not details["email"] and emails:
        details["email"] = emails[0]
    if not details["services"]:
        warnings.append("No services could be identified automatically — please type them in.")
    return {"details": details, "warnings": warnings, "sources": [u for u, _ in pages],
            "pages_read": len(catalog)}


def verify_details(d: dict, source: str) -> list:
    """Blank any value that cannot be found in the downloaded source (anti-hallucination)."""
    warnings = []
    low = source.lower()
    digits = re.sub(r"\D", "", source)

    def drop(key, why):
        warnings.append(f"{key} was not found on the website — removed ({why})")
        d[key] = [] if isinstance(d[key], list) else ""

    if d.get("phone"):
        pd = re.sub(r"\D", "", d["phone"])
        if len(pd) < 7 or pd not in digits:
            drop("phone", d["phone"])
    if d.get("email") and d["email"].lower() not in low:
        drop("email", d["email"])
    if d.get("street"):
        toks = [t for t in re.split(r"\W+", d["street"].lower()) if len(t) > 2]
        if toks and not all(t in low for t in toks):
            drop("street", d["street"])
    for k in ("city", "postal"):
        if d.get(k) and d[k].lower() not in low:
            drop(k, d[k])
    d["same_as"] = [u for u in d.get("same_as", []) if u.lower() in low]

    # services / areas: keep only items whose wording is actually found in the downloaded site data
    def _supported(item: str) -> bool:
        toks = [t.lower() for t in re.split(r"[^A-Za-z0-9]+", item) if len(t) >= 4]
        if not toks:
            return item.lower() in low
        hit = sum(1 for t in toks if t in low or t.rstrip("s") in low)
        return hit * 2 >= len(toks)

    for key, test in (("services", _supported), ("areas", lambda a: a.lower() in low)):
        items = d.get(key) or []
        kept = [x for x in items if test(x)]
        if len(kept) < len(items):
            warnings.append(f"{len(items) - len(kept)} {key} not found on the website were removed")
        d[key] = kept
    return warnings


# ─────────────────────────────────────────────────────────────────────────────
# Deep quality check — the AI-judged points of the SEO score
# ─────────────────────────────────────────────────────────────────────────────
JUDGE_KEYS = {  # JSON key -> score-check id
    "title_intent": "title.intent", "desc_accurate": "desc.accurate", "desc_intent": "desc.intent",
    "h1_title": "h1.title_ai", "schema_type": "schema.type_ai", "content_purpose": "content.purpose",
    "content_wording": "content.wording",
}


def build_judge_prompt(views: list) -> str:
    blocks = []
    for i, v in enumerate(views, 1):
        blocks.append(
            f"ITEM_{i}\n  page: {v['page']}\n  title: {v['title'] or '(none)'}\n  description: {v['desc'] or '(none)'}\n"
            f"  h1: {v['h1'] or '(none)'}\n  schema: {v['schema']}\n  page text: {v['excerpt'] or '(empty)'}")
    return f"""You are a strict, fair SEO quality reviewer. Rate each web page item below on seven criteria,
using ONLY what is shown for that item. Scores are integers 0-10 (0 = absent or harmful, 5 = acceptable but weak,
8 = good, 10 = excellent). Be consistent: the same standard for every item. Judge the writing, not the length.

CRITERIA
title_intent    - A searcher looking for this page's topic would understand the title and want to click it.
desc_accurate   - The description truthfully and specifically describes what the page offers (check against the page text). Vague, generic or misleading = low.
desc_intent     - The description answers the searcher's likely intent and gives a reason to act.
h1_title        - The H1 and the title target the same topic and intent, without contradicting each other.
schema_type     - The schema type suits the page's purpose (0 if there is no schema).
content_purpose - Title, description, H1 and page text together make ONE clear purpose; local/service details agree.
content_wording - Natural, useful, grammatical wording; no keyword stuffing, filler or awkward phrasing.

Also give "issue": the single biggest weakness in at most 12 words (or "" if none).

Return ONLY a raw JSON array with exactly {len(views)} objects in the same order, no markdown:
[{{"n":1,"title_intent":0,"desc_accurate":0,"desc_intent":0,"h1_title":0,"schema_type":0,"content_purpose":0,"content_wording":0,"issue":""}}, ...]

{chr(10).join(blocks)}"""


def judge_pages(ai: AI, cfg: GenConfig, views: list, model: str = "") -> list:
    """Judge page versions (blind). Returns, per view, {'marks': {check_id: 0..1}, 'issue': str} or None."""
    data, _ = ai.ask_json(model or cfg.model, build_judge_prompt(views), 700 + 140 * len(views))
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    by_n = {}
    for idx, item in enumerate(data if isinstance(data, list) else []):
        if isinstance(item, dict):
            by_n.setdefault(item.get("n", idx + 1), item)
    out = []
    for i in range(len(views)):
        item = by_n.get(i + 1)
        if not item:
            out.append(None)
            continue
        marks = {}
        for key, cid in JUDGE_KEYS.items():
            try:
                marks[cid] = max(0.0, min(1.0, float(item[key]) / 10.0))
            except (KeyError, TypeError, ValueError):
                continue
        out.append({"marks": marks, "issue": C.clean_text(item.get("issue", ""))[:90]} if marks else None)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Score protection: re-write ONLY the fields that made the page score lower
# ─────────────────────────────────────────────────────────────────────────────
FIELD_KEYS = {"title": "meta_title", "desc": "meta_description", "h1": "h1"}


def build_regen_prompt(page: dict, fields: list, original: dict, last: dict, locked: dict, feedback: list,
                       topic_words: list, cfg: GenConfig, avoid_entries: list) -> str:
    keys = [FIELD_KEYS[f] for f in fields]
    show = lambda d, ks: "\n".join(f"  {FIELD_KEYS[k]}: {d.get(k) or '(none)'}" for k in ks)
    why = "\n".join(f"- {x}" for x in feedback) or "- the overall score fell; make the wording clearer and closer to the page's topic"
    keep = show(locked, list(locked)) if locked else "  (nothing locked)"
    content = re.sub(r"\s+", " ", page["content"])[:900]
    out_json = ",".join(f'"{k}":"..."' for k in keys)
    topic = ", ".join(topic_words) if topic_words else "(use the page name and content)"
    return f"""You are a senior SEO copywriter. A previous rewrite of this page LOWERED its SEO score,
so rewrite ONLY these fields: {', '.join(keys)}.

PAGE
Name: {page['page_name']}
URL: {page['url'] or '(unknown)'}
Section: {page['folder'] or '(root)'}
Existing H1: {page['h1'] or '(none)'}
Page text: {content}

ORIGINAL TEXT ON THE PAGE (known to score well on these points):
{show(original, fields)}

YOUR LAST ATTEMPT (scored lower):
{show(last, fields)}

WHY IT SCORED LOWER:
{why}

PAGE TOPIC WORDS — work the important ones naturally into the new text: {topic}

ALREADY GOOD — keep consistent with these, do not contradict them:
{keep}

{WRITING_RULES}

LIMITS
{_limits_text(cfg)}
{_avoid_block(avoid_entries)}
Improve on the problems above; you may reuse good phrases from the original. Return ONLY raw JSON with exactly these keys:
{{{out_json}}}"""


def regenerate_fields(ai: AI, cfg: GenConfig, reg: C.Registry, page: dict, fields: list, original: dict, last: dict,
                      locked: dict, feedback: list, topic_words: list, token_sets) -> dict:
    """One AI call for the given fields. Returns {field: new value} for the fields that came back valid."""
    avoid = reg.nearest(token_sets, NEIGHBORS_SINGLE, NEIGHBORS_SINGLE)
    prompt = build_regen_prompt(page, fields, original, last, locked, feedback, topic_words, cfg, avoid)
    before = ai.cost
    data, _ = ai.ask_json(cfg.rw_model, prompt, 1500)
    page["_cost"] = page.get("_cost", 0.0) + (ai.cost - before)
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return {}
    out = {}
    for f in fields:
        raw = data.get(FIELD_KEYS[f])
        if not raw:
            continue
        value = C.strip_brand(raw, cfg.brand) if f == "title" else C.clean_text(raw)
        if not value:
            continue
        probs = validate_field(f, value, cfg, reg)
        if probs:
            cfg.say(f"    new {FIELD_KEYS[f]} rejected: {probs[0][:80]}", "muted")
            continue
        out[f] = value
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Review before writing: a fresh version of ONE field, on request of the person reviewing the page
# ─────────────────────────────────────────────────────────────────────────────
def build_review_prompt(page: dict, field: str, original: dict, current: dict, locked: dict, problems: list,
                        instruction: str, topic_words: list, cfg: GenConfig, avoid_entries: list) -> str:
    key = FIELD_KEYS[field]
    show = lambda d, ks: "\n".join(f"  {FIELD_KEYS[k]}: {d.get(k) or '(none)'}" for k in ks)
    notes = [f"- {x}" for x in problems] + ([f"- Editor's instruction: {instruction.strip()}"] if instruction.strip() else [])
    notes = "\n".join(notes) or "- write a clearly different, better version"
    keep = show(locked, list(locked)) if locked else "  (nothing locked)"
    content = re.sub(r"\s+", " ", page["content"])[:900]
    topic = ", ".join(topic_words) if topic_words else "(use the page name and content)"
    return f"""You are a senior SEO copywriter. The editor reviewing this page asked for a NEW version of its {key}.

PAGE
Name: {page['page_name']}
URL: {page['url'] or '(unknown)'}
Section: {page['folder'] or '(root)'}
Existing H1: {page['h1'] or '(none)'}
Page text: {content}

ORIGINAL TEXT ON THE PAGE:
{show(original, [field])}

CURRENT VERSION (write something different — do not repeat it):
{show(current, [field])}

WHAT TO FIX / EDITOR'S NOTES:
{notes}

PAGE TOPIC WORDS — work the important ones in naturally: {topic}

ALREADY DECIDED — stay consistent with these, do not contradict them:
{keep}

{WRITING_RULES}

LIMITS
{_limits_text(cfg)}
{_avoid_block(avoid_entries)}
Return ONLY raw JSON with exactly this key:
{{"{key}":"..."}}"""


def regenerate_for_review(ai: AI, cfg: GenConfig, reg: C.Registry, page: dict, field: str, original: dict,
                          current: dict, locked: dict, problems: list, instruction: str, topic_words: list,
                          token_sets):
    """One AI call for one field. Returns (value or None, problems). The text is NOT thrown away when it
    fails a check — the reviewer sees it together with the reason and may edit it."""
    avoid = reg.nearest(token_sets, NEIGHBORS_SINGLE, NEIGHBORS_SINGLE)
    prompt = build_review_prompt(page, field, original, current, locked, problems, instruction, topic_words, cfg, avoid)
    before = ai.cost
    data, _ = ai.ask_json(cfg.rw_model, prompt, 800)
    page["_cost"] = page.get("_cost", 0.0) + (ai.cost - before)
    if isinstance(data, list) and data:
        data = data[0]
    raw = data.get(FIELD_KEYS[field]) if isinstance(data, dict) else None
    if not raw:
        return None, ["the AI did not return any text — try again"]
    value = C.strip_brand(raw, cfg.brand) if field == "title" else C.clean_text(raw)
    return value, validate_field(field, value, cfg, reg)
