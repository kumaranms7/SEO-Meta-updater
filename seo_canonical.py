"""
seo_canonical.py — step 1 of a run: canonical tags + .htaccess redirect / gone detection (pure, no GUI, no AI).

* parse_htaccess()  reads Redirect / RedirectMatch / RewriteRule rules (301, 302, 410) in file order.
  Site-wide rules (www / https enforcement, catch-alls such as ^/(.*)\\.html$ -> home page, rules with RewriteCond)
  are never treated as page rules; they are listed as "ignored" with the reason so the user can see them.
* Rules.match()     tells whether a page URL is redirected (301 / 302) or gone (410).
* decide() / apply_decision()  work out and apply the canonical <link> for one page with the quote-aware scanner:
      missing  -> insert            same page, right host -> leave alone
      points to a different page of THIS site (or a wrong host / relative / empty) -> replace
      points to ANOTHER domain -> keep and flag
* undo() rebuilds the page as it was before the canonical edit (so the report's "before" score stays honest).
"""
from __future__ import annotations

import re
import shlex
import urllib.parse
from dataclasses import dataclass, field

import seo_core as C

KIND_TEXT = {"301": "301 redirect", "302": "302 temporary redirect", "410": "410 gone"}

_STATUS = {"301": "301", "permanent": "301", "308": "301",
           "302": "302", "temp": "302", "temporary": "302", "303": "302", "seeother": "302", "307": "302",
           "410": "410", "gone": "410"}

# pages the injector must not tag: copies, backups, system folders, our own reports
_JUNK_DIRS = {"backup", "backups", "_notes", "cgi-bin", "node_modules", ".git", "_vti_cnf", "_backup", "old", "bkp"}
_COPY_RE = re.compile(r"(^|[\s_\-.(])(copy|kopie|backup|bak|old)([\s_\-.)]|$)|\(\d+\)", re.I)


# ─────────────────────────────────────────────────────────────────────────────
# .htaccess
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Rule:
    kind: str                 # "301" | "302" | "410"
    mode: str                 # "path" (Redirect, prefix match) | "regex" (RedirectMatch) | "rewrite" (RewriteRule)
    pattern: str
    raw: str
    line: int
    rx: object = None
    target: str = ""          # where a 301 / 302 sends the visitor (may contain $1 back-references)


@dataclass
class Ignored:
    raw: str
    line: int
    why: str


@dataclass
class Rules:
    rules: list = field(default_factory=list)
    ignored: list = field(default_factory=list)
    found: bool = False

    def counts(self) -> dict:
        c = {"301": 0, "302": 0, "410": 0}
        for r in self.rules:
            c[r.kind] += 1
        return c

    # ── matching a URL path (used for links), not a file ──────────────────────────
    def hit(self, path: str):
        """(rule, match) for the first rule that hits this URL path, else None."""
        variants = [path]
        if path.endswith("/"):
            variants.append(path + "index.html")
        elif path.lower().endswith(("/index.html", "/index.htm")):
            variants.append(path.rsplit("/", 1)[0] + "/")
        for r in self.rules:
            for u in variants:
                if r.mode == "path":
                    p = r.pattern.rstrip("/")
                    if u == p or u.startswith(p + "/"):
                        return r, u
                else:
                    m = r.rx.search(u if r.mode == "regex" else u.lstrip("/"))
                    if m:
                        return r, m
        return None

    @staticmethod
    def target_of(r: "Rule", found) -> str:
        if not r.target:
            return ""
        if r.mode == "path":
            p = r.pattern.rstrip("/")
            rest = found[len(p):] if isinstance(found, str) else ""
            return (r.target.rstrip("/") + rest) if rest else r.target
        try:
            return found.expand(re.sub(r"\$(\d)", r"\\\1", r.target))
        except (re.error, IndexError):
            return ""

    def follow(self, path: str, site_host: str, depth: int = 6):
        """Where does a link to `path` really end up?  None when no rule applies, else a dict:
        rule, kind (of the FIRST rule), final_path / final_query, gone, temp, external (url), loop, unresolved."""
        first = self.hit(path)
        if not first:
            return None
        out = {"rule": first[0], "kind": first[0].kind, "final_path": path, "final_query": "", "gone": False,
               "temp": False, "external": "", "loop": False, "unresolved": False, "hops": 0}
        cur, seen = path, {path}
        for _ in range(depth):
            h = self.hit(cur)
            if not h:
                return out
            r, found = h
            if r.kind == "410":
                out.update(gone=True, final_path=cur)
                return out
            if r.kind == "302":
                out.update(temp=True, final_path=cur)
                return out
            tgt = self.target_of(r, found)
            if not tgt:
                out.update(unresolved=True, final_path=cur)
                return out
            sp = urllib.parse.urlsplit(tgt)
            if sp.scheme in ("http", "https"):
                if _site_key(tgt) != site_host:
                    out.update(external=tgt, final_path=cur)
                    return out
                nxt = sp.path or "/"
            else:
                nxt = tgt.split("?")[0].split("#")[0]
                nxt = nxt if nxt.startswith("/") else "/" + nxt
            out["final_query"] = ("?" + sp.query) if sp.query else ""
            out["hops"] += 1
            if nxt in seen:
                out.update(loop=True, final_path=cur)
                return out
            seen.add(nxt)
            cur = nxt
            out["final_path"] = cur
        out.update(loop=True)                                    # too many hops
        return out

    def match(self, rel: str, clean_urls: bool = False):
        """First rule (file order) that hits this page, or None."""
        paths = url_paths(rel, clean_urls)
        for r in self.rules:
            if r.mode == "path":
                p = r.pattern.rstrip("/")
                if any(u == p or u.startswith(p + "/") for u in paths):
                    return r
            elif r.mode == "regex":
                if any(r.rx.search(u) for u in paths):
                    return r
            else:                                              # RewriteRule in .htaccess sees the path WITHOUT "/"
                if any(r.rx.search(u.lstrip("/")) for u in paths):
                    return r
        return None


