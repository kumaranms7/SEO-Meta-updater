"""
seo_core.py — pure-Python HTML / SEO logic for the Top Ten Website Marketing SEO Meta Tag Updater.

No GUI and no API calls live here, so everything in this module can be unit-tested.

Design rules
------------
* Files are read and written with their ORIGINAL encoding and line endings
  (never lossy), see read_html / write_html.
* All edits are exact slice replacements located by a quote-aware tag scanner —
  never regex substitution with AI text — and they ignore HTML comments,
  <script>, <style>, <svg>, <noscript>, <template> and <textarea> regions.
* Text is held as plain text and escaped only when it is written to HTML, so
  lengths are counted on what people actually see ("&" is 1 character).
"""
from __future__ import annotations

import codecs
import difflib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import struct
import time
import urllib.parse
import urllib.request
import html as _html
from bisect import bisect_right
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Limits / thresholds
# ─────────────────────────────────────────────────────────────────────────────
MAX_TITLE_LEN = 60
MIN_TITLE_LEN = 30
MAX_DESC_LEN = 160
MIN_DESC_LEN = 70
TARGET_DESC_MIN = 140
TARGET_DESC_MAX = 158
MAX_H1_LEN = 70

TITLE_SIMILARITY_MAX = 0.88
DESC_SIMILARITY_MAX = 0.80
H1_SIMILARITY_MAX = 0.92


# ─────────────────────────────────────────────────────────────────────────────
# Text helpers
# ─────────────────────────────────────────────────────────────────────────────
def clean_text(s) -> str:
    """Plain, single-spaced text (HTML entities decoded)."""
    s = _html.unescape(str(s or ""))
    return re.sub(r"\s+", " ", s).strip()


def esc_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def esc_attr(s: str) -> str:
    return esc_text(s).replace('"', "&quot;")


def strip_tags(s: str) -> str:
    return clean_text(re.sub(r"<[^>]*>", " ", s or ""))


def sentence_complete(text: str) -> bool:
    """True if text ends like a finished sentence."""
    return bool(text) and text.rstrip()[-1] in ".!?"


_DANGLING_END = re.compile(
    r"(?:\b(?:and|or|the|an|in|on|at|for|to|of|with|by|from|your|our|&|\+|near)\b|[,&+:;|\-–—])\s*$", re.I)


def title_complete(text: str) -> bool:
    """A title must not end on a dangling word or separator."""
    t = (text or "").strip()
    return bool(t) and not _DANGLING_END.search(t)


# ─────────────────────────────────────────────────────────────────────────────
# Title building (brand + separator)
# ─────────────────────────────────────────────────────────────────────────────
def norm_separator(sep: str) -> str:
    """' | ' style separator with exactly one space either side ('' if none)."""
    s = (sep or "").strip()
    return f" {s} " if s else " "


def title_budget(sep: str, brand: str, max_len: int = MAX_TITLE_LEN) -> int:
    """Characters available for the AI-written part of the title."""
    if not brand:
        return max_len
    return max_len - len(norm_separator(sep)) - len(brand)


def strip_brand(core: str, brand: str) -> str:
    """Remove the brand and dangling separators the AI may have added."""
    t = clean_text(core)
    if brand:
        t = re.sub(re.escape(brand), "", t, flags=re.I)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip(" \t|-–—:·•~/,")


def build_title(core: str, sep: str, brand: str) -> str:
    core = strip_brand(core, brand)
    if not brand:
        return core
    return f"{core}{norm_separator(sep)}{brand}" if core else brand


# ─────────────────────────────────────────────────────────────────────────────
# File IO — encoding / newline safe
# ─────────────────────────────────────────────────────────────────────────────
class DecodeError(Exception):
    pass


_CHARSET_RE = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)", re.I)
_ENC_ALIASES = {"iso-8859-1": "cp1252", "latin1": "cp1252", "latin-1": "cp1252",
                "windows-1252": "cp1252", "us-ascii": "utf-8", "ascii": "utf-8"}


def read_html(path):
    """Return (text, encoding, newline). Text always uses '\\n'. Never lossy."""
    raw = Path(path).read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        candidates = ["utf-8-sig"]
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        candidates = ["utf-16"]
    else:
        candidates = ["utf-8"]
        m = _CHARSET_RE.search(raw[:4096])
        if m:
            declared = m.group(1).decode("ascii", "ignore").lower()
            declared = _ENC_ALIASES.get(declared, declared)
            if declared not in candidates:
                candidates.append(declared)
        candidates += ["cp1252", "latin-1"]      # latin-1 round-trips every byte
    last = None
    for enc in candidates:
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError) as e:
            last = e
    else:
        raise DecodeError(f"Cannot decode file: {last}")
    crlf = raw.count(b"\r\n")
    nl = "\r\n" if crlf and crlf >= raw.count(b"\n") / 2 else "\n"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, enc, nl


