"""
seo_score.py — the internal 100-point SEO Content Quality Score (pure Python, no GUI, no API).

IMPORTANT: this is the tool's own benchmark, NOT a Google score. Google publishes no 100-point on-page score.

How it works
------------
* 7 categories with fixed weights (15/15/20/10/10/15/15) made of ~40 small checks.
* Every check is tagged  A  (decided by code, free)  or  AI  (judged by Claude, optional "Deep check").
  With the AI switch off, each category is rescaled  earned / available x category max  so the 100-point
  scale and the weights stay the same.  Checks that cannot run (n/a) are rescaled the same way.
* The SAME rules score the original HTML ("before") and the final HTML ("after").
* Scores are stored compactly:  f = per-check percentages (-1 = n/a / not judged yet).
"""
from __future__ import annotations

import re
import urllib.parse
from collections import Counter
from pathlib import Path

import seo_core as C

SCORING_VERSION = "1.1"
# 1.1: the page topic is built from the URL name and folder FIRST; the original H1 is used only when those say
# too little. (1.0 always included the original H1, so a rewritten H1 could never beat the old one.)
# Stored 1.0 scores use the same checks, so they stay readable.
COMPATIBLE_VERSIONS = {"1.0", "1.1"}

CATS = [
    ("title", "Meta title", 15),
    ("desc", "Meta description", 15),
    ("h1", "H1 & on-page relevance", 20),
    ("canon", "Canonical & robots", 10),
    ("social", "Open Graph & social tags", 10),
    ("schema", "Structured data", 15),
    ("content", "Content relevance & consistency", 15),
]
CAT_NAME = {c: n for c, n, _ in CATS}
CAT_MAX = {c: m for c, _, m in CATS}

# (id, category, label, max points, kind)   kind: A = code check, AI = judged by Claude
CHECKS = [
    ("title.exists", "title", "Title tag exists", 2, "A"),
    ("title.length", "title", "Title length is 30–60 characters", 2, "A"),
    ("title.topic", "title", "Title covers the page topic", 3, "A"),
    ("title.intent", "title", "Title matches search intent", 3, "AI"),
    ("title.unique", "title", "Title is unique on the site", 3, "A"),
    ("title.natural", "title", "Title is natural, not stuffed", 2, "A"),

    ("desc.exists", "desc", "Description exists", 2, "A"),
    ("desc.length", "desc", "Description length is 70–160 characters", 2, "A"),
    ("desc.overlap", "desc", "Description reflects the page content", 2, "A"),
    ("desc.accurate", "desc", "Description accurately describes the page", 2, "AI"),
    ("desc.topic", "desc", "Description covers the page topic", 1, "A"),
    ("desc.intent", "desc", "Description matches search intent", 2, "AI"),
    ("desc.unique", "desc", "Description is unique on the site", 2, "A"),
    ("desc.sentence", "desc", "Description is a finished sentence", 2, "A"),

    ("h1.single", "h1", "Exactly one clear H1", 4, "A"),
    ("h1.content", "h1", "H1 matches the page content", 5, "A"),
    ("h1.topic", "h1", "H1 covers the page topic", 4, "A"),
    ("h1.title", "h1", "H1 and title are aligned", 2, "A"),
    ("h1.title_ai", "h1", "H1 and title serve the same intent", 2, "AI"),
    ("h1.structure", "h1", "Sound heading structure", 3, "A"),

    ("canon.present", "canon", "Canonical tag present", 2, "A"),
    ("canon.valid", "canon", "Canonical URL is valid and on this site", 2, "A"),
    ("canon.index", "canon", "Page is indexable (as intended)", 3, "A"),
    ("canon.robots", "canon", "Robots directives are valid", 3, "A"),

    ("social.basic", "social", "og:title and og:description match the page", 3, "A"),
    ("social.image", "social", "og:image is absolute, exists and is large enough", 3, "A"),
    ("social.url", "social", "og:url matches canonical, og:site_name set", 2, "A"),
    ("social.twitter", "social", "Twitter card tags present", 2, "A"),

    ("schema.valid", "schema", "JSON-LD present and valid", 3, "A"),
    ("schema.type", "schema", "Appropriate schema type", 2, "A"),
    ("schema.type_ai", "schema", "Schema type fits the page purpose", 1, "AI"),
    ("schema.props", "schema", "Required properties present", 3, "A"),
    ("schema.match", "schema", "Schema matches the visible page", 4, "A"),
    ("schema.dupes", "schema", "No conflicting duplicate entities", 2, "A"),

    ("content.purpose", "content", "Content matches the page purpose", 4, "AI"),
    ("content.agree", "content", "H1, title, description and schema agree", 3, "A"),
    ("content.wording", "content", "Natural, useful wording", 3, "AI"),
    ("content.dup", "content", "No duplicate content or stuffing", 2, "A"),
    ("content.brand", "content", "Brand name is consistent", 3, "A"),
]
CHECK_INDEX = {c[0]: i for i, c in enumerate(CHECKS)}
CHECK_INFO = {c[0]: c for c in CHECKS}

