"""
seo_pipeline.py — the run engine (no GUI). Reads each page, asks Claude for metadata,
validates / rewrites until it fits, applies safe edits, backs up, writes, records progress.
"""
from __future__ import annotations

import copy
import itertools
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import seo_ai as A
import seo_canonical as K
import seo_core as C
import seo_links as L
import seo_score as S
import seo_structure as ST


@dataclass
class RunOptions:
    mode: str = "dry_run"                 # dry_run | per_page | batch
    run_kind: str = "fresh"               # fresh | resume | retry
    folder: str = ""
    site_url: str = ""
    brand: str = ""
    sep: str = "|"
    site_context: str = ""
    default_image: str = ""
    facts: dict = field(default_factory=dict)
    model: str = "claude-haiku-4-5"
    rewrite_model: str = ""
    do_h1: bool = True
    do_schema: bool = True
    do_robots: bool = True
    do_lazy: bool = True
    do_alt: bool = True
    ai_alt: bool = True
    do_og: bool = True
    backup: bool = True
    skip_partial: bool = True
    skip_noindex: bool = True
    skip_redirect: bool = True
    skip_error: bool = True
    exclude_patterns: list = field(default_factory=list)
    excluded_paths: set = field(default_factory=set)
    # scoring
    do_score: bool = True
    deep_check: bool = False              # AI-judged points (optional, costs tokens)
    score_model: str = ""                 # "" = same as the main model
    score_target: float = 90.0
    # score protection: never write a version that scores lower than the page had
    protect_score: bool = True
    max_retries: int = 3                  # extra fetches after the first one (stops as soon as the score is >= before)
    # step 1 — canonical tags (the app switches these on; the plain defaults keep older callers unchanged)
    do_canonical: bool = False            # add / fix the canonical tag on every page BEFORE the metadata step
    canon_remove_redirected: bool = True  # take the canonical off pages that are 301 / 302 / 410 in .htaccess
    skip_htaccess: bool = False           # leave 301 / 302 / 410 pages out of the metadata step
    htaccess_path: str = ""               # "" = <folder>/.htaccess
    clean_url_match: bool = False         # also match /page and /page/ for page.html when reading .htaccess rules
    fix_links: bool = False               # internal links to 301-redirected pages -> point at the final page
    # page structure
    fix_multi_h1: bool = False            # keep the best H1, turn the others into H2
    do_breadcrumb: bool = False           # BreadcrumbList JSON-LD from the folder path
    do_faq: bool = False                  # FAQPage JSON-LD from question headings / <details> already on the page


def new_result(fp: str, rel: str, mode: str) -> dict:
    return {
        "file": fp, "rel": rel, "mode": mode, "status": "", "reason": "", "notes": [],
        "page_name": "", "folder_context": "",
        "old_title": "", "new_title": "", "old_description": "", "new_description": "",
        "old_h1": "", "new_h1": "", "title_len": 0, "desc_len": 0,
        "title_changed": False, "description_changed": False, "h1_changed": False,
        "schema_type": "", "schema_action": "", "schema_json": None,
        "robots_added": False, "keywords_removed": 0, "og_changed": False,
        "lazy_added": 0, "alts_added": 0, "image_url": "", "image_source": "",
        "canonical": "", "similar_to": "", "cost": 0.0, "error": "",
        "score_before": None, "score_after": None, "view_before": None, "view_after": None,
        "attempts": 1, "protected": False, "retry_note": "", "canonical_action": "", "review": None,
        "h1_demoted": 0, "breadcrumb_added": False, "faq_items": 0,
    }