def write_html(path, text: str, enc: str, nl: str):
    """Atomic write in the file's original encoding and newline style."""
    if nl != "\n":
        text = text.replace("\n", nl)
    data = text.encode(enc, errors="xmlcharrefreplace")
    p = Path(path)
    tmp = p.with_name(p.name + ".seo_tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)


def file_sha1(path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Quote-aware tag scanner
# ─────────────────────────────────────────────────────────────────────────────
_ATTR = r"""(?:[^>"']|"[^"]*"|'[^']*')*"""
_MASK_RE = re.compile(
    r"<!--.*?-->|<(script|style|svg|noscript|template|textarea)\b" + _ATTR + r">.*?</\1\s*>",
    re.I | re.S)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_ATTR_RE = re.compile(r"""([^\s"'<>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?""")
_TAG_RE_CACHE: dict = {}


class Mask:
    """Spans of text that must never be edited (comments, scripts, svg, …)."""

    def __init__(self, html: str, comments_only: bool = False):
        rx = _COMMENT_RE if comments_only else _MASK_RE
        self.spans = [(m.start(), m.end()) for m in rx.finditer(html)]
        self.starts = [s for s, _ in self.spans]

    def __contains__(self, pos: int) -> bool:
        i = bisect_right(self.starts, pos) - 1
        return i >= 0 and pos < self.spans[i][1]


class Tag:
    __slots__ = ("start", "end", "raw", "name", "attrs")

    def __init__(self, start, end, raw, name, attrs):
        self.start, self.end, self.raw, self.name, self.attrs = start, end, raw, name, attrs


def parse_attrs(raw: str) -> dict:
    body = raw[1:-1].rstrip("/") if raw.endswith(">") else raw[1:]
    parts = body.split(None, 1)
    rest = parts[1] if len(parts) > 1 else ""
    attrs: dict = {}
    for m in _ATTR_RE.finditer(rest):
        k = m.group(1).lower()
        if k in attrs:
            continue
        v = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else (m.group(4) or ""))
        attrs[k] = _html.unescape(v)
    return attrs


def iter_tags(html: str, name: str, mask: Mask):
    rx = _TAG_RE_CACHE.get(name)
    if rx is None:
        rx = _TAG_RE_CACHE[name] = re.compile(r"<%s\b%s>" % (name, _ATTR), re.I)
    for m in rx.finditer(html):
        if m.start() in mask:
            continue
        raw = m.group(0)
        yield Tag(m.start(), m.end(), raw, name, parse_attrs(raw))


_HEAD_CLOSE = re.compile(r"</head\s*>", re.I)
_TITLE_CLOSE = re.compile(r"</title\s*>", re.I)


def head_bounds(html: str, mask: Mask):
    for t in iter_tags(html, "head", mask):
        m = _HEAD_CLOSE.search(html, t.end)
        return (t.end, m.start() if m else len(html))
    return None


def _head_insert_pos(html: str, mask: Mask, hb) -> int:
    lo, hi = hb
    for t in iter_tags(html, "meta", mask):
        if lo <= t.start < hi and ("charset" in t.attrs or
                                   t.attrs.get("http-equiv", "").lower() == "content-type"):
            return t.end
    return lo


def _indent(html: str, hb) -> str:
    m = re.search(r"\n([ \t]+)<", html[hb[0]:hb[0] + 800])      # first indented child of <head>
    return m.group(1) if m else "    "


def has_html_structure(html: str) -> bool:
    return bool(re.search(r"<html\b", html, re.I) or re.search(r"<head\b", html, re.I))


# ─────────────────────────────────────────────────────────────────────────────
# Reading existing page data
# ─────────────────────────────────────────────────────────────────────────────
def read_title(html: str) -> str:
    mask = Mask(html)
    hb = head_bounds(html, mask)
    lo, hi = hb if hb else (0, len(html))
    for t in iter_tags(html, "title", mask):
        if lo <= t.start < hi:
            m = _TITLE_CLOSE.search(html, t.end)
            if m:
                return strip_tags(html[t.end:m.start()])
    return ""


def read_meta(html: str, key: str) -> str:
    mask = Mask(html)
    key = key.lower()
    for t in iter_tags(html, "meta", mask):
        if t.attrs.get("name", "").lower() == key or t.attrs.get("property", "").lower() == key:
            return clean_text(t.attrs.get("content", ""))
    return ""


def get_canonical(html: str, site_url: str = "") -> str:
    mask = Mask(html)
    for t in iter_tags(html, "link", mask):
        if "canonical" in t.attrs.get("rel", "").lower().split():
            href = t.attrs.get("href", "").strip()
            if not href:
                continue
            if site_url and not re.match(r"^https?://", href, re.I):
                return urllib.parse.urljoin(site_url.rstrip("/") + "/", href)
            return href
    return ""


def get_base_href(html: str) -> str:
    mask = Mask(html)
    for t in iter_tags(html, "base", mask):
        if t.attrs.get("href"):
            return t.attrs["href"]
    return ""


def page_name_from_path(fp, root) -> str:
    p = Path(fp)
    stem = p.stem
    if stem.lower() in ("index", "default", "home"):
        try:
            rel = p.relative_to(root)
            parent = rel.parts[-2] if len(rel.parts) > 1 else ""
        except Exception:
            parent = ""
        if not parent:
            return "Home"
        return parent.replace("-", " ").replace("_", " ").title()
    return stem.replace("-", " ").replace("_", " ").title()


def folder_context_from_path(fp, root) -> str:
    try:
        parts = list(Path(fp).relative_to(root).parts[:-1])
        return " > ".join(p.replace("-", " ").replace("_", " ").title() for p in parts)
    except Exception:
        return ""


def page_context(page_name: str, folder_ctx: str, page_url: str, h1: str) -> str:
    slug = re.sub(r"https?://[^/]+", "", page_url or "")
    return f"{page_name} {folder_ctx} {slug} {h1}"


def context_tokens(ctx: str) -> frozenset:
    return frozenset(w for w in re.split(r"[^a-z0-9]+", (ctx or "").lower()) if len(w) > 1)


def find_published_date(html: str) -> str:
    v = read_meta(html, "article:published_time")
    if v:
        return v
    mask = Mask(html)
    for t in iter_tags(html, "time", mask):
        if t.attrs.get("datetime"):
            return t.attrs["datetime"]
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Page text extraction (content only — no nav / header / footer boilerplate)
# ─────────────────────────────────────────────────────────────────────────────
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
         "param", "source", "track", "wbr"}
_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "nav", "header", "footer",
              "aside", "form", "select", "button", "iframe", "head", "title"}
_SKIP_CLASS = re.compile(r"\b(nav|navbar|navigation|menu|footer|cookie|cookies|breadcrumb|"
                         r"breadcrumbs|sidebar|popup|modal|social)\b", re.I)


class _ContentParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []             # (tag, skip, main)
        self.skip_depth = 0
        self.main_depth = 0
        self.all_parts: list = []
        self.main_parts: list = []
        self.h1s: list = []
        self.heads: list = []
        self._cur_head = None       # (tag, parts)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in _VOID:
            return
        cls = f"{a.get('class') or ''} {a.get('id') or ''}"
        skip = tag in _SKIP_TAGS or bool(_SKIP_CLASS.search(cls))
        main = tag in ("main", "article") or (a.get("role") or "").lower() == "main"
        self.stack.append((tag, skip, main))
        if skip:
            self.skip_depth += 1
        if main:
            self.main_depth += 1
        if tag in ("h1", "h2", "h3"):
            self._cur_head = (tag, [])

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        idx = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                idx = i
                break
        if idx is None:
            return
        while len(self.stack) > idx:
            t, skip, main = self.stack.pop()
            if skip:
                self.skip_depth -= 1
            if main:
                self.main_depth -= 1
        if self._cur_head and self._cur_head[0] == tag:
            txt = clean_text(" ".join(self._cur_head[1]))
            if txt:
                (self.h1s if tag == "h1" else self.heads).append(txt)
            self._cur_head = None

    def handle_data(self, data):
        if self._cur_head is not None:
            self._cur_head[1].append(data)
        if self.skip_depth:
            return
        self.all_parts.append(data)
        if self.main_depth:
            self.main_parts.append(data)


def extract_content(html: str, max_chars: int = 20000) -> dict:
    """Return {'text', 'h1s', 'headings'} using only real page content."""
    p = _ContentParser()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    main = clean_text(" ".join(p.main_parts))
    allt = clean_text(" ".join(p.all_parts))
    text = main if len(main) >= 250 else allt
    return {"text": text[:max_chars], "h1s": p.h1s, "headings": p.heads[:10]}


# ─────────────────────────────────────────────────────────────────────────────
# Safe tag edits — return (html, changed)
# ─────────────────────────────────────────────────────────────────────────────
def set_title(html: str, text: str):
    mask = Mask(html)
    hb = head_bounds(html, mask)
    lo, hi = hb if hb else (0, len(html))
    found = []
    for t in iter_tags(html, "title", mask):
        if lo <= t.start < hi:
            m = _TITLE_CLOSE.search(html, t.end)
            if m:
                found.append((t, m))
    inner = esc_text(text)
    if found:
        out = html
        for t, m in reversed(found[1:]):          # drop duplicate <title> tags
            out = out[:t.start] + out[m.end():]
        t, m = found[0]
        out = out[:t.end] + inner + out[m.start():]
        return out, out != html
    if not hb:
        return html, False
    pos = _head_insert_pos(html, mask, hb)
    out = html[:pos] + "\n" + _indent(html, hb) + f"<title>{inner}</title>" + html[pos:]
    return out, True