def url_paths(rel: str, clean_urls: bool = False) -> list:
    """The URL paths a file is served at.  Clean-URL variants (/page, /page/) only when asked for."""
    rel = rel.replace("\\", "/").lstrip("/")
    parts = rel.split("/")
    name = parts[-1]
    if name.lower() in ("index.html", "index.htm"):
        d = "/".join(parts[:-1])
        base = "/" + d if d else ""
        out = [base + "/", base + "/" + name]
        if clean_urls and base:
            out.append(base)
        return out
    out = ["/" + rel]
    if clean_urls:
        stem = "/" + rel.rsplit(".", 1)[0]
        out += [stem, stem + "/"]
    return out


_PROBES = ["/a.html", "/about.html", "/x/y.html", "/x/y/z-w.html", "/services/page.html", "/x/index.html"]


def _catch_all(rx, rewrite: bool) -> bool:
    """True if the pattern matches (almost) every page — a domain-wide rule, not a page rule."""
    probes = [p.lstrip("/") if rewrite else p for p in _PROBES]
    return sum(1 for p in probes if rx.search(p)) >= 4


def _tokens(line: str) -> list:
    try:
        return shlex.split(line, posix=True)
    except ValueError:
        return line.split()


def _status(tok: str):
    return _STATUS.get(tok.lower())


def parse_htaccess(text: str) -> Rules:
    out = Rules(found=True)
    # join "\" line continuations, keep the first physical line number
    lines, buf, start = [], "", 0
    for i, raw in enumerate(text.splitlines(), 1):
        if not buf:
            start = i
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        lines.append((start, buf + raw))
        buf = ""
    if buf:
        lines.append((start, buf))

    engine_on = False
    conds = 0
    for no, raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("<"):
            continue
        low = line.lower()
        if re.match(r"rewriteengine\s+on\b", low):
            engine_on = True
            continue
        if re.match(r"rewriteengine\s+off\b", low):
            engine_on = False
            continue
        if low.startswith("rewritecond"):
            conds += 1
            continue
        tok = _tokens(line)
        if not tok:
            continue
        d = tok[0].lower()

        if d in ("redirect", "redirectpermanent", "redirecttemp"):
            kind, path = None, None
            if d == "redirectpermanent":
                kind, rest = "301", tok[1:]
            elif d == "redirecttemp":
                kind, rest = "302", tok[1:]
            elif len(tok) > 1 and tok[1].startswith("/") is False and (_status(tok[1]) or tok[1].isdigit()):
                kind, rest = _status(tok[1]), tok[2:]
                if kind is None:
                    continue                                   # 401/403/404… are not redirects
            else:
                kind, rest = "302", tok[1:]
            if not rest:
                continue
            path = rest[0]
            if path.rstrip("/") == "":
                out.ignored.append(Ignored(line, no, "applies to the whole site"))
                continue
            out.rules.append(Rule(kind, "path", path, line, no, None, rest[1] if len(rest) > 1 else ""))
            continue

        if d == "redirectmatch":
            kind, rest = "302", tok[1:]
            if len(tok) > 1 and (_status(tok[1]) or tok[1].isdigit()) and not tok[1].startswith(("^", "/", "(")):
                kind, rest = _status(tok[1]), tok[2:]
                if kind is None:
                    continue
            if not rest:
                continue
            try:
                rx = re.compile(rest[0])
            except re.error:
                out.ignored.append(Ignored(line, no, "pattern could not be read"))
                continue
            if _catch_all(rx, False):
                out.ignored.append(Ignored(line, no, "catch-all — matches every page, so it is not used"))
                continue
            out.rules.append(Rule(kind, "regex", rest[0], line, no, rx, rest[1] if len(rest) > 1 else ""))
            continue

        if d == "rewriterule":
            had_conds, conds = conds, 0                        # conditions belong to the next rule only
            if not engine_on or len(tok) < 3:
                continue
            pat, subst = tok[1], tok[2]
            flags = tok[3].strip("[]").upper().split(",") if len(tok) > 3 else []
            kind = None
            if "G" in flags:
                kind = "410"
            else:
                for f in flags:
                    if f == "R" or f.startswith("R="):
                        code = f[2:] if "=" in f else "302"
                        kind = _STATUS.get(code.lower()) or ("302" if code.isdigit() else None)
                        break
            if kind is None:
                continue                                       # an internal rewrite, not a redirect
            if had_conds:
                out.ignored.append(Ignored(line, no, "has RewriteCond (host / https / user-agent rule)"))
                continue
            if "%{" in subst or re.match(r"^(https?://)?%\d+/?$", subst):
                out.ignored.append(Ignored(line, no, "site-wide host / https rule"))
                continue
            try:
                rx = re.compile(pat, re.I if "NC" in flags else 0)
            except re.error:
                out.ignored.append(Ignored(line, no, "pattern could not be read"))
                continue
            if _catch_all(rx, True) or (kind != "410" and subst.strip("/") == ""):
                out.ignored.append(Ignored(line, no, "catch-all — matches every page, so it is not used"))
                continue
            out.rules.append(Rule(kind, "rewrite", pat, line, no, rx, "" if subst == "-" else subst))
    return out


