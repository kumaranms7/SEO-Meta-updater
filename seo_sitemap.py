"""
seo_sitemap.py — check sitemap.xml against the real pages and build a clean one (pure, no AI, no GUI).

A sitemap should list exactly the pages that are meant to be found: live (not 301 / 302 / 410), indexable (no noindex),
whose canonical is the listed address.  analyse() lists what is wrong with an existing sitemap and which live pages are
missing; build_xml() writes the recommended list.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import seo_canonical as K
import seo_core as C

_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_EXTRA_TAGS = ("image:", "video:", "news:", "xhtml:", "mobile:")


@dataclass
class Page:
    rel: str
    url: str                  # the address the page should be listed under
    status: str               # live | 301 | 302 | 410 | noindex | junk | partial | error | unreadable
    canonical: str = ""
    mtime: str = ""


@dataclass
class SitemapReport:
    path: str = ""
    found: bool = False
    kind: str = ""            # urlset | index
    error: str = ""
    entries: int = 0
    ok: int = 0
    issues: list = field(default_factory=list)      # (loc, issue)
    missing: list = field(default_factory=list)     # (rel, url) live pages that are not listed
    live_pages: int = 0
    extras: bool = False
    new_xml: str = ""
    new_count: int = 0


def parse_sitemap(path) -> dict:
    out = {"kind": "", "urls": [], "children": [], "extras": False, "error": ""}
    try:
        raw = Path(path).read_bytes()
        root = ET.fromstring(raw)
    except (OSError, ET.ParseError) as e:
        out["error"] = f"cannot read the sitemap: {e}"
        return out
    tag = root.tag.replace(_NS, "")
    out["kind"] = "index" if tag == "sitemapindex" else "urlset"
    txt = raw.decode("utf-8", "ignore")
    out["extras"] = any(("<" + t) in txt for t in _EXTRA_TAGS)
    for el in root:
        loc = el.find(_NS + "loc")
        if loc is None:
            loc = el.find("loc")
        if loc is None or not (loc.text or "").strip():
            continue
        lm = el.find(_NS + "lastmod")
        if lm is None:
            lm = el.find("lastmod")
        item = {"loc": loc.text.strip(), "lastmod": (lm.text or "").strip() if lm is not None else ""}
        (out["children"] if out["kind"] == "index" else out["urls"]).append(item)
    return out


def _key(url: str) -> str:
    """Comparable form of an address: scheme + www are ignored, '/index.html' == '/', trailing slash ignored."""
    sp = urllib.parse.urlsplit(url.strip())
    host = sp.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    path = urllib.parse.unquote(sp.path or "/")
    if path.lower().endswith(("/index.html", "/index.htm")):
        path = path.rsplit("/", 1)[0] + "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return host + path


def _rel_candidates(loc: str) -> list:
    path = urllib.parse.unquote(urllib.parse.urlsplit(loc).path or "/").lstrip("/")
    if path == "" or path.endswith("/"):
        return [path + "index.html", path + "index.htm"]
    return [path, path + ".html", path + "/index.html"]


def read_pages(root, files: list, rules: K.Rules, site_url: str, clean_urls: bool = False, progress=None) -> list:
    """Classify every page file. `files` are absolute paths."""
    root = Path(root)
    pages = []
    total = len(files)
    for i, fp in enumerate(files, 1):
        if progress and i % 50 == 0:
            progress(i, total)
        rel = C.rel_posix(fp, root)
        expected = K.canonical_url(site_url, rel) if site_url else ""
        try:
            mtime = _dt.date.fromtimestamp(os.path.getmtime(fp)).isoformat()
        except OSError:
            mtime = ""
        hit = rules.match(rel, clean_urls) if rules.found else None
        if hit:
            pages.append(Page(rel, expected, hit.kind, "", mtime))
            continue
        if K.is_junk(rel):
            pages.append(Page(rel, expected, "junk", "", mtime))
            continue
        try:
            html, _, _ = C.read_html(fp)
        except Exception:                                        # noqa: BLE001
            pages.append(Page(rel, expected, "unreadable", "", mtime))
            continue
        canon = C.get_canonical(html, site_url)
        url = expected
        if site_url:
            d = K.decide(html, rel, site_url)
            if d.action == "ok":
                url = d.old_href or expected                   # keep the form the page already uses
            elif d.action == "keep_foreign":
                pages.append(Page(rel, expected, "canon_foreign", canon, mtime))
                continue
            elif d.action == "replace" and d.other_page:
                pages.append(Page(rel, expected, "canon_other", canon, mtime))
                continue
        reason = C.classify_skip(html, rel)
        status = "live"
        if reason == "noindex page":
            status = "noindex"
        elif reason.startswith("partial"):
            status = "partial"
        elif reason.startswith("404"):
            status = "error"
        elif reason:
            status = "redirect"
        pages.append(Page(rel, url, status, canon, mtime))
    return pages


STATUS_TEXT = {"301": "redirected (301)", "302": "temporarily redirected (302)", "410": "gone (410)",
               "noindex": "has noindex", "junk": "copy / backup file", "partial": "include file, not a page",
               "error": "error page", "unreadable": "cannot be read", "redirect": "meta-refresh redirect",
               "canon_other": "a duplicate of another page", "canon_foreign": "canonical points to another website"}


def analyse(root, site_url: str, pages: list, sitemap_path: str = "") -> SitemapReport:
    root = Path(root)
    rep = SitemapReport()
    path = Path(sitemap_path) if sitemap_path else root / "sitemap.xml"
    rep.path, rep.found = str(path), path.is_file()
    base = K.norm_site(site_url)
    live = [p for p in pages if p.status == "live"]
    rep.live_pages = len(live)
    by_key = {_key(p.url): p for p in live if p.url}
    by_rel = {p.rel: p for p in pages}
    old_lastmod = {}

    listed = set()
    if rep.found:
        sm = parse_sitemap(path)
        rep.kind, rep.error, rep.extras = sm["kind"], sm["error"], sm["extras"]
        if rep.kind == "index":
            rep.error = rep.error or ("this is a sitemap index (it lists other sitemaps) — check each file on its own; "
                                      "it is not rewritten")
        seen = set()
        for u in sm["urls"]:
            loc = u["loc"]
            rep.entries += 1
            if u["lastmod"]:
                old_lastmod[_key(loc)] = u["lastmod"]
            k = _key(loc)
            if k in seen:
                rep.issues.append((loc, "listed more than once"))
                continue
            seen.add(k)
            problems = []
            sp, bp = urllib.parse.urlsplit(loc), urllib.parse.urlsplit(base)
            if base and K._site_key(loc) != K._site_key(base):
                problems.append("belongs to another website")
            elif base and (sp.netloc.lower() != bp.netloc.lower() or sp.scheme != bp.scheme):
                problems.append(f"wrong www / http form — should start with {bp.scheme}://{bp.netloc}")
            pg = None
            for cand in _rel_candidates(loc):
                if cand in by_rel:
                    pg = by_rel[cand]
                    break
            if pg is None:
                problems.append("no page file found for this address")
            elif pg.status in ("canon_other", "canon_foreign"):
                problems.append(f"page's canonical is {pg.canonical} — it is {STATUS_TEXT[pg.status]}")
            elif pg.status != "live":
                problems.append(f"page is {STATUS_TEXT.get(pg.status, pg.status)}")
            else:
                listed.add(pg.rel)
                if pg.canonical and _key(pg.canonical) != k and K._site_key(pg.canonical) == K._site_key(loc):
                    problems.append(f"page's canonical is {pg.canonical} — list that address instead")
                elif urllib.parse.urlsplit(loc).path.lower().endswith(("/index.html", "/index.htm")):
                    problems.append("use the folder address instead of /index.html")
            if problems:
                for pr in problems:
                    rep.issues.append((loc, pr))
            else:
                rep.ok += 1
    rep.missing = [(p.rel, p.url) for p in live if p.rel not in listed and p.url]

    # recommended sitemap: every live page under its canonical address
    rows = []
    for p in sorted(live, key=lambda x: (x.url or "").lower()):
        if not p.url:
            continue
        rows.append((p.url, old_lastmod.get(_key(p.url)) or p.mtime))
    rep.new_count = len(rows)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for url, lm in rows:
        lines.append("  <url>")
        lines.append(f"    <loc>{_xml(url)}</loc>")
        if lm:
            lines.append(f"    <lastmod>{_xml(lm)}</lastmod>")
        lines.append("  </url>")
    lines.append("</urlset>")
    rep.new_xml = "\n".join(lines) + "\n"
    return rep


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def report_rows(rep: SitemapReport) -> list:
    """Rows for a CSV: (kind, address, note)."""
    rows = [("in sitemap", loc, why) for loc, why in rep.issues]
    rows += [("missing from sitemap", url, rel) for rel, url in rep.missing]
    return rows