def upsert_metas(html: str, items: list):
    """items: dicts {key, content, attr('name'|'property'), only_if_missing}.
    Updates existing tags in place, removes duplicates, inserts the rest as one block."""
    mask = Mask(html)
    hb = head_bounds(html, mask)
    lo, hi = hb if hb else (0, len(html))
    metas = [t for t in iter_tags(html, "meta", mask) if lo <= t.start < hi]
    edits, missing = [], []
    for it in items:
        key = it["key"].lower()
        attr = it.get("attr", "name")
        content = it["content"]
        matches = [t for t in metas
                   if t.attrs.get("name", "").lower() == key
                   or t.attrs.get("property", "").lower() == key]
        newtag = f'<meta {attr}="{it["key"]}" content="{esc_attr(content)}">'
        if matches:
            if it.get("only_if_missing"):
                continue
            first = matches[0]
            if clean_text(first.attrs.get("content", "")) != clean_text(content):
                edits.append((first.start, first.end, newtag))
            for extra in matches[1:]:
                edits.append((extra.start, extra.end, ""))
        else:
            missing.append(newtag)
    out = html
    for s, e, rep in sorted(edits, reverse=True):
        out = out[:s] + rep + out[e:]
    if missing and hb:
        mask2 = Mask(out)
        hb2 = head_bounds(out, mask2)
        pos = None
        # tidy order: title → description → robots → og/twitter. New tags go after the last
        # SEO meta tag already present, otherwise right after </title>.
        for t in iter_tags(out, "meta", mask2):
            k = (t.attrs.get("name") or t.attrs.get("property") or "").lower()
            if hb2[0] <= t.start < hb2[1] and (k in ("description", "robots") or
                                               k.startswith(("og:", "twitter:"))):
                pos = t.end
        if pos is None:
            for t in iter_tags(out, "title", mask2):
                if hb2[0] <= t.start < hb2[1]:
                    m = _TITLE_CLOSE.search(out, t.end)
                    if m:
                        pos = m.end()
                    break
        if pos is None:
            pos = _head_insert_pos(out, mask2, hb2)
        ind = _indent(out, hb2)
        out = out[:pos] + "".join("\n" + ind + tg for tg in missing) + out[pos:]
    return out, out != html


def remove_metas(html: str, key: str):
    mask = Mask(html)
    key = key.lower()
    spans = [(t.start, t.end) for t in iter_tags(html, "meta", mask)
             if t.attrs.get("name", "").lower() == key]
    out = html
    for s, e in sorted(spans, reverse=True):
        # also swallow the indentation and newline in front of the tag
        ls = out.rfind("\n", 0, s)
        if ls != -1 and not out[ls + 1:s].strip():
            s = ls
        out = out[:s] + out[e:]
    return out, len(spans)


_H1_CLOSE = re.compile(r"</h1\s*>", re.I)


def find_h1s(html: str, mask: Mask):
    res = []
    for t in iter_tags(html, "h1", mask):
        m = _H1_CLOSE.search(html, t.end)
        if m:
            res.append((t, t.end, m.start()))
    return res


def set_h1(html: str, text: str):
    """Returns (html, changed, issue). Issue ∈ {'', 'missing', 'multiple', 'no_text'}.
    Only the text is replaced — nested markup (span, a, i, br …) is kept."""
    mask = Mask(html)
    hs = find_h1s(html, mask)
    if not hs:
        return html, False, "missing"
    if len(hs) > 1:
        return html, False, "multiple"
    _, i0, i1 = hs[0]
    inner = html[i0:i1]
    if strip_tags(inner) == clean_text(text):
        return html, False, ""
    parts = re.split(r"(<[^>]*>)", inner)
    textidx = [k for k in range(0, len(parts), 2) if clean_text(parts[k])]
    if not textidx:
        if len(parts) == 1:
            new_inner = esc_text(text)
        else:
            return html, False, "no_text"
    else:
        best = max(textidx, key=lambda k: len(clean_text(parts[k])))
        lead = parts[best][:len(parts[best]) - len(parts[best].lstrip())]
        trail = parts[best][len(parts[best].rstrip()):]
        parts[best] = lead + esc_text(text) + trail
        for k in textidx:
            if k != best:
                parts[k] = " " if parts[k].strip() != parts[k] else ""
        new_inner = "".join(parts)
    out = html[:i0] + new_inner + html[i1:]
    return out, out != html, ""


# ─────────────────────────────────────────────────────────────────────────────
# Images — resolution, size, alt text, lazy loading
# ─────────────────────────────────────────────────────────────────────────────
def image_size(path):
    """(width, height) read from the file header, or None. No Pillow needed."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                return struct.unpack(">II", head[16:24])
            if head[:6] in (b"GIF87a", b"GIF89a"):
                return struct.unpack("<HH", head[6:10])
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                kind = head[12:16]
                if kind == b"VP8X":
                    return (int.from_bytes(head[24:27], "little") + 1,
                            int.from_bytes(head[27:30], "little") + 1)
                if kind == b"VP8 ":
                    w, h = struct.unpack("<HH", head[26:30])
                    return w & 0x3FFF, h & 0x3FFF
                if kind == b"VP8L":
                    b = int.from_bytes(head[21:25], "little")
                    return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
                return None
            if head[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    b = f.read(1)
                    while b and b != b"\xff":
                        b = f.read(1)
                    while b == b"\xff":
                        b = f.read(1)
                    if not b:
                        return None
                    marker = b[0]
                    if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                        f.read(3)
                        h, w = struct.unpack(">HH", f.read(4))
                        return w, h
                    if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                        continue
                    seg = f.read(2)
                    if len(seg) < 2:
                        return None
                    f.seek(struct.unpack(">H", seg)[0] - 2, 1)
    except Exception:
        return None
    return None


def resolve_image(src: str, page_path, root, site_url: str, base_href: str = ""):
    """Resolve an <img>/og:image reference against the PAGE's location.

    Returns {'url', 'local', 'exists'} or None. 'local' is the file on disk when
    the image belongs to this site. Relative paths such as ../images/a.jpg are
    resolved from the page's folder (or <base href>), not from the site root."""
    src = (src or "").strip()
    if not src or src.startswith(("data:", "javascript:", "#")):
        return None
    root = Path(root)
    site = (site_url or "").rstrip("/")
    if src.startswith("//"):
        src = ("https:" if not site.startswith("http:") else "http:") + src
    if re.match(r"^https?://", src, re.I):
        if site and src.lower().startswith(site.lower() + "/"):
            clean = urllib.parse.unquote(src[len(site):].split("#")[0].split("?")[0]).lstrip("/")
            local = root / clean
            return {"url": src, "local": str(local) if local.exists() else None,
                    "exists": local.exists()}
        return {"url": src, "local": None, "exists": True}      # external — trust it
    clean = urllib.parse.unquote(src.split("#")[0].split("?")[0])
    if clean.startswith("/"):
        local = root / clean.lstrip("/")
    else:
        base_dir = Path(page_path).parent
        if base_href and not re.match(r"^https?://", base_href, re.I):
            base_dir = (root / base_href.lstrip("/")) if base_href.startswith("/") \
                else (Path(page_path).parent / base_href)
            if not base_href.endswith("/"):
                base_dir = base_dir.parent
        local = Path(os.path.normpath(base_dir / clean))
    try:
        rel = Path(os.path.normpath(local)).relative_to(root)
    except Exception:
        return None                                              # points outside the site
    exists = Path(local).exists()
    url = (site + "/" + rel.as_posix()) if site else ""
    if not url:
        return None
    return {"url": url, "local": str(local) if exists else None, "exists": exists}


_NOT_CONTENT_IMG = re.compile(
    r"logo|icon|sprite|avatar|favicon|pixel|spacer|blank|badge|button|arrow|bullet|"
    r"divider|separator|tracking|1x1|placeholder|loader|spinner|payment|seal|flag", re.I)
_RASTER_EXT = (".jpg", ".jpeg", ".png", ".webp")


def _chrome_spans(html: str, mask: Mask):
    rx = re.compile(r"<(header|nav|footer)\b%s>.*?</\1\s*>" % _ATTR, re.I | re.S)
    return [(m.start(), m.end()) for m in rx.finditer(html) if m.start() not in mask]


def _in_spans(spans, pos):
    return any(s <= pos < e for s, e in spans)