# Failing these means "you have to fix it yourself" — the tool cannot (canonical is handled by another app).
MANUAL_IDS = {"canon.present", "canon.valid", "social.image"}
CRIT_CAPS = {"noindex": 69, "canonical_foreign": 79, "no_title": 79, "no_h1": 79}
CRIT_TEXT = {
    "noindex": "Unintended noindex on an indexable page",
    "canonical_foreign": "Canonical points to another website",
    "no_title": "Page has no title tag",
    "no_h1": "Page has no H1",
    "schema_broken": "Broken JSON-LD (syntax error)",
    "dup_title": "Title is identical on several pages",
    "og_image_bad": "Share image is missing or invalid",
    "decreased": "Score went DOWN after optimisation",
}

DECREASE_TOLERANCE = 0.01    # a page only counts as "went down" if it loses more than a hundredth of a point

LABELS = [(97, "Excellent"), (90, "Very good"), (75, "Good"), (50, "Needs improvement"), (0, "Poor")]


def label_for(score: float) -> str:
    s = round(score)
    for floor, name in LABELS:
        if s >= floor:
            return name
    return "Poor"


# ─────────────────────────────────────────────────────────────────────────────
# Term helpers (topic = slug + original H1 + sub-headings — the same for before and after)
# ─────────────────────────────────────────────────────────────────────────────
_STOP = set("""the and for with from that this your you our are was were have has can will all any not but into out
use using over under about more most than then them they their its one two get got new best top http https www html
htm com php index home page pages in on at of to by an or as is it be we us a""".split())


def _stem(w: str) -> str:
    for suf in ("ing", "es", "ed", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[:-len(suf)]
    return w


_ORIG: dict = {}            # stem -> a real word it came from, so messages say "service", not "servic"


def terms(text: str) -> list:
    """Unique significant stems, in order of appearance."""
    out, seen = [], set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) < 3 or w in _STOP or w.isdigit():
            continue
        s = _stem(w)
        _ORIG.setdefault(s, w)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def topic_terms(page_name: str, folder: str, h1: str, headings: list, limit: int = 8) -> list:
    pn = "" if page_name.strip().lower() == "home" else page_name
    out = []

    def add(text):
        for t in terms(text):
            if t not in out:
                out.append(t)
    add(pn)
    add(folder)
    if len(out) < 2:                                  # the original H1 only fills a gap, so it cannot "lock in" its own wording
        add(h1)
    if len(out) < 2:                                  # sub-headings are the last resort
        add(" ".join((headings or [])[:4]))
    return out[:limit]


def _coverage(needles: list, hay: set) -> float:
    if not needles:
        return 1.0
    return sum(1 for n in needles if n in hay) / len(needles)


def _missing(needles: list, hay: set, n: int = 3) -> str:
    miss = [x for x in needles if x not in hay][:n]
    return ", ".join(_ORIG.get(x, x) for x in miss)


# ─────────────────────────────────────────────────────────────────────────────
# Page facts
# ─────────────────────────────────────────────────────────────────────────────
class Facts:
    """Everything the checks need, extracted once per page."""

    def __init__(self, html, path, root, site_url, topic, facts_biz, brand):
        self.html, self.path, self.root = html, path, root
        self.site_url = (site_url or "").rstrip("/")
        self.brand = (brand or "").strip()
        self.biz = facts_biz or {}
        self.title = C.read_title(html)
        self.desc = C.read_meta(html, "description")
        mask = C.Mask(html)
        self.h1s = [C.strip_tags(html[a:b]) for _, a, b in C.find_h1s(html, mask)]
        self.h1 = self.h1s[0] if self.h1s else ""
        self.empty_headings = 0
        for name in ("h2", "h3"):
            for t in C.iter_tags(html, name, mask):
                m = re.compile(r"</%s\s*>" % name, re.I).search(html, t.end)
                if m and not C.strip_tags(html[t.end:m.start()]) and "<img" not in html[t.end:m.start()].lower():
                    self.empty_headings += 1
        cont = C.extract_content(html)
        self.body = cont["text"]
        self.headings = cont["headings"]
        self.canonical = C.get_canonical(html)               # as written (not joined) so absolutes are checked
        self.robots = C.read_meta(html, "robots").lower()
        self.og = {k: C.read_meta(html, k) for k in
                   ("og:title", "og:description", "og:image", "og:url", "og:site_name", "og:type")}
        self.tw = {k: C.read_meta(html, k) for k in ("twitter:card", "twitter:title", "twitter:description")}
        self.blocks = C.parse_ld_blocks(html)
        self.nodes = []
        for b in self.blocks:
            if b.data is not None:
                self.nodes.extend(C._iter_nodes(b.data))
        self.topic = topic
        self.title_terms = set(terms(self.title))
        self.desc_terms = set(terms(self.desc))
        self.h1_terms = set(terms(self.h1))
        self.body_terms_list = terms(self.body)
        self.body_terms = set(self.body_terms_list)
        freq = Counter(_stem(w) for w in re.findall(r"[a-z]{4,}", self.body.lower()) if w not in _STOP)
        self.top_body_terms = [w for w, _ in freq.most_common(8)]


