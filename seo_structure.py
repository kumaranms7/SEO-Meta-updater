"""
seo_structure.py — page structure helpers (pure, no AI, no GUI):

* choose_h1() / demote_extra_h1()   one clear H1 per page: the best H1 is kept, the others become H2
* extract_faq() / build_faq()       FAQPage markup from question headings, <details> and <dl> blocks already on the page
* build_breadcrumb()                BreadcrumbList from the folder path (Home › Section › Page)
* add_node()                        adds a JSON-LD block unless the page already has that type
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

import seo_core as C

_WORD = re.compile(r"[a-z0-9]+")
_BLOCK_SPAN = {"main": re.compile(r"<(main|article)\b[^>]*>.*?</\1\s*>", re.I | re.S),
               "chrome": re.compile(r"<(header|nav|footer|aside)\b[^>]*>.*?</\1\s*>", re.I | re.S)}


def _words(text: str) -> set:
    return {w for w in _WORD.findall((text or "").lower()) if len(w) > 2}


# ─────────────────────────────────────────────────────────────────────────────
# One H1 per page
# ─────────────────────────────────────────────────────────────────────────────
def choose_h1(html: str, title: str = "", page_name: str = "") -> tuple:
    """(index of the H1 to keep, number of H1 tags). The best H1 sits in <main>/<article>, not in the
    header / nav / footer, and shares the most words with the title and page name."""
    mask = C.Mask(html)
    hs = C.find_h1s(html, mask)
    if len(hs) < 2:
        return 0, len(hs)
    main = [m.span() for m in _BLOCK_SPAN["main"].finditer(html)]
    chrome = [m.span() for m in _BLOCK_SPAN["chrome"].finditer(html)]
    ref = _words(title) | _words(page_name)
    best, best_score = 0, None
    for i, (tag, i0, i1) in enumerate(hs):
        score = 0.0
        if any(a <= tag.start < b for a, b in main):
            score += 2
        if any(a <= tag.start < b for a, b in chrome):
            score -= 2
        words = _words(C.strip_tags(html[i0:i1]))
        if words and ref:
            score += 3 * len(words & ref) / len(words | ref)
        if best_score is None or score > best_score + 1e-9:         # ties keep the earlier H1
            best, best_score = i, score
    return best, len(hs)


def demote_extra_h1(html: str, keep: int) -> tuple:
    """Turn every H1 except number `keep` into an H2 (attributes and text untouched). Returns (html, count)."""
    mask = C.Mask(html)
    hs = C.find_h1s(html, mask)
    edits = []
    for i, (tag, i0, i1) in enumerate(hs):
        if i == keep:
            continue
        m = C._H1_CLOSE.search(html, i1)
        edits.append((i1, m.end(), re.sub(r"(?i)h1", "h2", m.group(0))))
        edits.append((tag.start, tag.end, "<h2" + tag.raw[3:]))
    for s, e, rep in sorted(edits, reverse=True):
        html = html[:s] + rep + html[e:]
    return html, len(edits) // 2


# ─────────────────────────────────────────────────────────────────────────────
# FAQ
# ─────────────────────────────────────────────────────────────────────────────
class _FaqParser(HTMLParser):
    """Collects question / answer blocks from the real page content (never nav, footer, scripts…)."""
    _TEXT = {"h2", "h3", "h4", "p", "li", "dt", "dd", "summary"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.skip = 0
        self.buf = None                       # (tag, parts)
        self.blocks = []                      # (kind, text)
        self.details = []                     # open <details>: {"q": str, "a": [str]}
        self.pairs = []                       # finished <details> pairs

    def handle_starttag(self, tag, attrs):
        if tag in C._VOID:
            return
        a = dict(attrs)
        skip = tag in C._SKIP_TAGS or bool(C._SKIP_CLASS.search(f"{a.get('class') or ''} {a.get('id') or ''}"))
        self.stack.append((tag, skip))
        if skip:
            self.skip += 1
        if self.skip:
            return
        if tag == "details":
            self.details.append({"q": "", "a": []})
        if tag in self._TEXT:
            self.buf = (tag, [])

    def handle_endtag(self, tag):
        if tag in C._VOID:
            return
        idx = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if idx is None:
            return
        while len(self.stack) > idx:
            t, skip = self.stack.pop()
            if skip:
                self.skip -= 1
        if self.skip:
            return
        if self.buf and self.buf[0] == tag:
            text = C.clean_text(" ".join(self.buf[1]))
            self.buf = None
            if text:
                if tag == "summary" and self.details and not self.details[-1]["q"]:
                    self.details[-1]["q"] = text
                else:
                    if self.details and tag in ("p", "li", "dd"):
                        self.details[-1]["a"].append(text)
                    self.blocks.append(("h" if tag in ("h2", "h3", "h4") else tag, text))
        if tag == "details" and self.details:
            d = self.details.pop()
            ans = C.clean_text(" ".join(d["a"]))
            if d["q"] and len(ans) >= 40:
                self.pairs.append((d["q"], ans))

    def handle_data(self, data):
        if self.buf is not None and not self.skip:
            self.buf[1].append(data)


def extract_faq(html: str, limit: int = 10) -> list:
    """[(question, answer)] — only what is visibly on the page. Needs at least two to be worth marking up."""
    p = _FaqParser()
    try:
        p.feed(html)
        p.close()
    except Exception:                                          # noqa: BLE001
        return []
    pairs = list(p.pairs)
    blocks = p.blocks
    i = 0
    while i < len(blocks):
        kind, text = blocks[i]
        if kind == "h" and text.endswith("?"):
            ans, j = [], i + 1
            while j < len(blocks) and blocks[j][0] in ("p", "li"):
                ans.append(blocks[j][1])
                j += 1
            a = C.clean_text(" ".join(ans))
            if len(a) >= 40:
                pairs.append((text, a[:600]))
            i = j
            continue
        if kind == "dt" and i + 1 < len(blocks) and blocks[i + 1][0] == "dd" and len(blocks[i + 1][1]) >= 40:
            pairs.append((text, blocks[i + 1][1][:600]))
            i += 2
            continue
        i += 1
    seen, out = set(), []
    for q, a in pairs:
        k = q.lower()
        if k not in seen:
            seen.add(k)
            out.append((q, a[:600]))
    return out[:limit] if len(out) >= 2 else []


def build_faq(pairs: list):
    if len(pairs) < 2:
        return None
    return {"@context": "https://schema.org", "@type": "FAQPage",
            "mainEntity": [{"@type": "Question", "name": q,
                            "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in pairs]}


# ─────────────────────────────────────────────────────────────────────────────
# Breadcrumb
# ─────────────────────────────────────────────────────────────────────────────
def _titled(folder: str) -> str:
    return re.sub(r"[-_]+", " ", folder).strip().title()


def build_breadcrumb(site_url: str, rel: str, page_url: str, page_name: str, home_name: str, folder_info):
    """Home › [sections that have their own index page] › this page.
    `folder_info(dir_rel)` -> (name, url) when that folder has an index page, else None. Home pages get none."""
    rel = rel.replace("\\", "/").lstrip("/")
    parts = rel.split("/")
    is_index = parts[-1].lower() in ("index.html", "index.htm")
    dirs = parts[:-1]
    if not parts[:-1] and is_index:
        return None                                              # the home page
    base = (site_url or "").rstrip("/")
    items = [("Home" if not home_name else home_name, base + "/")]
    walk = dirs[:-1] if is_index else dirs                         # an index page IS its own folder
    for i in range(len(walk)):
        d = "/".join(walk[:i + 1])
        info = folder_info(d)
        if info:
            items.append(info)
    items.append((page_name or _titled(parts[-1].rsplit(".", 1)[0]), page_url))
    if len(items) < 2 or not base:
        return None
    seen, uniq = set(), []
    for name, url in items:
        if url.rstrip("/") not in seen:
            seen.add(url.rstrip("/"))
            uniq.append((name, url))
    if len(uniq) < 2:
        return None
    return {"@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": n, "name": nm, "item": u}
                                for n, (nm, u) in enumerate(uniq, 1)]}


# ─────────────────────────────────────────────────────────────────────────────
# JSON-LD block
# ─────────────────────────────────────────────────────────────────────────────
def add_node(html: str, node: dict):
    """Adds `node` as its own JSON-LD block. Returns (html, 'added' | 'present' | 'no_head' | 'skipped')."""
    want = node.get("@type")
    for b in C.parse_ld_blocks(html):
        if b.data is None:
            continue
        for n in C._iter_nodes(b.data):
            if want in C._types(n):
                return html, "present"
    blocks = C.parse_ld_blocks(html)
    block = f'<script type="application/ld+json">\n{C.dump_ld(node)}\n</script>'
    if blocks:
        last = blocks[-1]
        return html[:last.end] + "\n" + block + html[last.end:], "added"
    mask = C.Mask(html)
    hb = C.head_bounds(html, mask)
    if not hb:
        return html, "no_head"
    ind = C._indent(html, hb)
    pos = hb[1]
    ls = html.rfind("\n", 0, pos)
    if ls != -1 and not html[ls + 1:pos].strip():
        pos = ls + 1
        return html[:pos] + ind + block.replace("\n", "\n" + ind) + "\n" + html[pos:], "added"
    return html[:pos] + "\n" + ind + block + "\n" + html[pos:], "added"