def load_htaccess(path) -> Rules:
    from pathlib import Path
    p = Path(path)
    if not p.is_file():
        return Rules(found=False)
    try:
        text = p.read_bytes().decode("utf-8-sig", errors="replace")
    except OSError:
        return Rules(found=False)
    return parse_htaccess(text)


# ─────────────────────────────────────────────────────────────────────────────
# Which files get a canonical at all
# ─────────────────────────────────────────────────────────────────────────────
def is_junk(rel: str) -> str:
    """Reason a file must not be tagged (copies, backups, system folders, our reports), or ''."""
    parts = rel.replace("\\", "/").split("/")
    if any(p.lower() in _JUNK_DIRS for p in parts[:-1]):
        return "backup / system folder"
    name = parts[-1]
    if name.lower().startswith(("seo_report_", "canonical_injector_report")):
        return "report file"
    stem = name.rsplit(".", 1)[0]
    if _COPY_RE.search(stem):
        return "looks like a copy / backup file"
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Canonical URL + decision
# ─────────────────────────────────────────────────────────────────────────────
def norm_site(site_url: str) -> str:
    s = (site_url or "").strip().rstrip("/")
    if s and not re.match(r"^https?://", s, re.I):
        s = "https://" + s
    return s


def canonical_url(site_url: str, rel: str) -> str:
    base = norm_site(site_url)
    path = url_paths(rel)[0]                                   # index.html -> the folder URL
    return base + urllib.parse.quote(path, safe="/-._~!$&'()*+,;=:@%")


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower()


def _site_key(url: str) -> str:
    h = _host(url).split("@")[-1].split(":")[0]
    return h[4:] if h.startswith("www.") else h


@dataclass
class Decision:
    action: str               # insert | replace | ok | keep_foreign | nohead
    url: str = ""             # the canonical the page should have
    old_href: str = ""
    old_raw: str = ""
    new_tag: str = ""
    note: str = ""
    edits: list = field(default_factory=list)     # (start, end, replacement) for replace / cleanup
    insert_at: int = -1
    insert_text: str = ""
    other_page: bool = False  # the existing canonical names a DIFFERENT page of this site


def make_tag(url: str) -> str:
    return '<link rel="canonical" href="' + C.esc_attr(url) + '">'


def _canonical_tags(html: str, mask) -> list:
    return [t for t in C.iter_tags(html, "link", mask) if "canonical" in t.attrs.get("rel", "").lower().split()]