# ─────────────────────────────────────────────────────────────────────────────
# Individual checks: return (fraction 0..1 | None, short reason)
# ─────────────────────────────────────────────────────────────────────────────
def _chk_title_exists(f: Facts):
    return (1.0, "") if f.title else (0.0, "no <title> tag")


def _chk_title_length(f: Facts):
    n = len(f.title)
    if not n:
        return 0.0, "no title"
    if 30 <= n <= 60:
        return 1.0, ""
    if 20 <= n < 30 or 60 < n <= 70:
        return 0.5, f"{n} chars (ideal 30–60)"
    return 0.0, f"{n} chars (ideal 30–60)"


def _chk_title_topic(f: Facts):
    if not f.topic:
        return None, ""
    need = f.topic[:4]
    cov = _coverage(need, f.title_terms)
    return cov, "" if cov >= 1 else f"missing topic words: {_missing(need, f.title_terms)}"


def _stuffing_penalty(text: str) -> tuple:
    letters = re.findall(r"[A-Za-z]", text)
    pen, why = 0.0, []
    if len(letters) > 8 and sum(1 for c in letters if c.isupper()) / len(letters) > 0.6:
        pen += 0.5
        why.append("mostly CAPITALS")
    words = [w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in _STOP]
    if words:
        top, cnt = Counter(words).most_common(1)[0]
        if cnt >= 3:
            pen += 0.5
            why.append(f"'{top}' repeated {cnt}×")
    if len(re.findall(r"[|•»·]|\s[-–—]\s", text)) >= 3:
        pen += 0.3
        why.append("many separators")
    if text.count(",") >= 4:
        pen += 0.3
        why.append("keyword list")
    return pen, why


def _chk_title_natural(f: Facts):
    if not f.title:
        return 0.0, "no title"
    pen, why = _stuffing_penalty(f.title)
    return max(0.0, 1 - pen), ("; ".join(why) if why else "")


def _chk_desc_exists(f: Facts):
    return (1.0, "") if f.desc else (0.0, "no meta description")


def _chk_desc_length(f: Facts):
    n = len(f.desc)
    if not n:
        return 0.0, "no description"
    if 70 <= n <= 160:
        return 1.0, ""
    if 50 <= n < 70 or 160 < n <= 180:
        return 0.5, f"{n} chars (ideal 70–160)"
    return 0.0, f"{n} chars (ideal 70–160)"


def _chk_desc_overlap(f: Facts):
    if not f.top_body_terms:
        return None, ""
    hit = sum(1 for t in f.top_body_terms if t in f.desc_terms)
    need = max(2.0, 0.4 * len(f.top_body_terms))
    frac = min(1.0, hit / need)
    return frac, "" if frac >= 1 else "description shares few words with the page content"


def _chk_desc_topic(f: Facts):
    if not f.topic:
        return None, ""
    need = f.topic[:4]
    cov = _coverage(need, f.desc_terms)
    return min(1.0, cov * 2), "" if cov >= 0.5 else f"missing topic words: {_missing(need, f.desc_terms)}"


def _chk_desc_sentence(f: Facts):
    if not f.desc:
        return 0.0, "no description"
    pts, why = 0.0, []
    if C.sentence_complete(f.desc):
        pts += 0.5
    else:
        why.append("does not end like a sentence")
    if "..." not in f.desc and "…" not in f.desc and C.title_complete(f.desc.rstrip(".!? ")):
        pts += 0.5
    else:
        why.append("cut off / ellipsis")
    return pts, "; ".join(why)


def _chk_h1_single(f: Facts):
    if not f.h1s:
        return 0.0, "no H1"
    if len(f.h1s) > 1:
        return 0.25, f"{len(f.h1s)} H1 tags"
    n = len(f.h1)
    if not n:
        return 0.0, "empty H1"
    if 10 <= n <= 70:
        return 1.0, ""
    return 0.5, f"H1 is {n} chars (ideal 10–70)"


def _chk_h1_content(f: Facts):
    if not f.h1:
        return 0.0, "no H1"
    if not f.body_terms:
        return None, ""
    need = list(f.h1_terms)
    if not need:
        return 0.0, "H1 has no meaningful words"
    cov = _coverage(need, f.body_terms)
    return min(1.0, cov / 0.7), "" if cov >= 0.7 else "H1 words are rare in the page text"


def _chk_h1_topic(f: Facts):
    if not f.topic:
        return None, ""
    if not f.h1:
        return 0.0, "no H1"
    need = f.topic[:4]
    cov = _coverage(need, f.h1_terms)
    return min(1.0, cov / 0.75), "" if cov >= 0.75 else f"missing topic words: {_missing(need, f.h1_terms)}"


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b and (a | b) else 0.0