class Pipeline:
    def __init__(self, opts: RunOptions, ai: A.AI, files: list, log, on_result, on_progress, stop_event,
                 on_scores=None, on_phase=None, on_canonical_done=None):
        self.o = opts
        self.on_scores = on_scores
        self.on_phase = on_phase
        self.on_canonical_done = on_canonical_done   # (stats: dict) -> bool; False = stop after step 1 (canonical only)
        self.canonical_only = False
        self.canon = {}                      # rel -> what step 1 did to the page (see seo_canonical.entry_of)
        self.canon_dec = {}                  # rel -> Decision (dry runs re-apply it in memory)
        self.redirected = {}                 # rel -> (.htaccess Rule) for pages that are 301 / 302 / 410
        self.canon_counts = {}
        self.link_changes, self.link_findings = [], []
        self.links_csv = ""
        self.results = []                    # every result of THIS session (used for the final score pass)
        self.score_items = []
        self.score_summary = None
        self.ai = ai
        self.files = files
        self.log = log
        self.on_result = on_result
        self.on_progress = on_progress
        self.stop = stop_event
        self.root = Path(opts.folder)
        self.dry = opts.mode == "dry_run"
        self.facts = dict(opts.facts or {})
        self.facts.setdefault("name", opts.brand)
        if not self.facts.get("name"):
            self.facts["name"] = opts.brand
        self.cfg = A.GenConfig(model=opts.model, rewrite_model=opts.rewrite_model, brand=opts.brand,
                               sep=opts.sep, site_context=opts.site_context, site_url=opts.site_url,
                               do_h1=opts.do_h1, log=log)
        self.reg = C.Registry()
        self.dups = C.DupDetector()
        self.dup_counter = 0
        self.state = None if self.dry else C.RunState(opts.folder)

    # ── public ───────────────────────────────────────────────────────────────
    def run(self):
        try:
            self._run()
        except A.FatalAPIError as e:
            self.log(f"Run stopped — {e}. Fix the API key / billing and press Resume.", "err")
            self.stop.set()
        finally:
            if self.state is not None:
                self.state.save()

    def _run(self):
        o, state = self.o, self.state
        if state is not None:
            if o.run_kind == "fresh":
                state.reset()
            if o.backup and not state.backup_dir:
                state.backup_dir = str(C.default_backup_dir(self.root))
            state.save()
        if o.run_kind in ("resume", "retry") and state is not None:
            self.load_registry_from_state()
            self.log(f"Resuming: {len(self.reg)} previously finished pages loaded for uniqueness checks.", "info")

        if o.default_image:
            r = C.resolve_image(o.default_image, self.root / "index.html", self.root, o.site_url)
            if r is None or not r["exists"]:
                self.log(f"Default share image not found: {o.default_image} — pages without their own "
                         f"image will have no og:image.", "warn")
        if not o.site_url:
            self.log("No site URL set — share images and schema URLs cannot be built.", "warn")

        todo = self._select_todo()
        self._canonical_phase(todo)
        if self.stop.is_set():
            return
        if (o.do_canonical or o.skip_htaccess or o.fix_links) and self.on_canonical_done:
            try:
                go = self.on_canonical_done(self.canonical_stats(todo))
            except Exception:                                    # noqa: BLE001  (a broken dialog must not lose the run)
                go = True
            if not go:
                self.canonical_only = True
                self.log("Step 1 finished — the SEO update was cancelled. Canonical tags stay as they are.", "warn")
                return
        if self.on_phase and (o.do_canonical or o.skip_htaccess or o.fix_links):
            self.on_phase("Step 2 of 2 — meta titles, descriptions and H1")
        if self.redirected and o.skip_htaccess:
            self.log(f"Step 2 of 2 — metadata for {len(todo) - len(self.redirected)} pages "
                     f"({len(self.redirected)} redirected / gone pages left out).", "info")
        total = len(self.files)
        done = total - len(todo)
        self.on_progress(done, total)
        size = A.BATCH_SIZE if o.mode == "batch" else 1
        for i in range(0, len(todo), size):
            if self.stop.is_set():
                self.log("Stopped by user — progress saved, you can resume later.", "warn")
                break
            chunk = todo[i:i + size]
            self._process_chunk(chunk)           # emits each result as soon as it is final
            done += len(chunk)
            self.on_progress(done, total)
        if o.do_score:
            self._finish_scores()

    # ── step 1 of 2: canonical tags (+ .htaccess 301 / 302 / 410 detection) ─────────────────────────────
    def _canonical_phase(self, todo: list):
        o = self.o
        if not (o.do_canonical or o.skip_htaccess or o.fix_links):
            return
        can = o.do_canonical
        if (can or o.fix_links) and not K.norm_site(o.site_url):
            self.log("Canonical tags and link fixing skipped — set the website address first.", "warn")
            can = False
        links = o.fix_links and bool(K.norm_site(o.site_url))
        if self.on_phase:
            self.on_phase("Step 1 of 2 — canonical tags" + (" and internal links" if links else ""))
        ht = Path(o.htaccess_path) if o.htaccess_path else self.root / ".htaccess"
        rules = K.load_htaccess(ht)
        if rules.found:
            c = rules.counts()
            self.log(f"Step 1 of 2 — .htaccess: {c['301']} redirect (301), {c['302']} temporary (302) and "
                     f"{c['410']} gone (410) page rule(s); {len(rules.ignored)} site-wide / conditional rule(s) ignored.",
                     "info")
        else:
            self.log("Step 1 of 2 — no .htaccess found: redirected pages cannot be detected"
                     + (" and links cannot be fixed." if links else "."), "warn")
            links = False
        try:
            subs = [x for x in self.root.rglob(".htaccess") if x.parent != self.root]
            if subs:
                self.log(f"  {len(subs)} .htaccess file(s) in sub-folders are not read — only the main one is used.", "warn")
        except OSError:
            pass

        cnt = {"insert": 0, "replace": 0, "ok": 0, "keep_foreign": 0, "removed": 0, "redirected": 0, "junk": 0,
               "error": 0, "301": 0, "302": 0, "410": 0, "links_fixed": 0, "link_pages": 0, "links_gone": 0,
               "links_other": 0, "links": links}
        saved = self.state.data.get("canon", {}) if self.state is not None else {}
        total = len(todo)
        for i, fp in enumerate(todo, 1):
            if self.stop.is_set():
                self.log("Stopped by user during the canonical step.", "warn")
                break
            rel = C.rel_posix(fp, self.root)
            if i % 25 == 0 or i == total:
                self.on_progress(i, total)
            if o.excluded_paths and rel in o.excluded_paths or C.matches_patterns(rel, o.exclude_patterns):
                continue
            hit = rules.match(rel, o.clean_url_match) if rules.found else None
            try:
                html, enc, nl = C.read_html(fp)
            except Exception:                                    # noqa: BLE001  (step 2 reports unreadable files)
                continue
            new_html, entry, act, dec = html, None, "", None
            lchanges, lfind = [], []
            if hit:
                self.redirected[rel] = hit
                cnt["redirected"] += 1
                cnt[hit.kind] += 1
                if can and o.canon_remove_redirected:
                    new_html, n = K.remove_all(html)
                    if n:
                        entry, act = {"a": "removed", "url": "", "new": "", "old": "", "note": ""}, "removed"
            else:
                junk = K.is_junk(rel)
                if can:
                    if junk or C.classify_skip(html, rel, o.skip_partial, o.skip_noindex, o.skip_redirect, o.skip_error):
                        cnt["junk"] += 1
                    else:
                        dec = K.decide(html, rel, o.site_url)
                        entry, act = K.entry_of(dec), dec.action
                        if act == "ok":
                            old = saved.get(rel)
                            if old and old.get("a") in ("insert", "replace") and old.get("new") and old["new"] in html:
                                entry = old                      # added in an earlier session — keep how to undo it
                        elif act in ("insert", "replace"):
                            new_html = K.apply_decision(html, dec)
                if links and not junk:                           # includes partials / menus: that is where most links live
                    new_html, lchanges, lfind = L.fix_links(new_html, rel, o.site_url, rules)

            if new_html != html and not self._canon_write(fp, rel, new_html, enc, nl):
                if entry and act in ("insert", "replace", "removed"):
                    entry, act = {"a": "error", "url": "", "new": "", "old": "", "note": "could not write the file"}, "error"
                lchanges = []
            if act == "removed":
                cnt["removed"] += 1
                self.log(f"  {rel}: {K.KIND_TEXT[hit.kind]} — canonical tag removed", "muted")
            elif act in ("insert", "replace"):
                cnt[act] += 1
                self.canon_dec[rel] = dec
                self.log(f"  {rel}: canonical {'added' if act == 'insert' else 'fixed'} → {dec.url}"
                         + (f"   ({dec.note})" if dec.note else ""), "dry" if self.dry else "ok")
            elif act == "ok":
                cnt["ok"] += 1
            elif act == "keep_foreign":
                cnt["keep_foreign"] += 1
                self.log(f"  {rel}: {dec.note}", "warn")
            elif act == "error":
                cnt["error"] += 1
            elif act in ("nohead",):
                cnt["error"] += 1
                self.log(f"  {rel}: canonical not added — {dec.note}", "warn")
            if entry is not None:
                self.canon[rel] = entry
                if self.state is not None and entry.get("a") in ("insert", "replace"):
                    self.state.data.setdefault("canon", {})[rel] = entry
                    self.state._dirty += 1
            if lchanges:
                cnt["links_fixed"] += len(lchanges)
                cnt["link_pages"] += 1
                self.link_changes += lchanges
                self.log(f"  {rel}: {len(lchanges)} link(s) now point straight at the final page", "dry" if self.dry else "ok")
            for f in lfind:
                cnt["links_gone" if "(410)" in f.issue else "links_other"] += 1
            self.link_findings += lfind
        if self.state is not None:
            self.state.save()
        self.canon_counts = cnt
        word = "would be " if self.dry else ""
        self.log(f"Canonical tags: {cnt['insert']} {word}added, {cnt['replace']} {word}fixed, {cnt['ok']} already right, "
                 f"{cnt['keep_foreign']} point to another domain (kept), {cnt['removed']} {word}removed from "
                 f"redirected / gone pages. Left out: {cnt['redirected']} redirected / gone pages "
                 f"(301: {cnt['301']}, 302: {cnt['302']}, 410: {cnt['410']}).", "ok")
        if links:
            self.log(f"Internal links: {cnt['links_fixed']} {word}fixed on {cnt['link_pages']} page(s); "
                     f"{cnt['links_gone']} link(s) to gone (410) pages and {cnt['links_other']} other link(s) "
                     f"need a look.", "ok")
            self._write_links_csv()

    def _write_links_csv(self):
        """seo_links_<time>.csv in the scanned folder: every link that was (or would be) changed + everything to review."""
        if not (self.link_changes or self.link_findings):
            return
        import csv
        path = self.root / f"seo_links_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["result", "page", "link", "new_link", "note"])
                for c in self.link_changes:
                    w.writerow(["would change" if self.dry else "changed", c.page, c.old, c.new, c.rule])
                for x in self.link_findings:
                    w.writerow(["review", x.page, x.href, "", x.issue])
            self.links_csv = str(path)
            self.log(f"Link list saved → {path}", "ok")
        except OSError as e:
            self.log(f"Could not save the link list: {e}", "warn")

    def canonical_stats(self, todo: list) -> dict:
        """Numbers for the "canonical tags done" window."""
        c = dict(self.canon_counts)
        c.update(checked=len(todo), dry=self.dry, next_pages=max(0, len(todo) - len(self.redirected)),
                 tags=self.o.do_canonical)
        return c

    def _canon_write(self, fp: str, rel: str, html: str, enc: str, nl: str) -> bool:
        """Dry run: nothing is written (True). Live run: backup first, then an atomic write."""
        if self.dry:
            return True
        try:
            if self.o.backup and self.state and self.state.backup_dir:
                C.backup_file(fp, self.root, self.state.backup_dir)
            C.write_html(fp, html, enc, nl)
            return True
        except Exception as e:                                   # noqa: BLE001
            self.log(f"  {rel}: could not write the canonical tag — {e}", "err")
            return False

    def _redirect_reason(self, rule) -> str:
        return f"{K.KIND_TEXT[rule.kind]} in .htaccess (line {rule.line}: {rule.raw[:70]})"

    def load_registry_from_state(self):
        """Every finished page's title / description / H1 goes into the uniqueness registry."""
        if self.state is None:
            return
        for e in self.state.files.values():
            if e.get("status") == "done" and e.get("title"):
                self.reg.add(e["title"], e.get("core", e["title"]), e.get("desc", ""), e.get("h1", ""), e.get("ctx", ""))

    # ── review before writing ────────────────────────────────────────────────
    def review_session(self):
        """Call once before reviewing: loads what earlier steps decided (uniqueness registry, canonical edits)."""
        self.load_registry_from_state()
        if self.state is not None:
            self.canon = dict(self.state.data.get("canon", {}))

    def review_open(self, rel: str):
        """(prep, error): the page prepared exactly like in a normal run — needed to preview, regenerate and write."""
        fp = self.root / rel
        if not fp.is_file():
            return None, "the file no longer exists"
        p = self._prepare(str(fp))
        if isinstance(p, dict) and p.get("status") in ("skipped", "failed"):
            return None, p.get("reason") or p.get("error") or "this page cannot be reviewed"
        return p, ""

    def review_check(self, p: dict, title_text: str, desc: str, h1_text: str):
        """Turns what the reviewer typed into field values and checks them. Returns (vals, {field: [problems]})."""
        o = self.o
        core = C.strip_brand(C.clean_text(title_text), o.brand) if title_text.strip() else ""
        desc = C.clean_text(desc)
        h1 = C.clean_text(h1_text)
        probs = {"title": A.validate_field("title", core, self.cfg, self.reg) if core else ["the title is empty"],
                 "desc": A.validate_field("desc", desc, self.cfg, self.reg),
                 "h1": A.validate_field("h1", h1, self.cfg, self.reg) if (o.do_h1 and h1) else []}
        rv = p["res"].get("review") or {}
        vals = {"title": core or None, "desc": desc, "h1": h1 if (o.do_h1 and h1) else None,
                "schema_ai": rv.get("schema_ai") or {"schema_type": "WebPage"}}
        return vals, probs

    def review_preview(self, p: dict, vals: dict):
        """(html, info, score) of the page with these values — nothing is written."""
        html, info = self._build(p, vals)
        return html, info, self._score_html(html, p)

    def review_regen(self, p: dict, field: str, vals: dict, problems: list, instruction: str = ""):
        """A new version of ONE field. Returns (text or None, problems). Costs one small AI call."""
        res = p["res"]
        original = {"title": res["old_title"], "desc": res["old_description"], "h1": p["h1"]}
        current = {"title": vals.get("title") or "", "desc": vals.get("desc") or "", "h1": vals.get("h1") or ""}
        locked = {f: vals[f] for f in ("title", "desc", "h1") if f != field and vals.get(f)}
        topic_words = [S._ORIG.get(t, t) for t in p["topic"]][:8]
        return A.regenerate_for_review(self.ai, self.cfg, self.reg, p["ai_page"], field, original, current, locked,
                                       problems, instruction, topic_words, [p["tokens"]])

    def review_apply(self, p: dict, vals: dict) -> dict:
        """Writes the page with the approved values (backup first; a dry run only previews). Returns the new result."""
        o, res, rel, fp = self.o, p["res"], p["rel"], p["fp"]
        html, info = self._build(p, vals)
        blob = self._score_html(html, p) if (o.do_score and res.get("score_before")) else None
        title_eff, desc_eff, h1_eff = info["title_eff"], info["desc_eff"], info["h1_eff"]
        res.update(new_title=title_eff, new_description=desc_eff, new_h1=h1_eff, title_len=len(title_eff),
                   desc_len=len(desc_eff), reason="", error="")
        for k in ("title_changed", "description_changed", "h1_changed", "keywords_removed", "robots_added",
                  "og_changed", "lazy_added", "alts_added", "h1_demoted"):
            res[k] = info[k]
        res["breadcrumb_added"] = info.get("breadcrumb") == "added"
        res["faq_items"] = info.get("faq", 0)
        res["notes"].extend(info["notes"])
        res["notes"].append("approved after review")
        if info.get("img"):
            res["image_url"], res["image_source"] = info["img"]["url"], info["img"]["source"]
        if info.get("schema"):
            res.update(schema_type=info["schema"]["type"], schema_action=info["schema"]["action"],
                       schema_json=info["schema"]["node"])
        if blob is not None:
            res["score_after"] = blob
            base = p.get("gate") or res.get("score_before")
            if base and blob["t"] < base["t"] - 0.005:
                res["notes"].append("score is lower than before — approved by you")
        if not self.dry:
            if o.backup and self.state and self.state.backup_dir:
                C.backup_file(fp, self.root, self.state.backup_dir)
            C.write_html(fp, html, p["enc"], p["nl"])
        res["status"] = "preview" if self.dry else "updated"
        res["cost"] = p["ai_page"].get("_cost", 0.0)
        core_eff = vals.get("title") or C.strip_brand(title_eff, o.brand)
        self.reg.add(title_eff, core_eff, desc_eff, h1_eff, p["ctx"])
        self.log(f"{'[DRY] ' if self.dry else ''}{rel}: approved after review", "dry" if self.dry else "ok")
        self._record(rel, res, "done", core=core_eff, ctx=p["ctx"], write=True)
        self.results.append(res)
        return res

    def refresh_scores(self):
        """(items, summary) from the progress file + the pages approved in this review session (no AI)."""
        items = self._collect_score_items()
        if not items:
            return [], None
        S.apply_site_checks(items)
        use_ai = any(it["before"].get("ai") for it in items)
        summary = S.aggregate(items, self.o.score_target, use_ai)
        for it in items:
            if it.get("result") is not None:
                it["result"]["score_before"], it["result"]["score_after"] = it["before"], it["after"]
        self.score_items, self.score_summary = items, summary
        return items, summary

    def _emit(self, r: dict):
        self.results.append(r)
        self.on_result(r)

    # ── scoring: cross-page checks, optional deep check, summary ─────────────
    def _collect_score_items(self) -> list:
        """Pages from earlier sessions (saved state) + this session, newest wins."""
        items = {}
        if self.state is not None:
            for rel, e in self.state.files.items():
                sb = S.expand(e.get("sb")) if e.get("sb") else None
                if not sb:
                    continue
                sa = None
                if e.get("status") == "done":
                    fp = str(self.root / rel)
                    try:
                        valid = C.file_sha1(fp) == e.get("sha_after")
                    except OSError:
                        valid = False
                    if not valid or not e.get("sa"):
                        continue                                  # file changed since / no score — stale, ignore
                    sa = S.expand(e["sa"])
                items[rel] = {"rel": rel, "before": sb, "after": sa, "chg": e.get("ch") or {},
                              "old_title": e.get("old_title", ""),
                              "new_title": e.get("title", ""), "old_description": e.get("old_desc", ""),
                              "new_description": e.get("desc", ""), "result": None}
        for r in self.results:
            if r.get("score_before"):
                items[r["rel"]] = {"rel": r["rel"], "before": r["score_before"], "after": r.get("score_after"),
                                   "chg": {"h": int(bool(r.get("h1_changed"))), "s": r.get("schema_action", ""),
                                           "o": int(bool(r.get("og_changed"))),
                                           "i": r.get("lazy_added", 0) + r.get("alts_added", 0),
                                           "n": r.get("page_name", ""), "oh": r.get("old_h1", ""),
                                           "nh": r.get("new_h1", ""), "c": r.get("canonical_action", ""),
                                           "d": r.get("h1_demoted", 0), "b": int(bool(r.get("breadcrumb_added"))),
                                           "f": r.get("faq_items", 0)},
                                   "old_title": r["old_title"], "new_title": r["new_title"],
                                   "old_description": r["old_description"], "new_description": r["new_description"],
                                   "result": r}
        return list(items.values())

    def _finish_scores(self):
        o = self.o
        try:
            items = self._collect_score_items()
            if not items:
                return
            S.apply_site_checks(items)
            use_ai = False
            if o.deep_check:
                use_ai = self._deep_check(items)
                S.apply_site_checks(items)
            self.score_items = items
            self.score_summary = S.aggregate(items, o.score_target, use_ai)
            for it in items:                                      # keep the table's result objects in sync
                if it.get("result") is not None:
                    it["result"]["score_before"], it["result"]["score_after"] = it["before"], it["after"]
            if self.on_scores:
                self.on_scores(items, self.score_summary)
            if self.state is not None:
                self.state.save()
        except A.FatalAPIError:
            raise
        except Exception as e:                                    # noqa: BLE001
            self.log(f"Scoring could not be completed: {e}", "warn")

    MAX_DEEP_PAGES = 1500

    def _deep_check(self, items: list) -> bool:
        """AI-judged points for pages scored in THIS session. Returns True if any page carries AI marks.
        Never raises: on any problem the free scores stay valid and the page is simply not judged."""
        o = self.o
        model = o.score_model or o.model
        already = sum(1 for it in items if it["before"].get("ai"))
        targets = [it for it in items if it.get("result") and it["result"].get("view_before")
                   and it["result"].get("view_after") and it["before"] and it["after"] and not it["before"].get("ai")]
        if not targets:
            return already > 0
        if len(targets) > self.MAX_DEEP_PAGES:
            step = len(targets) / self.MAX_DEEP_PAGES
            targets = [targets[int(i * step)] for i in range(self.MAX_DEEP_PAGES)]
            self.log(f"Deep check limited to {self.MAX_DEEP_PAGES} evenly spread pages.", "warn")
        views = []
        for k, it in enumerate(targets):
            views.append((k, "b", it["result"]["view_before"]))
            views.append((k, "a", it["result"]["view_after"]))
        random.Random(7).shuffle(views)                           # blind: the judge cannot tell before from after
        self.log(f"Deep quality check: judging {len(views)} page versions with {model}…", "info")
        before_cost = self.ai.cost
        results = {}
        for i in range(0, len(views), 10):
            if self.stop.is_set():
                self.log("Deep check stopped — pages judged so far keep their AI marks.", "warn")
                break
            batch = views[i:i + 10]
            try:
                judged = A.judge_pages(self.ai, self.cfg, [v[2] for v in batch], model)
            except A.FatalAPIError as e:
                self.log(f"Deep check stopped — {e}. Free scores are unaffected.", "err")
                break
            except Exception as e:                                # noqa: BLE001
                self.log(f"  deep check batch failed ({e}) — those pages keep free scores", "warn")
                continue
            for v, res in zip(batch, judged):
                if res:
                    results[(v[0], v[1])] = res
        n = 0
        for k, it in enumerate(targets):
            rb, ra = results.get((k, "b")), results.get((k, "a"))
            if not (rb and ra):
                continue
            for blob, res in ((it["before"], rb), (it["after"], ra)):
                S.ai_fill(blob, res["marks"])
                if res["issue"]:
                    worst = min(res["marks"], key=res["marks"].get)
                    if res["marks"][worst] < 0.8:
                        blob.setdefault("r", {})[worst] = res["issue"]
            n += 1
            if self.state is not None:
                e = self.state.files.get(it["rel"])
                if e is not None:
                    e["sb"], e["sa"] = S.compact(it["before"]), S.compact(it["after"])
                    self.state._dirty += 1
        self.log(f"Deep quality check done — {n} pages judged, cost ${self.ai.cost - before_cost:.4f}.", "ok")
        return n > 0 or already > 0

    # ── selection ────────────────────────────────────────────────────────────
    def _select_todo(self) -> list:
        o = self.o
        todo = []
        for fp in self.files:
            rel = C.rel_posix(fp, self.root)
            if o.run_kind == "fresh" or self.state is None:
                todo.append(fp)
                continue
            e = self.state.files.get(rel)
            if o.run_kind == "retry":
                if e and e.get("status") in ("failed", "review"):
                    todo.append(fp)
                continue
            # resume: skip anything already handled, unless the file changed since
            if e and e.get("status") in ("done", "failed", "review"):
                try:
                    if e.get("status") != "done" or C.file_sha1(fp) == e.get("sha_after"):
                        continue
                except OSError:
                    pass
            todo.append(fp)
        return todo

    # ── chunk processing ─────────────────────────────────────────────────────
    def _process_chunk(self, chunk: list) -> None:
        preps = []
        for fp in chunk:
            r = self._prepare(fp)
            if isinstance(r, dict) and "status" in r and r["status"] in ("skipped", "failed"):
                self._emit(r)
            else:
                preps.append(r)
        if not preps:
            return

        batch = len(preps) > 1
        for p in preps:
            p["ai_page"]["body_chars"] = A.BATCH_BODY_CHARS if batch else A.SINGLE_BODY_CHARS
        try:
            items = A.generate(self.ai, self.cfg, self.reg, [p["ai_page"] for p in preps],
                               [p["tokens"] for p in preps])
        except A.FatalAPIError:
            raise
        except Exception as e:                                   # noqa: BLE001
            self.log(f"AI error: {e}", "err")
            items = [None] * len(preps)
            for p in preps:
                p["res"]["error"] = f"AI error: {e}"

        for p, item in zip(preps, items):
            if self.stop.is_set() and item is None:
                continue
            if item is None and not p["res"]["error"]:
                try:                                              # one targeted retry for a missing page
                    again = A.generate(self.ai, self.cfg, self.reg, [p["ai_page"]], [p["tokens"]])
                    item = again[0]
                except A.FatalAPIError:
                    raise
                except Exception as e:                            # noqa: BLE001
                    p["res"]["error"] = f"AI error: {e}"
            self._emit(self._finish(p, item))

    # ── step 1: read + analyse a file ────────────────────────────────────────
    def _prepare(self, fp: str):
        o = self.o
        rel = C.rel_posix(fp, self.root)
        res = new_result(fp, rel, o.mode)
        if o.excluded_paths and rel in o.excluded_paths or C.matches_patterns(rel, o.exclude_patterns):
            res.update(status="skipped", reason="excluded by you")
            return res
        hit = self.redirected.get(rel) if o.skip_htaccess else None
        if hit:                                                  # 301 / 302 / 410 in .htaccess: not worth optimising
            canon = self.canon.get(rel)
            res.update(status="skipped", reason=self._redirect_reason(hit),
                       canonical_action=K.ACTION_TEXT.get(canon["a"], "") if canon else "")
            return res
        try:
            html, enc, nl = C.read_html(fp)
        except Exception as e:                                   # noqa: BLE001
            res.update(status="failed", error=f"Cannot read file safely: {e}")
            self.log(f"Skipped (unreadable): {rel} — {e}", "err")
            self._record(rel, res, "failed")
            return res
        # step 1 may already have changed the canonical tag. `html` is the page as step 2 sees it (with the
        # canonical); `html_orig` is the page as it was, so the report's "before" score shows the canonical gain.
        html_orig = html
        canon = self.canon.get(rel)
        if canon and canon.get("a") in ("insert", "replace"):
            if self.dry:
                dec = self.canon_dec.get(rel)
                if dec is not None:
                    html = K.apply_decision(html, dec)
            else:
                html_orig = K.undo(html, canon)
        if canon:
            res["canonical_action"] = K.ACTION_TEXT.get(canon["a"], "")
        reason = C.classify_skip(html, rel, o.skip_partial, o.skip_noindex, o.skip_redirect, o.skip_error)
        if reason:
            res.update(status="skipped", reason=reason)
            return res

        content = C.extract_content(html)
        page_name = C.page_name_from_path(fp, self.root)
        folder = C.folder_context_from_path(fp, self.root)
        old_title, old_desc = C.read_title(html), C.read_meta(html, "description")
        raw_h1s = [C.strip_tags(html[a:b]) for _, a, b in C.find_h1s(html, C.Mask(html))]
        h1 = raw_h1s[0] if raw_h1s else ""
        canonical = C.get_canonical(html, o.site_url)

        res.update(page_name=page_name, folder_context=folder, old_title=old_title,
                   old_description=old_desc, old_h1=h1, canonical=canonical)
        demote_keep = None
        if len(raw_h1s) > 1 and o.fix_multi_h1:                  # keep the best H1, the others become H2 (in _build)
            demote_keep = ST.choose_h1(html, old_title, page_name)[0]
            h1 = raw_h1s[demote_keep]
            res["old_h1"] = h1
        elif len(raw_h1s) > 1:
            res["notes"].append(f"{len(raw_h1s)} H1 tags on page (not changed)")
        if canon and canon.get("a") == "keep_foreign":
            res["notes"].append(canon.get("note") or "canonical points to another domain — kept")
        if not canonical:
            res["notes"].append("no canonical tag — og:url and schema url skipped")

        similar = self.dups.check_and_add(rel, content["text"])
        style = ""
        if similar:
            res["similar_to"] = similar[0]
            res["notes"].append(f"near-duplicate content of {similar[0]} ({similar[1]:.0%}) — needs unique page copy")
            style = A.OPENING_STYLES[self.dup_counter % len(A.OPENING_STYLES)]
            self.dup_counter += 1

        ctx = C.page_context(page_name, folder, canonical, h1)
        topic, noindex_before, gate = [], False, None
        if o.do_score:
            topic = S.topic_terms(page_name, folder, h1, content["headings"])
            noindex_before = "noindex" in C.read_meta(html, "robots").lower()
            if o.deep_check:                      # small snapshot for the AI judge (not stored in the state file)
                res["view_before"] = {"page": rel, "title": old_title, "desc": old_desc, "h1": h1,
                                      "schema": S.schema_summary(html), "excerpt": content["text"][:500]}
            try:
                score = lambda page: S.score_page(
                    page, path=fp, root=self.root, site_url=o.site_url, brand=o.brand, biz=self.facts,
                    topic=topic, intended_noindex=noindex_before, dup_similar=bool(similar))
                res["score_before"] = score(html_orig)           # the report compares with the ORIGINAL page
                if html_orig != html:                            # step 1 changed the canonical: the protection's
                    gate = score(html)                           # baseline is the page WITH it
            except Exception as e:                                # noqa: BLE001
                self.log(f"  score (before) failed for {rel}: {e}", "warn")
        return {
            "fp": fp, "rel": rel, "html": html, "enc": enc, "nl": nl, "res": res, "ctx": ctx, "gate": gate,
            "topic": topic, "noindex_before": noindex_before, "dup_similar": bool(similar),
            "tokens": C.context_tokens(ctx), "h1": h1, "multi_h1": len(raw_h1s) > 1 and demote_keep is None,
            "demote_keep": demote_keep,
            "canonical": canonical, "content": content,
            "ai_page": {"page_name": page_name, "url": canonical, "folder": folder, "h1": h1,
                        "old_title": old_title, "content": content["text"],
                        "headings": content["headings"], "style_hint": style, "body_chars": 600},
        }

    # ── step 2: validate, protect the score, write ───────────────────────────
    def _finish(self, p: dict, item):
        o, res, rel, fp = self.o, p["res"], p["rel"], p["fp"]
        pg = p["ai_page"]
        if item is None:
            res.update(status="failed", error=res["error"] or "AI returned no usable result", score_before=None)
            res["cost"] = pg.get("_cost", 0.0)
            self.log(f"FAILED: {rel} — {res['error']}", "err")
            self._record(rel, res, "failed")
            return res

        cand = A.candidate_from_item(item, self.cfg, p["h1"])
        cand, probs = A.refine(self.ai, self.cfg, self.reg, pg, cand, [p["tokens"]])
        if probs:
            title = C.build_title(cand["core"], o.sep, o.brand)
            res.update(new_title=title, new_description=cand["desc"], new_h1=cand["h1"],
                       title_len=len(title), desc_len=len(cand["desc"]))
            res.update(status="review", reason="; ".join(probs)[:400])
            res["cost"] = pg.get("_cost", 0.0)
            res["notes"].append("needs review — not written")
            res["review"] = {"core": cand["core"], "desc": cand["desc"], "h1": cand["h1"],
                             "schema_ai": cand["schema_ai"], "problems": list(probs)[:8]}
            self.log(f"NEEDS REVIEW: {rel} — {probs[0][:100]}", "warn")
            self._record(rel, res, "review")
            return res

        values = {"title": cand["core"], "desc": cand["desc"],
                  "h1": cand["h1"] if (o.do_h1 and cand["h1"]) else None, "schema_ai": cand["schema_ai"]}
        decision = None
        if o.do_score and o.protect_score and res.get("score_before"):
            decision = self._protect(p, values)          # never lets the page end up with a lower score
            values = decision["values"]

        orig = p["html"]
        if decision and decision["kept"]:
            html, info, blob = orig, self._info_unchanged(p), copy.deepcopy(p.get("gate") or res["score_before"])
        elif decision:
            html, info, blob = decision["html"], decision["info"], decision["blob"]
        else:
            html, info = self._build(p, values)
            blob = self._score_html(html, p) if (o.do_score and res.get("score_before")) else None

        # what is really on the page now (an untouched field keeps its original text)
        title_eff = info["title_eff"]
        desc_eff, h1_eff = info["desc_eff"], info["h1_eff"]
        res.update(new_title=title_eff, new_description=desc_eff, new_h1=h1_eff,
                   title_len=len(title_eff), desc_len=len(desc_eff))
        for k in ("title_changed", "description_changed", "h1_changed", "keywords_removed", "robots_added",
                  "og_changed", "lazy_added", "alts_added", "h1_demoted"):
            res[k] = info[k]
        res["breadcrumb_added"] = info.get("breadcrumb") == "added"
        res["faq_items"] = info.get("faq", 0)
        res["notes"].extend(info["notes"])
        if info.get("img"):
            res["image_url"], res["image_source"] = info["img"]["url"], info["img"]["source"]
        if info.get("schema"):
            res.update(schema_type=info["schema"]["type"], schema_action=info["schema"]["action"],
                       schema_json=info["schema"]["node"])
        if decision:
            res["attempts"] = decision["attempts"]
            res["protected"] = bool(decision["kept"] or decision["reverted"] or decision["attempts"] > 1)   # protection stepped in
            res["retry_note"] = decision["note"]
        if o.do_score and res.get("score_before"):
            res["score_after"] = blob
            if o.deep_check and res.get("view_before"):
                res["view_after"] = {"page": rel, "title": title_eff, "desc": desc_eff, "h1": h1_eff or p["h1"],
                                     "schema": S.schema_summary(html), "excerpt": res["view_before"]["excerpt"]}

        core_eff = values["title"] if values.get("title") else C.strip_brand(title_eff, o.brand)
        self.reg.add(title_eff, core_eff, desc_eff, h1_eff, p["ctx"])

        changed = html != orig
        if not self.dry and changed:
            try:
                if o.backup and self.state and self.state.backup_dir:
                    C.backup_file(fp, self.root, self.state.backup_dir)
                C.write_html(fp, html, p["enc"], p["nl"])
            except Exception as e:                               # noqa: BLE001
                res.update(status="failed", error=f"Write error: {e}", score_before=None, score_after=None)
                self.log(f"Write error: {rel} — {e}", "err")
                self._record(rel, res, "failed")
                return res
        kept = bool(decision and (decision["kept"] or not changed))     # nothing differs from the page as it was
        canon_changed = res.get("canonical_action") in ("added", "fixed")
        if kept and canon_changed:                                       # step 1 did write the canonical tag
            kept = False
            res["notes"].append("meta data left as it was (score protection) — canonical tag added")
        res["status"] = "kept" if kept else ("preview" if self.dry else "updated")
        if not changed and not kept and not canon_changed:
            res["notes"].append("already optimal — no changes")
        res["cost"] = pg.get("_cost", 0.0)
        self.log(f"{'[DRY] ' if self.dry else ''}{rel}  title={len(title_eff)}ch  desc={len(desc_eff)}ch  "
                 f"schema={res['schema_type'] or '-'}/{res['schema_action'] or '-'}"
                 + (f"  [{res['retry_note']}]" if res.get("retry_note") else ""),
                 "dry" if self.dry else "ok")
        self._record(rel, res, "done", core=core_eff, ctx=p["ctx"], write=changed)
        return res

    # ── building the page from a chosen set of field values (pure: nothing is written) ───────────────
    @staticmethod
    def _info_unchanged(p: dict = None) -> dict:
        info = {"notes": [], "title_changed": False, "description_changed": False, "h1_changed": False,
                "keywords_removed": 0, "robots_added": False, "og_changed": False, "lazy_added": 0,
                "alts_added": 0, "img": None, "schema": None, "title_eff": "", "desc_eff": "", "h1_eff": "",
                "h1_demoted": 0, "breadcrumb": "", "faq": 0}
        if p is not None:                                         # an untouched page keeps its own text
            info.update(title_eff=p["res"]["old_title"], desc_eff=p["res"]["old_description"], h1_eff=p["h1"])
        return info

    def _build(self, p: dict, vals: dict):
        """Apply the field values to the ORIGINAL page. A value of None means "leave that field as it is".
        Returns (html, info). Safe to call many times for the same page."""
        o, res = self.o, p["res"]
        html = p["html"]
        info = self._info_unchanged()
        notes = info["notes"]
        old_title, old_desc, old_h1 = res["old_title"], res["old_description"], p["h1"]
        if p.get("demote_keep") is not None:                      # one clear H1 per page
            html, info["h1_demoted"] = ST.demote_extra_h1(html, p["demote_keep"])
            if info["h1_demoted"]:
                notes.append(f"{info['h1_demoted']} extra H1 tag(s) changed to H2")

        title_new = C.build_title(vals["title"], o.sep, o.brand) if vals.get("title") else None
        title_eff = title_new if title_new is not None else old_title
        desc_eff = vals["desc"] if vals.get("desc") is not None else old_desc
        h1_eff = vals["h1"] if vals.get("h1") else old_h1
        info.update(title_eff=title_eff, desc_eff=desc_eff, h1_eff=h1_eff)

        if title_new is not None:
            html, info["title_changed"] = C.set_title(html, title_new)
        if vals.get("desc") is not None:
            html, info["description_changed"] = C.upsert_metas(
                html, [{"key": "description", "content": vals["desc"], "attr": "name"}])
        if o.do_h1 and vals.get("h1"):
            html, info["h1_changed"], issue = C.set_h1(html, vals["h1"])
            if issue == "missing":
                notes.append("page has no H1 — add one manually")
            elif issue == "no_text":
                notes.append("H1 contains no text — not changed")
        html, info["keywords_removed"] = C.remove_metas(html, "keywords")
        if o.do_robots:
            html, info["robots_added"] = C.upsert_metas(
                html, [{"key": "robots", "content": "index, follow", "attr": "name", "only_if_missing": True}])

        img = None
        if o.do_og or o.do_schema:
            if "img" not in p:                                   # same answer for every variant of this page
                p["img"] = C.pick_page_image(p["html"], p["fp"], self.root, o.site_url, o.default_image)
            img = p["img"]
            info["img"] = img
            if not img:
                notes.append("no usable share image found")

        schema_type = vals["schema_ai"].get("schema_type", "WebPage")
        if o.do_schema:
            node, stype, sissues = C.build_schema(
                vals["schema_ai"], self.facts, p["canonical"], o.site_url, img["url"] if img else "",
                desc_eff, h1_eff or old_h1, title_eff, C.find_published_date(p["html"]))
            notes.extend(sissues)
            html, action = C.apply_schema(html, node)
            info["schema"] = {"type": stype, "action": action, "node": node}
            if action == "skipped_unparseable":
                notes.append("existing JSON-LD has a syntax error — schema left untouched")
            schema_type = stype

        if o.do_breadcrumb or o.do_faq:
            html = self._extra_schema(p, html, info, h1_eff or old_h1)

        if p["canonical"] and res.get("canonical_action") in ("added", "fixed"):
            html, moved = C.align_schema_urls(html, p["canonical"])      # step 1 changed the canonical: keep schema in line
            if moved:
                notes.append("schema url set to the canonical URL")

        if o.do_og:
            items = []
            if title_eff:
                items.append({"key": "og:title", "content": title_eff, "attr": "property"})
            if desc_eff:
                items.append({"key": "og:description", "content": desc_eff, "attr": "property"})
            items.append({"key": "og:type", "content": "article" if schema_type == "Article" else "website",
                          "attr": "property", "only_if_missing": True})
            if o.brand:
                items.append({"key": "og:site_name", "content": o.brand, "attr": "property"})
            if p["canonical"]:
                items.append({"key": "og:url", "content": p["canonical"], "attr": "property"})
            if img:
                items.append({"key": "og:image", "content": img["url"], "attr": "property"})
                if img.get("width") and img.get("height"):
                    items.append({"key": "og:image:width", "content": str(img["width"]), "attr": "property"})
                    items.append({"key": "og:image:height", "content": str(img["height"]), "attr": "property"})
            big = bool(img and img.get("width") and img["width"] >= 600)
            items.append({"key": "twitter:card", "content": "summary_large_image" if big else "summary", "attr": "name"})
            if title_eff:
                items.append({"key": "twitter:title", "content": title_eff, "attr": "name"})
            if desc_eff:
                items.append({"key": "twitter:description", "content": desc_eff, "attr": "name"})
            if img:
                items.append({"key": "twitter:image", "content": img["url"], "attr": "name"})
            html, info["og_changed"] = C.upsert_metas(html, items)

        if o.do_lazy or o.do_alt:
            html, info["lazy_added"], info["alts_added"] = self._images(html, p, h1_eff)
        return html, info

    def _score_html(self, html: str, p: dict):
        o = self.o
        try:
            return S.score_page(html, path=p["fp"], root=self.root, site_url=o.site_url, brand=o.brand,
                                biz=self.facts, topic=p["topic"], intended_noindex=p["noindex_before"],
                                dup_similar=p["dup_similar"])
        except Exception as e:                                    # noqa: BLE001
            self.log(f"  score (after) failed for {p['rel']}: {e}", "warn")
            return None

    # ── score protection ─────────────────────────────────────────────────────
    def _protect(self, p: dict, values: dict) -> dict:
        """Make sure the page does not end up with a LOWER score than it had.

        Fetch 1 is already done (`values`). If the page would score lower, only the fields that hurt are
        fetched again (up to `max_retries` times) while the fields that helped stay locked. After the last
        try, the best combination of new/original field values is chosen; if even that scores lower than the
        original, the page is left completely unchanged."""
        o, res = self.o, p["res"]
        sb = p.get("gate") or res["score_before"]            # the page as step 1 left it (with its canonical)
        before_t = sb["t"]
        EPS = 0.005

        def deficit(blob):
            """How far the new version falls short of the original: the total AND every category must be >= before
            (so 'Content relevance & consistency' is protected the same way as title, description and H1)."""
            if not blob:
                return None
            d = max(0.0, before_t - blob["t"])
            for c, v in sb["cats"].items():
                d += max(0.0, v - blob["cats"].get(c, 0.0))
            return 0.0 if d < EPS else d

        fields = []
        new = {}
        if values.get("title") and C.build_title(values["title"], o.sep, o.brand) != res["old_title"]:
            fields.append("title"); new["title"] = values["title"]
        if values.get("desc") is not None and values["desc"] != res["old_description"]:
            fields.append("desc"); new["desc"] = values["desc"]
        if values.get("h1") and values["h1"] != p["h1"] and not p["multi_h1"] and p["h1"]:
            fields.append("h1"); new["h1"] = values["h1"]
        schema_ai = values["schema_ai"]
        cands = {f: [new[f]] for f in fields}                    # every value ever fetched, per field
        cache = {}

        def full(state):
            return {"title": state.get("title"), "desc": state.get("desc"), "h1": state.get("h1"),
                    "schema_ai": schema_ai}

        def ev(state):
            key = tuple(state.get(f) for f in fields)
            if key not in cache:
                html, info = self._build(p, full(state))
                blob = self._score_html(html, p)
                cache[key] = (deficit(blob), html, info, blob, dict(state))
            return cache[key]

        def result(state, attempts, note=""):
            t, html, info, blob, st = ev(state)
            reverted = [f for f in fields if st.get(f) is None]
            return {"values": full(st), "html": html, "info": info, "blob": blob, "attempts": attempts,
                    "kept": False, "reverted": reverted, "note": note}

        state = dict(new)
        attempts, retries_left = 1, max(0, int(o.max_retries))
        while True:
            t = ev(state)[0]
            if t is None or t <= 0:                               # nothing got worse (or unscored) = fine
                note = "" if attempts == 1 else f"fetched {attempts}×, no score went down"
                if attempts > 1 and ev(state)[3]:
                    self.log(f"  {p['rel']}: score protected after {attempts} fetch(es) "
                             f"({before_t:.2f} → {ev(state)[3]['t']:.2f})", "info")
                return result(state, attempts, note)
            if retries_left <= 0 or self.stop.is_set():
                break
            bad = [f for f in fields if state.get(f) is not None
                   and (ev({**state, f: None})[0] if ev({**state, f: None})[0] is not None else 1e9) < t]  # putting the original back helps
            if not bad:
                break
            # keep what helps: continue from the best combination seen so far, and re-fetch only the bad fields
            best_key = min(cache, key=lambda k: (cache[k][0] if cache[k][0] is not None else 1e9,
                                                 -sum(1 for v in k if v is not None)))
            base = dict(cache[best_key][4])
            self.log(f"  {p['rel']}: a score would drop ({before_t:.2f} → {ev(state)[3]['t']:.2f}) — "
                     f"re-fetching {', '.join(bad)} (retry {attempts}/{o.max_retries})", "warn")
            newv = self._regen(p, bad, base, values, ev(state)[3], cands)
            retries_left -= 1
            attempts += 1
            state = dict(base)
            for f in bad:
                if f in newv:
                    state[f] = newv[f]
                    cands[f].append(newv[f])
                else:
                    state[f] = None                                  # nothing valid came back: the original stays
        # no combination reached the before score within the retries → pick the best partial update
        choices = [[None] + cands[f][-3:] for f in fields]
        best = None
        for combo in itertools.product(*choices):
            st = dict(zip(fields, combo))
            dd = ev(st)
            if dd[0] is None or dd[0] > 0:
                continue
            rank = (dd[3]["t"], sum(1 for v in combo if v is not None))
            if best is None or rank >= best[0]:
                best = (rank, st)
        if best is not None:
            r = result(best[1], attempts)
            kept_fields = ", ".join({"title": "title", "desc": "description", "h1": "H1"}[f] for f in r["reverted"])
            r["note"] = (f"{attempts} fetch(es) — kept the original {kept_fields} (new text scored lower)"
                         if r["reverted"] else f"fetched {attempts}×, no score went down")
            self.log(f"  {p['rel']}: partial update — original {kept_fields or 'nothing'} kept", "info")
            return r
        self.log(f"  {p['rel']}: no version kept every score at or above the original — page left unchanged", "warn")
        return {"values": full({}), "html": p["html"], "info": self._info_unchanged(p), "blob": None,
                "attempts": attempts, "kept": True, "reverted": list(fields),
                "note": f"{attempts} fetch(es) — every version scored lower, so the page was left unchanged"}

    def _regen(self, p: dict, bad: list, base: dict, values: dict, cur_blob: dict, cands: dict) -> dict:
        """One AI call that rewrites only the fields in `bad`, with the reasons they scored lower."""
        res, o = p["res"], self.o
        original = {"title": res["old_title"], "desc": res["old_description"], "h1": p["h1"]}
        last = {f: (cands[f][-1] if cands.get(f) else original[f]) for f in bad}
        locked = {f: base[f] for f in ("title", "desc", "h1") if f not in bad and base.get(f)}
        feedback = self._feedback(p.get("gate") or res["score_before"], cur_blob, bad)
        topic_words = [S._ORIG.get(t, t) for t in p["topic"]][:8]
        try:
            return A.regenerate_fields(self.ai, self.cfg, self.reg, p["ai_page"], bad, original, last, locked,
                                       feedback, topic_words, [p["tokens"]])
        except A.FatalAPIError:
            raise
        except Exception as e:                                    # noqa: BLE001
            self.log(f"    re-fetch failed: {e}", "warn")
            return {}

    @staticmethod
    def _feedback(before: dict, after: dict, fields: list) -> list:
        """Plain-English reasons the new version scored lower (checks that got worse)."""
        if not after:
            return []
        cats = {"title": {"title"}, "desc": {"desc"}, "h1": {"h1"}}
        want = set().union(*(cats[f] for f in fields)) | {"content", "social", "schema"}
        lines = []
        for i, (cid, cat, label, mx, kind) in enumerate(S.CHECKS):
            b, a = before["f"][i], after["f"][i]
            if cat in want and kind == "A" and b >= 0 and a >= 0 and a < b:
                why = after.get("r", {}).get(cid, "")
                lines.append(f"{label}: {b}% → {a}%" + (f" ({why})" if why else ""))
        return lines[:8]

    # ── breadcrumb + FAQ markup ───────────────────────────────────────────────
    def _folder_info(self, d: str):
        """(name, url) of a folder that has its own index page, else None (cached)."""
        cache = self.__dict__.setdefault("_folders", {})
        if d in cache:
            return cache[d]
        out = None
        for name in ("index.html", "index.htm"):
            fp = self.root / d / name
            if fp.is_file():
                try:
                    html, _, _ = C.read_html(fp)
                    hs = C.find_h1s(html, C.Mask(html))
                    text = C.strip_tags(html[hs[0][1]:hs[0][2]]) if hs else ""
                except Exception:                                 # noqa: BLE001
                    text = ""
                out = (text or ST._titled(d.rsplit("/", 1)[-1]), self.o.site_url.rstrip("/") + "/" + d + "/")
                break
        cache[d] = out
        return out

    def _extra_schema(self, p: dict, html: str, info: dict, page_name: str) -> str:
        o, res = self.o, p["res"]
        if o.do_breadcrumb and o.site_url:
            url = p["canonical"] or K.canonical_url(o.site_url, p["rel"])
            node = ST.build_breadcrumb(o.site_url, p["rel"], url, page_name or res["page_name"], o.brand,
                                       self._folder_info)
            if node:
                html, act = ST.add_node(html, node)
                info["breadcrumb"] = act
                if act == "added":
                    info["notes"].append("breadcrumb markup added")
        if o.do_faq:
            if "faq" not in p:
                p["faq"] = ST.extract_faq(p["html"])
            node = ST.build_faq(p["faq"])
            if node:
                html, act = ST.add_node(html, node)
                if act == "added":
                    info["faq"] = len(p["faq"])
                    info["notes"].append(f"FAQ markup added ({len(p['faq'])} questions)")
        return html

    # ── images ───────────────────────────────────────────────────────────────
    def _alt_map(self, p: dict, imgs: list, h1_eff: str) -> dict:
        """Alt texts are worked out ONCE per page (the AI call is paid once, even if the page is rebuilt)."""
        if "alt_map" in p:
            return p["alt_map"]
        o, res = self.o, p["res"]
        alt_map = {}
        if o.do_alt:
            need_ai = []
            for im in imgs:
                if not C.img_needs_alt(im):
                    continue
                if im.decorative:
                    alt_map[im.index] = ""
                elif im.candidate:
                    alt_map[im.index] = im.candidate
                else:
                    need_ai.append(im)
            if need_ai and o.ai_alt and not self.stop.is_set():
                before = self.ai.cost
                items = [{"i": im.index,
                          "file": os.path.basename((im.attrs.get("src") or im.attrs.get("data-src") or "")
                                                   .split("?")[0]),
                          "context": im.context} for im in need_ai[:25]]
                alt_map.update(A.generate_alts(self.ai, self.cfg, res["page_name"], h1_eff or p["h1"], items))
                p["ai_page"]["_cost"] = p["ai_page"].get("_cost", 0.0) + (self.ai.cost - before)
            for im in need_ai:                                   # fallback: nearby heading / H1 / page name
                if im.index not in alt_map:
                    alt_map[im.index] = im.context or h1_eff or p["h1"] or res["page_name"]
        p["alt_map"] = alt_map
        return alt_map

    def _images(self, html: str, p: dict, h1_eff: str):
        imgs = C.scan_images(html)
        if not imgs:
            return html, 0, 0
        alt_map = self._alt_map(p, imgs, h1_eff)
        return C.apply_images(html, imgs, alt_map, self.o.do_lazy, self.o.do_alt)

    # ── state ────────────────────────────────────────────────────────────────
    def _record(self, rel: str, res: dict, status: str, core: str = "", ctx: str = "", write: bool = True):
        if self.state is None:
            return
        fields = {"status": status, "reason": res.get("reason") or res.get("error", "")}
        if res.get("score_before") and status in ("done", "review"):
            fields.update(sb=S.compact(res["score_before"]), old_title=res.get("old_title", ""),
                          old_desc=res.get("old_description", ""))
            fields["ch"] = {"h": int(bool(res.get("h1_changed"))), "s": res.get("schema_action", ""),
                            "o": int(bool(res.get("og_changed"))), "i": res.get("lazy_added", 0) + res.get("alts_added", 0),
                            "n": res.get("page_name", ""), "oh": res.get("old_h1", ""), "nh": res.get("new_h1", ""),
                            "c": res.get("canonical_action", ""), "d": res.get("h1_demoted", 0),
                            "b": int(bool(res.get("breadcrumb_added"))), "f": res.get("faq_items", 0)}
        if status == "review" and res.get("review"):
            fields["rv"] = res["review"]                          # so the page can be reviewed in a later session
        if status == "done":
            if res.get("status") == "kept":
                fields["kept"] = 1
            if res.get("score_after"):
                fields["sa"] = S.compact(res["score_after"])
            try:
                sha = C.file_sha1(res["file"])
            except OSError:
                sha = ""
            fields.update(title=res["new_title"], core=core, desc=res["new_description"],
                          h1=res["new_h1"], ctx=ctx, sha_after=sha)
        self.state.record(rel, **fields)


def pending_reviews(results, state) -> list:
    """Pages waiting for a decision: this session's "review" results, then earlier sessions (progress file)."""
    out, seen = [], set()
    for r in results:
        if r.get("status") == "review" and r.get("review"):
            out.append({"rel": r["rel"], "review": r["review"], "reason": r.get("reason", "")})
            seen.add(r["rel"])
    if state is not None:
        for rel, e in state.files.items():
            if e.get("status") == "review" and e.get("rv") and rel not in seen:
                out.append({"rel": rel, "review": e["rv"], "reason": e.get("reason", "")})
    return out