def pick_page_image(html: str, page_path, root, site_url: str, default_image: str = ""):
    """Choose the best share image for a page.
    Order: the page's own og:image → first real content image → user default."""
    base = get_base_href(html)
    mask = Mask(html)

    def pack(res, source):
        w = h = None
        if res.get("local"):
            sz = image_size(res["local"])
            if sz:
                w, h = sz
        return {"url": res["url"], "width": w, "height": h, "source": source}

    og = read_meta(html, "og:image")
    if og:
        r = resolve_image(og, page_path, root, site_url, base)
        if r and r["exists"] and r["url"]:
            return pack(r, "page og:image")

    chrome = _chrome_spans(html, mask)
    best = None
    for t in iter_tags(html, "img", mask):
        if _in_spans(chrome, t.start):
            continue
        src = t.attrs.get("src") or t.attrs.get("data-src") or ""
        low = src.lower().split("?")[0]
        if not low.endswith(_RASTER_EXT):
            continue
        if _NOT_CONTENT_IMG.search(low) or _NOT_CONTENT_IMG.search(
                t.attrs.get("class", "") + " " + t.attrs.get("id", "")):
            continue
        r = resolve_image(src, page_path, root, site_url, base)
        if not r or not r["local"]:
            continue
        sz = image_size(r["local"])
        if not sz or sz[0] < 300 or sz[1] < 160:
            continue
        cand = {"url": r["url"], "width": sz[0], "height": sz[1], "source": "page content image"}
        if sz[0] >= 600:
            return cand
        if best is None or sz[0] > best["width"]:
            best = cand
    if best:
        return best

    if default_image:
        r = resolve_image(default_image, root / "index.html", root, site_url)
        if r and r["url"]:
            return pack(r, "default image")
        if re.match(r"^https?://", default_image, re.I):
            return {"url": default_image, "width": None, "height": None, "source": "default image"}
    return None


_GENERIC_NAME_TOKENS = {
    "img", "image", "images", "dsc", "dscn", "dcim", "pic", "screenshot",
    "untitled", "banner", "slide", "slider", "hero", "bg", "background", "thumb", "thumbnail",
    "copy", "scaled", "min", "resized", "large", "small", "medium", "web", "final", "new", "edited",
    "shutterstock", "istock", "pexels", "unsplash", "adobestock", "gettyimages", "depositphotos",
    "jpg", "jpeg", "png", "webp", "gif", "wa", "mvimg", "pxl"}


def alt_from_filename(src: str):
    """A readable alt from the file name, or None if the name is meaningless."""
    stem = Path(urllib.parse.unquote((src or "").split("?")[0])).stem
    stem = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem)
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", stem) if t]
    words = []
    for t in tokens:
        tl = t.lower()
        if re.fullmatch(r"\d+", tl) or re.fullmatch(r"\d+x\d+", tl) or re.fullmatch(r"[0-9a-f]{6,}", tl):
            continue
        if tl in _GENERIC_NAME_TOKENS or len(tl) < 3 or re.fullmatch(r"[a-z]\d+", tl):
            continue
        if not re.search(r"[aeiouy]", tl):                    # consonant soup / hash
            continue
        words.append(tl)
    if len(words) < 2:
        return None
    text = " ".join(words)
    return text[:1].upper() + text[1:]


_DECORATIVE = re.compile(r"spacer|pixel|blank|sprite|bullet|divider|separator|arrow|tracking|1x1|"
                         r"icon|ico-|-ico|chevron|caret|star-?rating", re.I)


class Img:
    __slots__ = ("start", "end", "raw", "attrs", "index", "in_chrome", "svg",
                 "candidate", "context", "decorative", "first_content")


def scan_images(html: str) -> list:
    """Analyse every <img> (ignoring comments/scripts/svg/noscript/templates)."""
    mask = Mask(html)
    chrome = _chrome_spans(html, mask)
    # nearest-heading lookup
    heads = []
    for name in ("h1", "h2", "h3"):
        for t in iter_tags(html, name, mask):
            m = re.compile(r"</%s\s*>" % name, re.I).search(html, t.end)
            if m:
                txt = strip_tags(html[t.end:m.start()])
                if txt:
                    heads.append((t.start, txt))
    heads.sort()
    head_pos = [h[0] for h in heads]
    # figure captions
    figs = []
    for t in iter_tags(html, "figure", mask):
        m = re.compile(r"</figure\s*>", re.I).search(html, t.end)
        if m:
            cap = re.search(r"<figcaption\b[^>]*>(.*?)</figcaption\s*>", html[t.end:m.start()], re.I | re.S)
            figs.append((t.start, m.end(), strip_tags(cap.group(1)) if cap else ""))
    imgs, first_done = [], False
    for t in iter_tags(html, "img", mask):
        im = Img()
        im.start, im.end, im.raw, im.attrs = t.start, t.end, t.raw, t.attrs
        im.index = len(imgs)
        src = (t.attrs.get("src") or t.attrs.get("data-src") or "").lower()
        im.svg = src.split("?")[0].endswith(".svg") or src.startswith("data:image/svg")
        im.in_chrome = _in_spans(chrome, t.start)
        im.candidate = alt_from_filename(t.attrs.get("src") or t.attrs.get("data-src") or "")
        sig = f"{t.attrs.get('class', '')} {t.attrs.get('id', '')} {src.split('/')[-1]}"
        w, h = t.attrs.get("width", ""), t.attrs.get("height", "")
        tiny = w.isdigit() and h.isdigit() and (int(w) <= 24 or int(h) <= 24)
        im.decorative = (bool(_DECORATIVE.search(sig)) or tiny
                         or t.attrs.get("role", "").lower() in ("presentation", "none")
                         or t.attrs.get("aria-hidden", "").lower() == "true")
        ctx = ""
        for fs, fe, cap in figs:
            if fs <= t.start < fe and cap:
                ctx = cap
        if not ctx:
            ctx = clean_text(t.attrs.get("title", ""))
        if not ctx and head_pos:
            i = bisect_right(head_pos, t.start) - 1
            if i >= 0:
                ctx = heads[i][1]
        im.context = ctx
        im.first_content = False
        if not im.in_chrome and not im.svg and not first_done:
            im.first_content = True
            first_done = True
        imgs.append(im)
    return imgs


def img_needs_alt(im: Img) -> bool:
    return "alt" not in im.attrs and not im.svg


def img_lazy_ok(im: Img) -> bool:
    if im.svg or "loading" in im.attrs or im.in_chrome or im.first_content:
        return False
    if "fetchpriority" in im.attrs or "data-src" in im.attrs or "data-lazy" in im.attrs:
        return False
    sig = f"{im.attrs.get('class', '')} {im.attrs.get('id', '')} {im.attrs.get('src', '')}".lower()
    if "lazy" in sig or "logo" in sig:
        return False
    return True


def cap_alt(text: str, limit: int = 125) -> str:
    text = clean_text(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > limit * 0.5 else cut).rstrip(" ,.;:-")


def apply_images(html: str, imgs: list, alt_map: dict, do_lazy: bool, do_alt: bool):
    """Returns (html, lazy_added, alts_added). alt_map: {img.index: alt text}.
    SVG images are never modified."""
    out = html
    lazy_n = alt_n = 0
    for im in sorted(imgs, key=lambda i: i.start, reverse=True):
        if im.svg:
            continue
        add = []
        if do_alt and img_needs_alt(im) and im.index in alt_map:
            add.append(f'alt="{esc_attr(cap_alt(alt_map[im.index]))}"')
            alt_n += 1
        if do_lazy and img_lazy_ok(im):
            add.append('loading="lazy"')
            lazy_n += 1
        if not add:
            continue
        raw = im.raw.rstrip()
        if raw.endswith("/>"):
            body = raw[:-2].rstrip()
            tail = " />" if raw[-3:-2] == " " else "/>"
        else:
            body, tail = raw[:-1].rstrip(), ">"
        out = out[:im.start] + body + " " + " ".join(add) + tail + out[im.end:]
    return out, lazy_n, alt_n