def _chk_h1_title(f: Facts):
    if not f.h1 or not f.title:
        return 0.0, "needs both H1 and title"
    j = _jaccard(f.h1_terms, f.title_terms)
    return min(1.0, j / 0.35), "" if j >= 0.35 else "H1 and title share few words"


def _chk_h1_structure(f: Facts):
    pts, why = 0.0, []
    if len(f.h1s) == 1:
        pts += 1
    else:
        why.append("not exactly one H1")
    if f.empty_headings == 0:
        pts += 1
    else:
        why.append(f"{f.empty_headings} empty heading(s)")
    if len(f.body) < 600 or f.headings:
        pts += 1
    else:
        why.append("long page without sub-headings")
    return pts / 3, "; ".join(why)


def _chk_canon_present(f: Facts):
    return (1.0, "") if f.canonical else (0.0, "no canonical tag")


def _host(u: str) -> str:
    return urllib.parse.urlparse(u).netloc.lower().removeprefix("www.")


def _chk_canon_valid(f: Facts):
    if not f.canonical:
        return 0.0, "no canonical tag"
    if not re.match(r"^https?://", f.canonical, re.I):
        return 0.3, "canonical is not an absolute URL"
    if f.site_url and _host(f.canonical) != _host(f.site_url):
        return 0.0, "canonical is on another website"
    if "#" in f.canonical or "?" in f.canonical:
        return 0.6, "canonical has ? or # parts"
    return 1.0, ""


_ROBOTS_OK = {"index", "noindex", "follow", "nofollow", "all", "none", "noarchive", "nosnippet", "noimageindex",
              "notranslate", "indexifembedded"}


def _noindex(f: Facts) -> bool:
    return "noindex" in f.robots or re.search(r"\bnone\b", f.robots) is not None


def _chk_canon_index(f: Facts, intended_noindex: bool):
    if not _noindex(f):
        return 1.0, ""
    return (1.0, "noindex (intended)") if intended_noindex else (0.0, "page is set to noindex")


def _chk_canon_robots(f: Facts):
    if not f.robots:
        return 1.0, ""                                   # no tag = default "index, follow" — valid
    toks = [t.strip() for t in f.robots.split(",") if t.strip()]
    bad = [t for t in toks if t.split(":")[0] not in _ROBOTS_OK and not t.startswith(("max-", "unavailable_after"))]
    if bad:
        return 0.0, f"unknown directive: {bad[0]}"
    if "index" in toks and "noindex" in toks:
        return 0.0, "index and noindex together"
    return 1.0, ""


def _same(a: str, b: str, thr: float = 0.85) -> bool:
    a, b = C._norm(a), C._norm(b)
    if not a or not b:
        return False
    import difflib
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= thr


def _chk_social_basic(f: Facts):
    pts, why = 0.0, []
    for key, mine, third in (("og:title", f.title, 0.5), ("og:description", f.desc, 0.5)):
        v = f.og[key]
        if not v:
            why.append(f"{key} missing")
        else:
            pts += third * (2 / 3)
            if mine and _same(v, mine):
                pts += third * (1 / 3)
            elif mine:
                why.append(f"{key} differs from the page's own text")
    return pts, "; ".join(why)


def _og_image_state(f: Facts):
    """-> (fraction, reason, bad: bool)"""
    src = f.og["og:image"]
    if not src:
        return 0.0, "no og:image", False
    pts, why = 1 / 3, []
    if re.match(r"^https?://", src, re.I):
        pts += 1 / 3
    else:
        why.append("og:image is not an absolute URL")
        return pts, "; ".join(why), True
    res = C.resolve_image(src, f.path, f.root, f.site_url)
    if res and res["local"]:
        sz = C.image_size(res["local"])
        if sz and sz[0] >= 600:
            pts += 1 / 3
        elif sz:
            pts += 1 / 6
            why.append(f"image is only {sz[0]}px wide (ideal 1200)")
        else:
            pts += 1 / 6
    elif res and not res["exists"]:
        why.append("image file not found on disk")
        return pts, "; ".join(why), True
    else:
        pts += 1 / 3                                     # external / cannot check → trust it
    return pts, "; ".join(why), False


def _chk_social_image(f: Facts):
    frac, why, _ = _og_image_state(f)
    return frac, why


def _chk_social_url(f: Facts):
    pts, why = 0.0, []
    if f.og["og:url"]:
        if f.canonical and _same(f.og["og:url"].rstrip("/"), f.canonical.rstrip("/"), 0.98):
            pts += 0.5
        elif not f.canonical:
            pts += 0.25
            why.append("no canonical to compare og:url with")
        else:
            why.append("og:url differs from canonical")
    else:
        why.append("og:url missing")
    if f.og["og:site_name"]:
        pts += 0.5
    else:
        why.append("og:site_name missing")
    return pts, "; ".join(why)