def decide(html: str, rel: str, site_url: str) -> Decision:
    mask = C.Mask(html)
    hb = C.head_bounds(html, mask)
    if not hb:
        return Decision("nohead", note="no <head> found")
    url = canonical_url(site_url, rel)
    tag = make_tag(url)
    found = _canonical_tags(html, mask)
    if not found:
        return _insert(html, hb, Decision("insert", url=url, new_tag=tag))
    first = found[0]
    href = first.attrs.get("href", "").strip()
    d = Decision("replace", url=url, old_href=href, old_raw=first.raw, new_tag=tag)
    extras = found[1:]
    resolved = urllib.parse.urljoin(url, href) if href else ""
    same_form = False
    if resolved:
        if href and re.match(r"^https?://", href, re.I) and _site_key(resolved) != _site_key(url):
            return Decision("keep_foreign", url=url, old_href=href, old_raw=first.raw,
                            note=f"canonical points to another domain ({href}) — kept")
        ep = urllib.parse.urlsplit(resolved)
        site_sp = urllib.parse.urlsplit(norm_site(site_url))
        host_ok = (ep.scheme.lower(), ep.netloc.lower()) == (site_sp.scheme.lower(), site_sp.netloc.lower())
        variants = set(url_paths(rel, clean_urls=True))
        path = urllib.parse.unquote(ep.path)
        same_page = not ep.query and (path in variants or path.rstrip("/") in variants
                                      or path.rstrip("/") + "/" in variants)
        if href and not re.match(r"^https?://", href, re.I):
            d.note = "relative canonical made absolute"
        elif same_page and host_ok:
            same_form = True
        elif same_page:
            d.note = "canonical had the wrong host / scheme"
        else:
            d.note = "canonical pointed to a different page of this site"
            d.other_page = True
    else:
        d.note = "empty canonical replaced"
    if same_form and not extras:
        return Decision("ok", url=url, old_href=href, old_raw=first.raw)
    if same_form:
        d.note = "duplicate canonical tags cleaned up"
        d.new_tag = first.raw                                     # keep the first tag as it is
        d.edits = [(first.start, first.end, first.raw)]
    else:
        d.edits = [(first.start, first.end, tag)]
    for t in extras:
        d.edits.append((t.start, t.end, ""))
    return d


def _insert(html: str, hb: tuple, d: Decision) -> Decision:
    pos = hb[1]                                                   # start of </head>
    ls = html.rfind("\n", 0, pos)
    own_line = ls != -1 and not html[ls + 1:pos].strip()
    if own_line:
        d.insert_at = ls + 1
        d.insert_text = C._indent(html, hb) + d.new_tag + "\n"
    else:
        d.insert_at = pos
        d.insert_text = "\n" + C._indent(html, hb) + d.new_tag + "\n"
    return d


def apply_decision(html: str, d: Decision) -> str:
    """Pure: returns the edited page. `html` must be the text decide() was given."""
    if d.action == "insert":
        return html[:d.insert_at] + d.insert_text + html[d.insert_at:]
    if d.action == "replace":
        out = html
        for s, e, rep in sorted(d.edits, reverse=True):
            if not rep:                                           # swallow the whole line of a removed duplicate
                ls = out.rfind("\n", 0, s)
                if ls != -1 and not out[ls + 1:s].strip():
                    s = ls
            out = out[:s] + rep + out[e:]
        return out
    return html


def remove_all(html: str):
    """Strip every canonical tag (for redirected / gone pages). Returns (html, count)."""
    mask = C.Mask(html)
    tags = _canonical_tags(html, mask)
    out = html
    for t in sorted(tags, key=lambda t: t.start, reverse=True):
        s = t.start
        ls = out.rfind("\n", 0, s)
        if ls != -1 and not out[ls + 1:s].strip():
            s = ls
        out = out[:s] + out[t.end:]
    return out, len(tags)


def undo(html: str, entry: dict) -> str:
    """Rebuild the page as it was before the canonical edit recorded in `entry` (see entry_of())."""
    new, old = entry.get("new", ""), entry.get("old", "")
    if not new or new not in html:
        return html
    if entry.get("a") == "insert":
        i = html.index(new)
        s, e = i, i + len(new)
        ls = html.rfind("\n", 0, s)
        if ls != -1 and not html[ls + 1:s].strip():
            s = ls
            if html[e:e + 1] == "\n":                             # the line we added ends with its own newline
                e += 0
        else:
            e = e + (1 if html[e:e + 1] == "\n" else 0)
        return html[:s] + html[e:]
    if entry.get("a") == "replace" and old:
        return html.replace(new, old, 1)
    return html


def entry_of(d: Decision) -> dict:
    """What is kept (and saved in the progress file) about a canonical edit."""
    a = {"insert": "insert", "replace": "replace"}.get(d.action, d.action)
    return {"a": a, "url": d.url, "new": d.new_tag if a in ("insert", "replace") else "",
            "old": d.old_raw, "note": d.note}


ACTION_TEXT = {"insert": "added", "replace": "fixed", "ok": "ok", "keep_foreign": "other domain",
               "removed": "removed", "nohead": "no <head>", "error": "error"}