# ─────────────────────────────────────────────────────────────────────────────
# JSON-LD — read, merge, write
# ─────────────────────────────────────────────────────────────────────────────
_LD_RE = re.compile(r"(<script\b%s>)(.*?)(</script\s*>)" % _ATTR, re.I | re.S)

_ORG_TYPES = {"Organization", "LocalBusiness", "Corporation", "ProfessionalService", "Store",
              "Restaurant", "Plumber", "Electrician", "Locksmith", "RoofingContractor",
              "GeneralContractor", "HVACBusiness", "MovingCompany", "AutoRepair", "Dentist",
              "LegalService", "Attorney", "RealEstateAgent", "HomeAndConstructionBusiness",
              "MedicalBusiness", "FinancialService", "InsuranceAgency", "HousePainter"}
_PAGE_TYPES = {"WebPage", "AboutPage", "ContactPage", "CollectionPage", "ItemPage", "FAQPage",
               "QAPage", "ProfilePage", "CheckoutPage", "SearchResultsPage", "MedicalWebPage"}


def _types(node) -> list:
    t = node.get("@type") if isinstance(node, dict) else None
    if t is None:
        return []
    return [str(x) for x in t] if isinstance(t, list) else [str(t)]


def _family(t: str) -> str:
    if t in _ORG_TYPES or "Business" in t:
        return "org"
    if t in _PAGE_TYPES:
        return "page"
    return t


def _overlap(a: list, b: list) -> bool:
    return bool({_family(x) for x in a} & {_family(x) for x in b})


class LdBlock:
    __slots__ = ("start", "end", "open_tag", "close_tag", "body", "data", "hints")


def parse_ld_blocks(html: str) -> list:
    cm = Mask(html, comments_only=True)
    blocks = []
    for m in _LD_RE.finditer(html):
        if m.start() in cm:
            continue
        attrs = parse_attrs(m.group(1))
        if "ld+json" not in attrs.get("type", "").lower():
            continue
        b = LdBlock()
        b.start, b.end = m.start(), m.end()
        b.open_tag, b.body, b.close_tag = m.group(1), m.group(2), m.group(3)
        try:
            b.data = json.loads(b.body.strip())
        except Exception:
            b.data = None
        b.hints = re.findall(r'"@type"\s*:\s*"([^"]+)"', b.body)
        blocks.append(b)
    return blocks


def _iter_nodes(data):
    if isinstance(data, list):
        for x in data:
            yield from _iter_nodes(x)
    elif isinstance(data, dict):
        yield data
        g = data.get("@graph")
        if isinstance(g, list):
            for x in g:
                yield from _iter_nodes(x)


def _empty(v) -> bool:
    return v in (None, "", [], {})


def merge_node(existing: dict, gen: dict, override_keys=("description", "url")):
    """Existing values win; generated values only fill gaps — except the description and the page URL, which
    follow the new meta description and the page's canonical URL. Returns existing."""
    for k, v in gen.items():
        if k in ("@context", "@type"):
            continue
        if k in override_keys and not _empty(v):
            existing[k] = v
        elif k not in existing or _empty(existing[k]):
            existing[k] = v
        elif isinstance(existing[k], dict) and isinstance(v, dict):
            merge_node(existing[k], v, override_keys=())
    return existing


PAGE_SCHEMA_TYPES = {"WebPage", "Service", "Article", "BlogPosting", "NewsArticle", "AboutPage", "ContactPage",
                     "CollectionPage", "ItemPage", "FAQPage", "Product", "ProfilePage", "SearchResultsPage"}


def align_schema_urls(html: str, canonical: str):
    """Point the page-level schema `url` at the canonical (same website only; business / organisation nodes keep
    their own url). Needed once a canonical tag is added: a schema url that names another form of the page would
    then disagree with it. Returns (html, number_of_nodes_changed)."""
    if not canonical:
        return html, 0
    key = lambda u: re.sub(r"^www\.", "", urllib.parse.urlsplit(u).netloc.lower())
    edits, n = [], 0
    for b in parse_ld_blocks(html):
        if b.data is None:
            continue
        changed = False
        for node in _iter_nodes(b.data):
            ts = set(_types(node))
            url = node.get("url")
            if (ts and ts <= PAGE_SCHEMA_TYPES and isinstance(url, str) and url.strip()
                    and url.rstrip("/") != canonical.rstrip("/")
                    and re.match(r"^https?://", url, re.I) and key(url) == key(canonical)):
                node["url"] = canonical
                changed = True
                n += 1
        if changed:
            edits.append((b.start, b.end, f"{b.open_tag}\n{dump_ld(b.data)}\n{b.close_tag}"))
    for st, en, rep in sorted(edits, reverse=True):
        html = html[:st] + rep + html[en:]
    return html, n


def dump_ld(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False).replace("</", "<\\/")


def apply_schema(html: str, gen: dict):
    """Merge `gen` into the page's JSON-LD. Returns (html, action) where action ∈
    'added' | 'merged' | 'unchanged' | 'skipped_unparseable' | 'no_head'."""
    blocks = parse_ld_blocks(html)
    gtypes = _types(gen)
    target = None
    for b in blocks:
        if b.data is None:
            if _overlap(b.hints, gtypes):
                return html, "skipped_unparseable"
            continue
        for n in _iter_nodes(b.data):
            if _overlap(_types(n), gtypes):
                target = (b, n)
                break
        if target:
            break
    if target:
        b, node = target
        before = json.dumps(b.data, sort_keys=True)
        merge_node(node, gen)
        if json.dumps(b.data, sort_keys=True) == before:
            return html, "unchanged"
        rep = f"{b.open_tag}\n{dump_ld(b.data)}\n{b.close_tag}"
        return html[:b.start] + rep + html[b.end:], "merged"
    block = f'<script type="application/ld+json">\n{dump_ld(gen)}\n</script>'
    if blocks:
        last = blocks[-1]
        return html[:last.end] + "\n" + block + html[last.end:], "added"
    mask = Mask(html)
    hb = head_bounds(html, mask)
    if not hb:
        return html, "no_head"
    ind = _indent(html, hb)
    pos = hb[1]
    ls = html.rfind("\n", 0, pos)
    if ls != -1 and not html[ls + 1:pos].strip():
        pos = ls + 1
        return html[:pos] + ind + block.replace("\n", "\n" + ind) + "\n" + html[pos:], "added"
    return html[:pos] + "\n" + ind + block + "\n" + html[pos:], "added"


# Fields the AI may NOT invent — they come only from the verified business details.
FORBIDDEN_AI_SCHEMA_KEYS = {"telephone", "address", "openingHours", "openingHoursSpecification",
                            "priceRange", "sameAs", "geo", "email", "logo", "image", "url",
                            "aggregateRating", "review", "hasMap", "founder", "foundingDate"}
AI_SCHEMA_TYPES = ("WebPage", "Service", "LocalBusiness", "Article", "AboutPage",
                   "ContactPage", "CollectionPage")


def _org_node(facts: dict, site_url: str, with_contact: bool = True) -> dict:
    org_type = facts.get("business_type") or "LocalBusiness"
    node = {"@type": org_type, "name": facts.get("name", "")}
    if site_url:
        node["url"] = site_url
    if with_contact:
        if facts.get("phone"):
            node["telephone"] = facts["phone"]
        addr = {k: facts.get(v) for k, v in (
            ("streetAddress", "street"), ("addressLocality", "city"),
            ("addressRegion", "region"), ("postalCode", "postal"),
            ("addressCountry", "country")) if facts.get(v)}
        if addr:
            node["address"] = {"@type": "PostalAddress", **addr}
        if facts.get("hours"):
            node["openingHours"] = list(facts["hours"])
        if facts.get("price_range"):
            node["priceRange"] = facts["price_range"]
    if facts.get("same_as"):
        node["sameAs"] = list(facts["same_as"])
    if facts.get("logo"):
        node["logo"] = facts["logo"]
    return node