def _chk_social_twitter(f: Facts):
    got = sum(1 for k in ("twitter:card", "twitter:title", "twitter:description") if f.tw[k])
    return got / 3, "" if got == 3 else f"{3 - got} twitter tag(s) missing"


def _chk_schema_valid(f: Facts):
    if not f.blocks:
        return 0.0, "no JSON-LD"
    bad = sum(1 for b in f.blocks if b.data is None)
    if bad:
        return 0.0, f"{bad} JSON-LD block(s) with syntax errors"
    return 1.0, ""


_KNOWN_TYPES = {"WebPage", "AboutPage", "ContactPage", "CollectionPage", "FAQPage", "Service", "LocalBusiness",
                "Organization", "Article", "BlogPosting", "NewsArticle", "Product", "WebSite", "BreadcrumbList",
                "ItemPage", "ProfilePage", "QAPage"}


def _primary_nodes(f: Facts):
    skip = {"BreadcrumbList", "WebSite", "ImageObject", "SearchAction", "FAQPage"}
    return [n for n in f.nodes if not set(C._types(n)) <= skip and C._types(n)]


def _chk_schema_type(f: Facts):
    nodes = _primary_nodes(f)
    if not nodes:
        return 0.0, "no content schema (WebPage/Service/…)"
    for n in nodes:
        for t in C._types(n):
            if t in _KNOWN_TYPES or C._family(t) == "org":
                return 1.0, ""
    return 0.5, "unusual schema type"


_REQUIRED = {"LocalBusiness": ["name", "address"], "Service": ["name", "provider"],
             "Article": ["headline", "image", "datePublished", "author"], "Organization": ["name", "url"],
             "WebPage": ["name", "description"], "FAQPage": ["mainEntity"]}


def _chk_schema_props(f: Facts):
    nodes = _primary_nodes(f)
    if not nodes:
        return 0.0, "no content schema"
    fracs, why = [], []
    for n in nodes:
        t = next((t for t in C._types(n) if t in _REQUIRED), None)
        if t is None:
            fam = "LocalBusiness" if any(C._family(x) == "org" for x in C._types(n)) else None
            t = fam
        if t is None:
            continue
        need = _REQUIRED[t]
        have = [k for k in need if not C._empty(n.get(k))]
        fracs.append(len(have) / len(need))
        if len(have) < len(need):
            why.append(f"{t} missing: {', '.join(k for k in need if k not in have)}")
    if not fracs:
        return None, ""
    return sum(fracs) / len(fracs), "; ".join(why[:2])


def _chk_schema_match(f: Facts):
    nodes = _primary_nodes(f)
    if not nodes:
        return 0.0, "no content schema"
    n = nodes[0]
    parts, why = [], []
    sd = str(n.get("description", ""))
    if sd and f.desc:
        ok = _same(sd, f.desc, 0.75)
        parts.append(1.0 if ok else 0.0)
        if not ok:
            why.append("schema description differs from meta description")
    nm = str(n.get("name", n.get("headline", "")))
    if nm:
        j = _jaccard(set(terms(nm)), f.h1_terms | f.title_terms)
        parts.append(min(1.0, j / 0.3))
        if j < 0.3:
            why.append("schema name differs from H1/title")
    if n.get("url") and f.canonical:
        ok = _same(str(n["url"]).rstrip("/"), f.canonical.rstrip("/"), 0.98)
        parts.append(1.0 if ok else 0.0)
        if not ok:
            why.append("schema url differs from canonical")
    if f.biz.get("phone") and n.get("telephone"):
        digits = re.sub(r"\D", "", str(n["telephone"]))
        page_digits = re.sub(r"\D", "", f.body)
        parts.append(1.0 if digits and digits in page_digits else 0.5)
        if not (digits and digits in page_digits):
            why.append("schema phone not visible on page")
    if not parts:
        return None, ""
    return sum(parts) / len(parts), "; ".join(why[:2])


def _chk_schema_dupes(f: Facts):
    nodes = _primary_nodes(f)
    if not nodes:
        return 0.0, "no content schema"
    fam = Counter(C._family(t) for n in nodes for t in C._types(n)[:1])
    dup = sum(c - 1 for c in fam.values() if c > 1)
    pen = 0.5 * dup
    why = [f"{dup} duplicate entity type(s)"] if dup else []
    claims = any(n.get("aggregateRating") or n.get("review") for n in nodes)
    if claims and not re.search(r"review|rating|testimonial|stars", f.body, re.I):
        pen += 0.5
        why.append("rating/review in schema but none on the page")
    return max(0.0, 1 - pen), "; ".join(why)


def _chk_content_agree(f: Facts):
    pairs = [(f.title_terms, f.h1_terms), (f.title_terms, f.desc_terms), (f.h1_terms, f.desc_terms)]
    vals = [_jaccard(a, b) for a, b in pairs if a and b]
    if not vals:
        return 0.0, "missing title / H1 / description"
    avg = sum(vals) / len(vals)
    n = _primary_nodes(f)
    if n:
        nm = set(terms(str(n[0].get("name", n[0].get("headline", "")))))
        if nm:
            avg = (avg * len(vals) + _jaccard(nm, f.h1_terms | f.title_terms)) / (len(vals) + 1)
    return min(1.0, avg / 0.3), "" if avg >= 0.3 else "title, H1 and description say different things"


