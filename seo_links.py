"""
seo_links.py — internal links that point at redirected (301) or gone (410) pages (pure, no AI, no GUI).

fix_links() reads every <a href> of a page, asks the .htaccess rules where the link really ends up and
* rewrites a link to a 301-redirected page so it points straight at the final page (same link style: absolute,
  root-relative or relative; query and #fragment are kept),
* only REPORTS links to gone (410) pages, links that end on another website, temporary (302) redirects, loops.
"""
from __future__ import annotations

import posixpath
import re
import urllib.parse
from dataclasses import dataclass

import seo_canonical as K
import seo_core as C

_HREF = re.compile(r"""(\bhref\s*=\s*)(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "sms:", "whatsapp:", "#")


@dataclass
class LinkChange:
    page: str
    old: str
    new: str
    rule: str          # e.g. "301 redirect (line 12)"


@dataclass
class LinkFinding:
    page: str
    href: str
    issue: str


def _page_dir(rel: str) -> str:
    return posixpath.dirname("/" + rel.replace("\\", "/").lstrip("/")) or "/"


def link_path(href: str, rel: str, site_host: str):
    """(url_path, query, fragment, style) for an internal link, or None for anything else.
    style: 'absolute' | 'root' | 'relative'."""
    h = href.strip()
    if not h or h.lower().startswith(_SKIP_SCHEMES):
        return None
    sp = urllib.parse.urlsplit(h)
    if sp.scheme in ("http", "https"):
        if K._site_key(h) != site_host:
            return None
        style, path = "absolute", sp.path or "/"
    elif sp.scheme or h.startswith("//"):
        return None
    elif h.startswith("/"):
        style, path = "root", sp.path
    else:
        style = "relative"
        path = posixpath.normpath(posixpath.join(_page_dir(rel), sp.path)) if sp.path else ""
        if sp.path.endswith("/") and not path.endswith("/"):
            path += "/"
        if not sp.path:                                          # "?x" or "#x" only
            return None
    return urllib.parse.unquote(path), sp.query, sp.fragment, style


def _new_href(style: str, new_path: str, query: str, frag: str, rel: str, site_base: str) -> str:
    quoted = urllib.parse.quote(new_path, safe="/-._~!$&'()*+,;=:@%")
    if style == "absolute":
        out = site_base + quoted
    elif style == "root":
        out = quoted
    else:
        d = _page_dir(rel)
        out = posixpath.relpath(new_path.rstrip("/") or "/", start=d)
        out = urllib.parse.quote(out, safe="/-._~!$&'()*+,;=:@%.")
        if new_path.endswith("/") and not out.endswith("/"):
            out += "/"
        if out == ".":
            out = "./"
    if query:
        out += "?" + query
    if frag:
        out += "#" + frag
    return out


def fix_links(html: str, rel: str, site_url: str, rules: K.Rules):
    """Returns (new_html, [LinkChange], [LinkFinding]). Pure: nothing is written."""
    base = K.norm_site(site_url)
    host = K._site_key(base)
    mask = C.Mask(html)
    changes, findings, edits = [], [], []
    if re.search(r"<base\b[^>]*\bhref", html, re.I):              # a <base href> changes how relative links resolve
        return html, [], [LinkFinding(rel, "", "page has a <base href> — links not checked")]
    for t in C.iter_tags(html, "a", mask):
        href = t.attrs.get("href", "")
        lp = link_path(href, rel, host)
        if not lp:
            continue
        path, query, frag, style = lp
        f = rules.follow(path, host)
        if not f:
            continue
        rule = f["rule"]
        label = f"{K.KIND_TEXT[rule.kind]} (line {rule.line})"
        if f["gone"]:
            findings.append(LinkFinding(rel, href, "links to a page that is gone (410) — remove or replace this link"
                                        if f["hops"] == 0 else
                                        "redirects to a page that is gone (410) — remove or replace this link"))
            continue
        if f["temp"]:
            findings.append(LinkFinding(rel, href, "temporary (302) redirect — left as it is"))
            continue
        if f["external"]:
            findings.append(LinkFinding(rel, href, f"redirects to another website ({f['external'][:80]}) — left as it is"))
            continue
        if f["loop"] or f["unresolved"]:
            findings.append(LinkFinding(rel, href, "redirect loop or unreadable target — left as it is"))
            continue
        if f["final_path"] == path:
            continue
        new = _new_href(style, f["final_path"], f["final_query"] or query, frag, rel, base)
        m = _HREF.search(t.raw)
        if not m or new == href:
            continue
        q = '"' if m.group(2) is not None else ("'" if m.group(3) is not None else '"')
        val_start = t.start + (m.start(2) if m.group(2) is not None else
                               m.start(3) if m.group(3) is not None else m.start(4))
        val_end = t.start + (m.end(2) if m.group(2) is not None else
                             m.end(3) if m.group(3) is not None else m.end(4))
        if m.group(4) is not None:                                # unquoted attribute: add quotes
            val_start, val_end = val_start, val_end
            edits.append((val_start, val_end, q + C.esc_attr(new) + q))
        else:
            edits.append((val_start, val_end, C.esc_attr(new)))
        changes.append(LinkChange(rel, href, new, label))
    for s, e, rep in sorted(edits, reverse=True):
        html = html[:s] + rep + html[e:]
    return html, changes, findings