def build_schema(ai: dict, facts: dict, page_url: str, site_url: str, image_url: str,
                 meta_desc: str, h1: str, title: str, published: str = ""):
    """Build the JSON-LD node for a page. Returns (node, type_used, issues).

    The AI chooses the type and wording; contact facts come only from `facts`."""
    issues = []
    stype = (ai.get("schema_type") or "WebPage").strip()
    if stype not in AI_SCHEMA_TYPES:
        stype = "WebPage"
    brand = facts.get("name", "")
    has_addr = bool(facts.get("street") or facts.get("city"))

    if stype == "LocalBusiness":
        if brand and has_addr:
            node = _org_node(facts, page_url or site_url)
            node["@context"] = "https://schema.org"
            node["description"] = meta_desc
            if ai.get("area_served"):
                node["areaServed"] = ai["area_served"]
            if image_url:
                node["image"] = image_url
            return _order(node), node["@type"], issues
        issues.append("LocalBusiness needs verified business name + address — used WebPage")
        stype = "WebPage"

    if stype == "Service":
        node = {"@context": "https://schema.org", "@type": "Service",
                "name": ai.get("name") or h1 or title, "description": meta_desc}
        if page_url:
            node["url"] = page_url
        if ai.get("service_type"):
            node["serviceType"] = ai["service_type"]
        if ai.get("area_served"):
            node["areaServed"] = ai["area_served"]
        if brand:
            node["provider"] = _org_node(facts, site_url, with_contact=True)
        else:
            issues.append("Service schema has no provider (business name missing)")
        if image_url:
            node["image"] = image_url
        return _order(node), "Service", issues

    if stype == "Article":
        if image_url and published and brand:
            node = {"@context": "https://schema.org", "@type": "Article",
                    "headline": (h1 or title)[:110], "description": meta_desc,
                    "image": image_url, "datePublished": published,
                    "author": {"@type": "Organization", "name": brand},
                    "publisher": {"@type": "Organization", "name": brand}}
            if facts.get("logo"):
                node["publisher"]["logo"] = {"@type": "ImageObject", "url": facts["logo"]}
            if page_url:
                node["mainEntityOfPage"] = page_url
            return _order(node), "Article", issues
        issues.append("Article schema needs image + publish date — used WebPage")
        stype = "WebPage"

    page_type = stype if stype in ("AboutPage", "ContactPage", "CollectionPage") else "WebPage"
    node = {"@context": "https://schema.org", "@type": page_type,
            "name": ai.get("name") or h1 or title, "description": meta_desc, "inLanguage": "en"}
    if page_url:
        node["url"] = page_url
    if site_url and brand:
        node["isPartOf"] = {"@type": "WebSite", "name": brand, "url": site_url}
    if image_url:
        node["primaryImageOfPage"] = {"@type": "ImageObject", "url": image_url}
    return _order(node), page_type, issues


def _order(node: dict) -> dict:
    first = [k for k in ("@context", "@type", "name", "headline", "description", "url") if k in node]
    return {**{k: node[k] for k in first}, **{k: v for k, v in node.items() if k not in first}}


# ─────────────────────────────────────────────────────────────────────────────
# Skip rules / exclusions
# ─────────────────────────────────────────────────────────────────────────────
def rel_posix(path, root) -> str:
    try:
        return Path(path).relative_to(root).as_posix()
    except Exception:
        return Path(path).as_posix()


def matches_patterns(rel: str, patterns) -> bool:
    base = rel.rsplit("/", 1)[-1]
    for p in patterns:
        p = p.strip().replace("\\", "/")
        if not p or p.startswith("#"):
            continue
        if p.endswith("/"):
            if rel.startswith(p) or ("/" + p) in ("/" + rel):
                return True
        elif (fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(base, p) or rel.startswith(p + "/")):
            return True
    return False


def classify_skip(html: str, rel: str, skip_partial=True, skip_noindex=True,
                  skip_redirect=True, skip_error=True) -> str:
    """Reason a file should be left untouched, or ''."""
    mask = Mask(html)
    if skip_partial and not has_html_structure(html):
        return "partial / include (no <html> or <head>)"
    hb = head_bounds(html, mask)
    lo, hi = hb if hb else (0, len(html))
    for t in iter_tags(html, "meta", mask):
        if not (lo <= t.start < hi):
            continue
        name = t.attrs.get("name", "").lower()
        content = t.attrs.get("content", "").lower()
        if skip_noindex and name in ("robots", "googlebot") and "noindex" in content:
            return "noindex page"
        if skip_redirect and t.attrs.get("http-equiv", "").lower() == "refresh" and "url" in content:
            return "meta-refresh redirect"
    if skip_error:
        if re.search(r"(^|[/_\-])(404|403|500|error|not-?found)(\.|/|$|[_\-])", rel, re.I):
            return "404 / error page"
        if re.search(r"\b(404|page not found|not found)\b", read_title(html), re.I):
            return "404 / error page"
    return ""


REPORT_PREFIX = "seo_report_"          # reports saved into the scanned folder are never pages to optimise


def collect_html_files(folder, recursive=True) -> list:
    base = Path(folder)
    pats = ["**/*.html", "**/*.htm"] if recursive else ["*.html", "*.htm"]
    files = set()
    for pat in pats:
        files.update(str(f) for f in base.glob(pat)
                     if f.is_file() and not f.name.lower().startswith(REPORT_PREFIX))
    return sorted(files, key=lambda f: (os.path.dirname(f).lower(), os.path.basename(f).lower()))


# ─────────────────────────────────────────────────────────────────────────────
# Uniqueness registry (titles / descriptions / H1) with nearest-neighbour lookup
# ─────────────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=30000)
def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def find_similar(text: str, seen, threshold: float):
    a = _norm(text)
    if not a:
        return None
    sm = difflib.SequenceMatcher(None, autojunk=False)
    sm.set_seq2(a)
    for other in seen:
        b = _norm(other)
        if a == b:
            return other
        sm.set_seq1(b)
        if (sm.real_quick_ratio() >= threshold and sm.quick_ratio() >= threshold
                and sm.ratio() >= threshold):
            return other
    return None


class Registry:
    """Everything generated so far in (or resumed into) this run."""

    def __init__(self):
        self.titles, self.cores, self.descs, self.h1s = [], [], [], []
        self.tokens, self.ctxs = [], []

    def add(self, title_full, title_core, desc, h1, ctx: str):
        self.titles.append(title_full)
        self.cores.append(title_core)
        self.descs.append(desc)
        self.h1s.append(h1 or "")
        self.ctxs.append(ctx)
        self.tokens.append(context_tokens(ctx))

    def __len__(self):
        return len(self.titles)

    def title_dup(self, core):
        return find_similar(core, self.cores, TITLE_SIMILARITY_MAX)

    def desc_dup(self, desc):
        return find_similar(desc, self.descs, DESC_SIMILARITY_MAX)

    def h1_dup(self, h1):
        return find_similar(h1, [h for h in self.h1s if h], H1_SIMILARITY_MAX) if h1 else None

    def nearest(self, token_sets, per_page: int, cap: int) -> list:
        """(title, description, h1) of the earlier pages most similar to the given pages."""
        if not self.titles:
            return []
        idx = []
        for ts in token_sets:
            for i in _nearest_indices(ts, self.tokens, per_page):
                if i not in idx:
                    idx.append(i)
        return [(self.titles[i], self.descs[i], self.h1s[i]) for i in idx[:cap]]