def _chk_content_brand(f: Facts):
    if not f.brand:
        return None, ""
    b = f.brand.lower()
    parts, why = [], []
    parts.append(1.0 if b in f.title.lower() else 0.0)
    if parts[-1] == 0:
        why.append("brand not in title")
    sn = f.og["og:site_name"]
    parts.append(1.0 if sn and b in sn.lower() else 0.0)
    if parts[-1] == 0:
        why.append("brand not in og:site_name")
    n = _primary_nodes(f)
    if n:
        txt = " ".join(str(v) for x in n for v in (x.get("name"), (x.get("provider") or {}).get("name") if isinstance(x.get("provider"), dict) else None, (x.get("publisher") or {}).get("name") if isinstance(x.get("publisher"), dict) else None, (x.get("isPartOf") or {}).get("name") if isinstance(x.get("isPartOf"), dict) else None) if v).lower()
        parts.append(1.0 if b in txt else 0.0)
        if parts[-1] == 0:
            why.append("brand not in schema")
    return sum(parts) / len(parts), "; ".join(why)


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────
def score_page(html: str, *, path, root, site_url: str = "", brand: str = "", biz: dict | None = None,
               topic: list | None = None, page_name: str = "", folder: str = "",
               intended_noindex: bool = False, dup_similar: bool = False) -> dict:
    """Score one page with the free (code) checks. Returns a compact, JSON-safe blob."""
    if topic is None:
        h1 = ""
        mask = C.Mask(html)
        hs = C.find_h1s(html, mask)
        if hs:
            h1 = C.strip_tags(html[hs[0][1]:hs[0][2]])
        topic = topic_terms(page_name, folder, h1, C.extract_content(html)["headings"])
    f = Facts(html, path, root, site_url, topic, biz, brand)
    fns = {
        "title.exists": _chk_title_exists, "title.length": _chk_title_length, "title.topic": _chk_title_topic,
        "title.natural": _chk_title_natural,
        "desc.exists": _chk_desc_exists, "desc.length": _chk_desc_length, "desc.overlap": _chk_desc_overlap,
        "desc.topic": _chk_desc_topic, "desc.sentence": _chk_desc_sentence,
        "h1.single": _chk_h1_single, "h1.content": _chk_h1_content, "h1.topic": _chk_h1_topic,
        "h1.title": _chk_h1_title, "h1.structure": _chk_h1_structure,
        "canon.present": _chk_canon_present, "canon.valid": _chk_canon_valid,
        "canon.robots": _chk_canon_robots,
        "social.basic": _chk_social_basic, "social.image": _chk_social_image, "social.url": _chk_social_url,
        "social.twitter": _chk_social_twitter,
        "schema.valid": _chk_schema_valid, "schema.type": _chk_schema_type, "schema.props": _chk_schema_props,
        "schema.match": _chk_schema_match, "schema.dupes": _chk_schema_dupes,
        "content.agree": _chk_content_agree, "content.brand": _chk_content_brand,
    }
    fr = [-1] * len(CHECKS)
    reasons = {}
    for cid, fn in fns.items():
        val, why = fn(f)
        if val is not None:
            fr[CHECK_INDEX[cid]] = int(round(max(0.0, min(1.0, val)) * 100))
            if why and val < 1:
                reasons[cid] = why[:90]
    val, why = _chk_canon_index(f, intended_noindex)
    fr[CHECK_INDEX["canon.index"]] = int(round(val * 100))
    if why and val < 1:
        reasons["canon.index"] = why
    # duplicate content / stuffing (DupDetector result is supplied by the pipeline)
    pen, swhy = _stuffing_penalty(f.desc + " " + f.h1)
    dup_frac = max(0.0, (0.0 if dup_similar else 1.0) - pen * 0.5)
    fr[CHECK_INDEX["content.dup"]] = int(round(dup_frac * 100))
    if dup_frac < 1:
        reasons["content.dup"] = ("near-duplicate page content" if dup_similar else "; ".join(swhy))[:90]

    crit = []
    if not f.title:
        crit.append("no_title")
    if not f.h1:
        crit.append("no_h1")
    if _noindex(f) and not intended_noindex:
        crit.append("noindex")
    if f.canonical and re.match(r"^https?://", f.canonical, re.I) and f.site_url and _host(f.canonical) != _host(f.site_url):
        crit.append("canonical_foreign")
    if any(b.data is None for b in f.blocks):
        crit.append("schema_broken")
    if _og_image_state(f)[2]:
        crit.append("og_image_bad")
    blob = {"v": SCORING_VERSION, "f": fr, "r": reasons, "crit": crit, "topic": topic,
            "man": sorted(c for c in reasons if c in MANUAL_IDS), "ai": False}
    return recompute(blob)


def recompute(blob: dict, use_ai: bool = False) -> dict:
    """(Re)calculate category scores, total, cap and label from the per-check percentages."""
    cats = {c: [0.0, 0.0] for c, _, _ in CATS}                # earned, available
    for i, (cid, cat, _lbl, mx, kind) in enumerate(CHECKS):
        v = blob["f"][i]
        if v < 0 or (kind == "AI" and not use_ai):
            continue
        cats[cat][0] += mx * v / 100
        cats[cat][1] += mx
    out, total = {}, 0.0
    sc = blob.get("sc", 1.0)                              # >1 only for estimated reports that score fewer categories
    for cat, _n, mx in CATS:
        e, a = cats[cat]
        out[cat] = round(e / a * mx * sc, 2) if a else 0.0
        total += out[cat]
    cap = min((CRIT_CAPS[c] for c in blob.get("crit", []) if c in CRIT_CAPS), default=None)
    capped = min(total, cap) if cap is not None else total
    blob.update({"cats": out, "raw": round(total, 2), "t": round(capped, 2), "cap": cap, "use_ai": use_ai,
                 "l": label_for(capped)})
    return blob


def ai_fill(blob: dict, judged: dict) -> dict:
    """judged: {check_id: fraction 0..1}. Fills the AI checks, then recomputes in Deep mode."""
    for cid, frac in judged.items():
        if cid in CHECK_INDEX and CHECKS[CHECK_INDEX[cid]][4] == "AI":
            blob["f"][CHECK_INDEX[cid]] = int(round(max(0.0, min(1.0, float(frac))) * 100))
    blob["ai"] = True
    return recompute(blob, use_ai=True)


def explain(blob: dict) -> list:
    """Per-check rows for the detail view / report: id, label, max, earned, kind, ok, reason, manual."""
    rows = []
    for i, (cid, cat, lbl, mx, kind) in enumerate(CHECKS):
        v = blob["f"][i]
        if v < 0:
            state = "n/a" if kind == "A" else "not judged"
            rows.append({"id": cid, "cat": cat, "label": lbl, "max": mx, "earned": None, "kind": kind,
                         "ok": None, "reason": state, "manual": False})
            continue
        rows.append({"id": cid, "cat": cat, "label": lbl, "max": mx, "earned": round(mx * v / 100, 2), "kind": kind,
                     "ok": v >= 100, "reason": blob.get("r", {}).get(cid, ""), "manual": cid in blob.get("man", [])})
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Site-wide checks and aggregation
# ─────────────────────────────────────────────────────────────────────────────
def apply_site_checks(items: list) -> None:
    """items: dicts with 'before'/'after' blobs and 'old_title','new_title','old_description','new_description'.
    Fills the cross-page checks (title/description uniqueness) with EXACT normalised comparison, separately for the
    before and after state, then recomputes totals.  Idempotent."""
    for side, tkey, dkey in (("before", "old_title", "old_description"), ("after", "new_title", "new_description")):
        sub = [it for it in items if it.get(side)]
        tc = Counter(C._norm(it.get(tkey, "")) for it in sub if C._norm(it.get(tkey, "")))
        dc = Counter(C._norm(it.get(dkey, "")) for it in sub if C._norm(it.get(dkey, "")))
        for it in sub:
            b = it[side]
            for key, cid, cnt, label in ((tkey, "title.unique", tc, "title"), (dkey, "desc.unique", dc, "description")):
                norm = C._norm(it.get(key, ""))
                idx = CHECK_INDEX[cid]
                if not norm:
                    b["f"][idx] = 0
                    b.setdefault("r", {})[cid] = f"no {label}"
                elif cnt[norm] > 1:
                    b["f"][idx] = 0
                    b.setdefault("r", {})[cid] = f"same {label} as {cnt[norm] - 1} other page(s)"
                    if cid == "title.unique" and "dup_title" not in b["crit"]:
                        b["crit"].append("dup_title")
                else:
                    b["f"][idx] = 100
                    b.get("r", {}).pop(cid, None)
                    if cid == "title.unique" and "dup_title" in b["crit"]:
                        b["crit"].remove("dup_title")
            recompute(b, use_ai=b.get("ai", False))
    for it in items:
        b, a = it.get("before"), it.get("after")
        if b and a:
            for blob in (a,):
                if a["t"] < b["t"] - DECREASE_TOLERANCE and "decreased" not in blob["crit"]:
                    blob["crit"].append("decreased")
                elif a["t"] >= b["t"] - DECREASE_TOLERANCE and "decreased" in blob["crit"]:
                    blob["crit"].remove("decreased")


def _avg(vals):
    vals = list(vals)
    return sum(vals) / len(vals) if vals else 0.0