def _nearest_indices(tokens, all_tokens, k):
    if not all_tokens or not tokens:
        return list(range(max(0, len(all_tokens) - k), len(all_tokens)))
    scored = []
    for i, t in enumerate(all_tokens):
        inter = len(tokens & t)
        if inter:
            scored.append((inter / len(tokens | t), i))
    scored.sort(reverse=True)
    picked = [i for _, i in scored[:k]]
    if len(picked) < k:
        have = set(picked)
        for i in range(len(all_tokens) - 1, -1, -1):
            if len(picked) >= k:
                break
            if i not in have:
                picked.append(i)
    return picked


# ─────────────────────────────────────────────────────────────────────────────
# Duplicate-content detector (thin / near-identical pages, e.g. city swaps)
# ─────────────────────────────────────────────────────────────────────────────
class DupDetector:
    K = 48
    THRESH = 0.85

    def __init__(self):
        self.index: dict = {}
        self.sketches: dict = {}

    def _sketch(self, text: str):
        words = re.findall(r"[a-z0-9']+", text.lower())
        if len(words) < 40:
            return None
        hs = {int.from_bytes(hashlib.md5(" ".join(words[i:i + 5]).encode()).digest()[:8], "big")
              for i in range(len(words) - 4)}
        return sorted(hs)[:self.K]

    def check_and_add(self, key: str, text: str):
        """Returns (similar_key, score) if the page is a near-copy, else None."""
        sk = self._sketch(text)
        if not sk:
            return None
        counts: dict = {}
        for h in sk:
            for other in self.index.get(h, ()):
                counts[other] = counts.get(other, 0) + 1
        best = None
        for other, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:5]:
            a, b = set(sk), set(self.sketches[other])
            union = sorted(a | b)[:self.K]
            score = len([h for h in union if h in a and h in b]) / max(len(union), 1)
            if score >= self.THRESH and (best is None or score > best[1]):
                best = (other, score)
        self.sketches[key] = sk
        for h in sk:
            self.index.setdefault(h, []).append(key)
        return best


# ─────────────────────────────────────────────────────────────────────────────
# Backups / resume state
# ─────────────────────────────────────────────────────────────────────────────
def default_backup_dir(root) -> Path:
    """Backups live in the user's own folder — never inside (or next to) any website folder."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    name = re.sub(r"[^\w.-]+", "_", Path(root).name or "site")
    return Path.home() / "TTM_SEO_Backups" / f"{name}_{stamp}"


def backup_file(path, root, backup_dir) -> Path:
    """Copy the ORIGINAL once; later runs never overwrite the first backup."""
    rel = os.path.relpath(path, root)
    dst = Path(backup_dir) / rel
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
    return dst


def restore_backup(backup_dir, root) -> int:
    n = 0
    for src in Path(backup_dir).rglob("*"):
        if src.is_file():
            dst = Path(root) / src.relative_to(backup_dir)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
    return n


STATE_DIR = Path.home() / ".ttm_seo_updater"


class RunState:
    """Per-site progress file: lets a stopped run resume, and re-loads every
    generated title/description so uniqueness holds across sessions."""

    def __init__(self, folder):
        self.folder = str(Path(folder).resolve())
        h = hashlib.sha1(self.folder.lower().encode()).hexdigest()[:12]
        self.path = STATE_DIR / f"state_{h}.json"
        self.data = {"folder": self.folder, "backup_dir": "", "files": {}, "canon": {}}
        self._dirty = 0
        self._last_save = 0.0
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and "files" in loaded:
                    self.data = loaded
            except Exception:
                pass

    @property
    def files(self) -> dict:
        return self.data["files"]

    @property
    def backup_dir(self) -> str:
        return self.data.get("backup_dir", "")

    @backup_dir.setter
    def backup_dir(self, v):
        self.data["backup_dir"] = str(v)

    def counts(self) -> dict:
        c = {}
        for v in self.files.values():
            c[v.get("status", "?")] = c.get(v.get("status", "?"), 0) + 1
        return c

    def record(self, rel: str, **fields):
        self.files[rel] = {**fields, "time": int(time.time())}
        self._dirty += 1
        if self._dirty >= 3 and time.time() - self._last_save >= 4:
            self.save()                              # throttled: the state file grows with scores

    def reset(self):
        self.data = {"folder": self.folder, "backup_dir": "", "files": {}, "canon": {}}
        self.save()

    def save(self):
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = 0
            self._last_save = time.time()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Website fetching (for the "Fetch Details" button)
# ─────────────────────────────────────────────────────────────────────────────
_UA = "Mozilla/5.0 (compatible; TTM-SEO-Updater/4.0)"
_SOCIAL = ("facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
           "youtube.com", "tiktok.com", "pinterest.com", "yelp.com")


def http_get(url: str, timeout: int = 15, max_bytes: int = 2_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(max_bytes)
        final = resp.geturl()
        enc = resp.headers.get_content_charset() or "utf-8"
    try:
        return raw.decode(enc, errors="replace"), final
    except LookupError:
        return raw.decode("utf-8", errors="replace"), final


class _RegionText(HTMLParser):
    """Collect text from header / footer / address / contact-like blocks."""
    _KEY = re.compile(r"contact|address|phone|hours|location|footer|topbar|top-bar", re.I)

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.depth = [], 0
        self.regions: list = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        a = dict(attrs)
        hit = (tag in ("header", "footer", "address")
               or bool(self._KEY.search(f"{a.get('class') or ''} {a.get('id') or ''}")))
        hid = tag in ("script", "style", "noscript", "svg", "template")
        self.stack.append((tag, hit, hid))
        self.depth += hit
        self.hidden += hid

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                while len(self.stack) > i:
                    _, hit, hid = self.stack.pop()
                    self.depth -= hit
                    self.hidden -= hid
                break

    def handle_data(self, d):
        if self.depth and not self.hidden and d.strip():
            self.regions.append(d.strip())


def extract_business_hints(html: str, site_url: str = "") -> dict:
    """Deterministic facts from a page: JSON-LD org nodes, tel/mailto, social links, text."""
    hints = {"jsonld": [], "phones": [], "emails": [], "social": [], "text": "",
             "og_image": read_meta(html, "og:image"), "site_name": read_meta(html, "og:site_name"),
             "title": read_title(html)}
    for b in parse_ld_blocks(html):
        if b.data is None:
            continue
        for n in _iter_nodes(b.data):
            if _family(next(iter(_types(n)), "")) == "org":
                hints["jsonld"].append(n)
    mask = Mask(html)
    for t in iter_tags(html, "a", mask):
        href = t.attrs.get("href", "").strip()
        low = href.lower()
        if low.startswith("tel:"):
            v = urllib.parse.unquote(href[4:]).strip()
            if v and v not in hints["phones"]:
                hints["phones"].append(v)
        elif low.startswith("mailto:"):
            v = href[7:].split("?")[0].strip()
            if v and v not in hints["emails"]:
                hints["emails"].append(v)
        elif any(d in low for d in _SOCIAL) and "share" not in low and "intent" not in low:
            if href not in hints["social"]:
                hints["social"].append(href)
    rp = _RegionText()
    try:
        rp.feed(html)
    except Exception:
        pass
    seen, parts = set(), []
    for chunk in rp.regions:
        if chunk not in seen:
            seen.add(chunk)
            parts.append(chunk)
    hints["text"] = " | ".join(parts)[:4500]
    return hints


def find_contact_links(html: str, base_url: str, limit: int = 2) -> list:
    mask = Mask(html)
    out = []
    host = urllib.parse.urlparse(base_url).netloc
    for t in iter_tags(html, "a", mask):
        href = t.attrs.get("href", "")
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if re.search(r"contact|about|location|find-us|visit|hours", href, re.I):
            full = urllib.parse.urljoin(base_url, href).split("#")[0]
            if urllib.parse.urlparse(full).netloc == host and full not in out and full != base_url:
                out.append(full)
        if len(out) >= limit:
            break
    return out


# ── richer site discovery (services, service areas, menu, sitemap) ───────────
_INFO_CATEGORIES = [
    ("services", r"service|what-we-do|solutions|offerings|our-work|products|specialt|treatments|repair|install"),
    ("areas", r"service-?area|areas?-we-serve|areas?-served|coverage|locations?|cities|where-we|neighborhoods|towns"),
    ("about", r"about|who-we-are|our-story|company|why-us|why-choose|team"),
    ("contact", r"contact|find-us|visit|hours|get-in-touch|directions"),
]


def find_info_links(html: str, base_url: str, per_category: int = 2, limit: int = 6) -> list:
    """Internal links most likely to describe services, service areas, the company and contact info."""
    mask = Mask(html)
    host = urllib.parse.urlparse(base_url).netloc
    picked = {name: [] for name, _ in _INFO_CATEGORIES}
    seen = set()
    for t in iter_tags(html, "a", mask):
        href = t.attrs.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urllib.parse.urljoin(base_url, href).split("#")[0].split("?")[0]
        if urllib.parse.urlparse(full).netloc != host or full in seen or full.rstrip("/") == base_url.rstrip("/"):
            continue
        if re.search(r"\.(jpg|jpeg|png|gif|webp|pdf|zip|svg)$", full, re.I):
            continue
        path = urllib.parse.urlparse(full).path.lower()
        for name, rx in _INFO_CATEGORIES:
            if re.search(rx, path) and len(picked[name]) < per_category:
                picked[name].append(full)
                seen.add(full)
                break
    out = []
    for name, _ in _INFO_CATEGORIES:                      # services and areas first
        out.extend(picked[name])
    return out[:limit]


class _AnchorTexts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self._href, self._buf, self._hide = [], None, [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "svg", "noscript", "template"):
            self._hide += 1
        elif tag == "a":
            self._href, self._buf = dict(attrs).get("href") or "", []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "svg", "noscript", "template"):
            self._hide = max(0, self._hide - 1)
        elif tag == "a" and self._href is not None:
            txt = clean_text(" ".join(self._buf))
            if txt and self._href:
                self.links.append((txt, self._href))
            self._href = None

    def handle_data(self, d):
        if self._href is not None and not self._hide:
            self._buf.append(d)


def extract_menu_links(html: str, base_url: str, limit: int = 90) -> list:
    """Visible link texts of internal pages — the site menu is usually a list of every service and area."""
    p = _AnchorTexts()
    try:
        p.feed(html)
    except Exception:
        pass
    host = urllib.parse.urlparse(base_url).netloc
    out, seen = [], set()
    for txt, href in p.links:
        if href.startswith(("#", "mailto:", "tel:", "javascript:")) or len(txt) > 70 or len(txt) < 3:
            continue
        full = urllib.parse.urljoin(base_url, href)
        if urllib.parse.urlparse(full).netloc != host:
            continue
        key = txt.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(txt)
        if len(out) >= limit:
            break
    return out


def fetch_sitemap_pages(base_url: str, limit: int = 250) -> list:
    """Readable page names taken from sitemap.xml (e.g. 'garage door repair miami'). [] if none."""
    root = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(base_url))
    urls, queue = [], [root + "/sitemap.xml"]
    fetched = 0
    while queue and fetched < 4 and len(urls) < limit:
        sm = queue.pop(0)
        fetched += 1
        try:
            xml, _ = http_get(sm, timeout=10, max_bytes=1_500_000)
        except Exception:
            continue
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml):
            if loc.lower().endswith(".xml"):
                queue.append(loc)
            else:
                urls.append(loc)
    names, seen = [], set()
    for u in urls:
        path = urllib.parse.urlparse(u).path.strip("/")
        if not path or re.search(r"\.(jpg|png|pdf|gif|webp)$", path, re.I):
            continue
        name = re.sub(r"\.(html?|php)$", "", path.split("/")[-1], flags=re.I)
        name = re.sub(r"[-_]+", " ", urllib.parse.unquote(name)).strip()
        folder = path.split("/")[-2].replace("-", " ") if "/" in path else ""
        label = f"{name} ({folder})" if folder and folder.lower() != name.lower() else name
        if label.lower() not in seen:
            seen.add(label.lower())
            names.append(label)
        if len(names) >= limit:
            break
    return names


# ── page catalogue: read the title / H1 of many site pages to list every service and area ────────
_SKIP_PATH = re.compile(
    r"(^|/)(blog|blogs|news|tag|tags|category|categories|author|feed|wp-[a-z]+|cart|checkout|login|account|"
    r"privacy|privacy-policy|terms|terms-of-service|sitemap|search|careers|jobs|testimonials?|reviews?|"
    r"gallery|faqs?|thank-?you|404)(/|$|[-_.])", re.I)


def extract_menu_urls(html: str, base_url: str, limit: int = 200) -> list:
    """Internal page URLs linked from the page (menu, footer, body), de-duplicated, in order."""
    mask = Mask(html)
    host = urllib.parse.urlparse(base_url).netloc
    out, seen = [], set()
    for t in iter_tags(html, "a", mask):
        href = t.attrs.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urllib.parse.urljoin(base_url, href).split("#")[0].split("?")[0]
        pu = urllib.parse.urlparse(full)
        if pu.netloc != host or re.search(r"\.(jpg|jpeg|png|gif|webp|pdf|zip|svg|css|js|xml)$", pu.path, re.I):
            continue
        key = full.rstrip("/").lower()
        if key in seen or key == base_url.rstrip("/").lower():
            continue
        seen.add(key)
        out.append(full)
        if len(out) >= limit:
            break
    return out


def fetch_sitemap_urls(base_url: str, limit: int = 600) -> list:
    """Raw page URLs listed in sitemap.xml (follows sitemap indexes)."""
    root = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(base_url))
    urls, queue, fetched = [], [root + "/sitemap.xml"], 0
    while queue and fetched < 6 and len(urls) < limit:
        sm = queue.pop(0)
        fetched += 1
        try:
            xml, _ = http_get(sm, timeout=10, max_bytes=2_000_000)
        except Exception:
            continue
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml):
            if loc.lower().endswith(".xml"):
                queue.append(loc)
            else:
                urls.append(loc)
    return urls[:limit]


def pick_catalog_urls(urls: list, limit: int = 150) -> list:
    """Choose up to `limit` URLs spread across the site's folders (so city pages can't crowd out services)."""
    groups: dict = {}
    for u in urls:
        path = urllib.parse.urlparse(u).path.strip("/")
        if not path or _SKIP_PATH.search("/" + path):
            continue
        groups.setdefault(path.split("/")[0] if "/" in path else "", []).append(u)
    picked, i = [], 0
    names = list(groups)
    while len(picked) < limit and any(groups.values()):
        g = groups[names[i % len(names)]]
        if g:
            picked.append(g.pop(0))
        i += 1
    return picked


def fetch_page_catalog(urls: list, workers: int = 8, timeout: int = 8, progress=None) -> list:
    """Download the first part of each page (in parallel) and return its title, H1 and description."""
    from concurrent.futures import ThreadPoolExecutor

    def one(u):
        try:
            html, final = http_get(u, timeout=timeout, max_bytes=150_000)
        except Exception:
            return None
        c = extract_content(html, max_chars=300)
        return {"url": final, "path": urllib.parse.urlparse(final).path or "/",
                "title": read_title(html), "h1": (c["h1s"][0] if c["h1s"] else ""),
                "desc": read_meta(html, "description")[:140]}
    out = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, r in enumerate(ex.map(one, urls), 1):
            if r:
                out.append(r)
            if progress and i % 25 == 0:
                progress(i, len(urls))
    return out