def aggregate(items: list, target: float = 90.0, use_ai: bool = False) -> dict:
    """Site summary.  Only pages with BOTH a before and an after score count for improvement figures."""
    both = [it for it in items if it.get("before") and it.get("after")]
    only_before = [it for it in items if it.get("before") and not it.get("after")]
    ub = [recompute(dict(it["before"]), use_ai)["t"] for it in both]
    ua = [recompute(dict(it["after"]), use_ai)["t"] for it in both]
    n = len(both)
    sc = both[0]["after"].get("sc", 1.0) if both else 1.0
    # Every figure shown to the reader is rounded to one decimal FIRST, and everything else is worked out from
    # those rounded figures: the total is the sum of the category rows and each change is after - before, so the
    # table always adds up by hand (+0.1 and +0.1 shows a total of +0.2).
    cat_rows = []
    for cat, name, mx in CATS:
        cb = round(_avg(it["before"]["cats"][cat] for it in both), 2) if n else 0.0
        ca = round(_avg(it["after"]["cats"][cat] for it in both), 2) if n else 0.0
        cat_rows.append({"id": cat, "name": name, "max": round(mx * sc, 2) if sc != 1 else mx, "before": cb, "after": ca,
                         "change": round(ca - cb, 2)})
    avg_b = round(sum(r["before"] for r in cat_rows), 2)
    avg_a = round(sum(r["after"] for r in cat_rows), 2)
    delta = round(avg_a - avg_b, 2)
    dist_b = Counter(label_for(x) for x in ub)
    dist_a = Counter(label_for(x) for x in ua)
    fails = Counter()
    crit_pages = []
    decreased = []
    for it, sb, sa in zip(both, ub, ua):
        for r in explain(it["after"]):
            if r["ok"] is False:
                fails[r["id"]] += 1
        if it["after"].get("crit"):
            crit_pages.append((it["rel"], it["after"]["crit"]))
        if sa < sb - DECREASE_TOLERANCE:
            decreased.append((it["rel"], round(sb, 2), round(sa, 2)))
    gains = sorted(((round(sa - sb, 2), it["rel"], round(sb, 2), round(sa, 2)) for it, sb, sa in zip(both, ub, ua)),
                   reverse=True)
    return {
        "pages": n, "pages_before_only": len(only_before),
        "avg_before": round(avg_b, 2), "avg_after": round(avg_a, 2), "delta": round(delta, 2),
        "relative_pct": round(delta / avg_b * 100, 2) if avg_b else None,
        "target": target,
        "target_before": sum(1 for x in ub if x >= target), "target_after": sum(1 for x in ua if x >= target),
        "excellent_after": sum(1 for x in ua if x >= 97),
        "categories": cat_rows,
        "dist_before": {name: dist_b.get(name, 0) for _, name in LABELS},
        "dist_after": {name: dist_a.get(name, 0) for _, name in LABELS},
        "top_issues": [{"id": cid, "label": CHECK_INFO[cid][2], "pages": c,
                        "manual": cid in MANUAL_IDS} for cid, c in fails.most_common(10)],
        "critical_pages": crit_pages, "decreased": decreased,
        "best_gains": gains[:5], "smallest_gains": gains[-5:] if n > 5 else [],
        "use_ai": use_ai,
        "ai_pages": sum(1 for it in both if it["before"].get("ai") and it["after"].get("ai")),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Compact storage (RunState) — a few hundred bytes per score
# ─────────────────────────────────────────────────────────────────────────────
def compact(blob: dict) -> dict:
    return {"v": blob["v"], "f": blob["f"], "r": blob.get("r", {}), "c": blob.get("crit", []),
            "m": blob.get("man", []), "p": blob.get("topic", []), "ai": bool(blob.get("ai"))}


def expand(c: dict):
    """Inverse of compact(). Returns None if the stored score was made by another scoring version."""
    if not c or c.get("v") not in COMPATIBLE_VERSIONS or len(c.get("f", [])) != len(CHECKS):
        return None
    blob = {"v": SCORING_VERSION, "f": list(c["f"]), "r": dict(c.get("r", {})), "crit": list(c.get("c", [])),
            "man": list(c.get("m", [])), "topic": list(c.get("p", [])), "ai": bool(c.get("ai"))}
    return recompute(blob, use_ai=blob["ai"])


def schema_summary(html: str, limit: int = 220) -> str:
    """One short line describing the page's main schema (for the AI judge)."""
    try:
        nodes = []
        for b in C.parse_ld_blocks(html):
            if b.data is not None:
                nodes.extend(C._iter_nodes(b.data))
        skip = {"BreadcrumbList", "WebSite", "ImageObject", "SearchAction"}
        main = [n for n in nodes if C._types(n) and not set(C._types(n)) <= skip]
        if not main:
            return "(no schema)"
        n = main[0]
        return (f"{'/'.join(C._types(n))}: {n.get('name') or n.get('headline') or ''} — "
                f"{str(n.get('description', ''))[:120]}")[:limit]
    except Exception:
        return "(unreadable schema)"
