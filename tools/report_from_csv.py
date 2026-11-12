"""
Build a new-style report (and a CSV with score columns) from an OLD results CSV that has no scores.

    python tools/report_from_csv.py "path/to/seo_results_20260622_130323.csv"
        [--client "Example Plumbing"] [--site https://www.example.com] [--out folder]

No AI calls and no website files are needed: the old CSV only holds the old/new title, description and H1 text, so the
scores are ESTIMATES for those three categories only (meta title, meta description, H1). They are weighted up to a
100-point scale (title 30, description 30, H1 40) and the report says so.  Checks that need the page HTML (body text,
canonical, social tags, schema, ...) are not scored.
"""
import csv
import html as _h
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import seo_report as R  # noqa: E402
import seo_score as S  # noqa: E402

APP_VERSION = "5.2.0"
PARTIAL_CATS = ["title", "desc", "h1"]
SCALE = 2.0                       # 15 + 15 + 20 = 50 points  ->  100
USED = {"title.exists", "title.length", "title.topic", "title.unique", "title.natural",
        "desc.exists", "desc.length", "desc.topic", "desc.unique", "desc.sentence",
        "h1.single", "h1.topic", "h1.title"}
NOTE = ("Scores were estimated from the saved results file only (the page files were not available): meta title, "
        "meta description and H1 are scored and weighted to 100 points (title 30, description 30, H1 40). Canonical, "
        "social tags, structured data and body-content checks are not included.")


def _t(x) -> str:
    return " ".join(_h.unescape(str(x or "")).split())


def read_rows(path: Path) -> list:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def normalise(rows: list) -> list:
    """Old and new CSV layouts -> one record shape."""
    has_rel = bool(rows) and "rel" in rows[0]
    files = [r.get("file", "") for r in rows if r.get("file")]
    prefix = ""
    if not has_rel and files:
        parts = [re.split(r"[\\/]", x) for x in files]
        common = []
        for seg in zip(*parts):
            if len(set(seg)) == 1:
                common.append(seg[0])
            else:
                break
        if len(parts) == 1:
            common = common[:-1]                                  # one file: the folder is everything but its name
        prefix = "/".join(common)
    out = []
    for r in rows:
        if has_rel:
            rel = r["rel"]
        else:
            rel = "/".join(re.split(r"[\\/]", r.get("file", "")))
            rel = rel[len(prefix):].lstrip("/") if prefix and rel.startswith(prefix) else rel
        status = (r.get("status") or ("failed" if r.get("error") else "updated")).strip().lower()
        folder = r.get("folder_context") or " > ".join(p.replace("-", " ").title() for p in rel.split("/")[:-1])
        schema = r.get("schema_action") or ("added" if str(r.get("schema_added", "")).lower() == "true" else "")
        imgs = int(float(r.get("lazy_added") or 0)) + int(float(r.get("alts_added") or 0)) + int(float(r.get("images_updated") or 0))
        out.append({
            "rel": rel, "status": status, "reason": r.get("reason", "") or r.get("error", ""),
            "page_name": _t(r.get("page_name")), "folder": folder,
            "old_title": _t(r.get("old_title")), "new_title": _t(r.get("new_title")),
            "old_description": _t(r.get("old_description")), "new_description": _t(r.get("new_description")),
            "old_h1": _t(r.get("old_h1") if has_rel else r.get("h1_found")), "new_h1": _t(r.get("new_h1")),
            "schema": schema, "og": str(r.get("og_changed", "")).lower() == "true", "imgs": imgs,
            "canonical": r.get("canonical", ""), "cost": float(r.get("cost") or r.get("actual_cost") or 0),
        })
    return out


def page_html(title: str, desc: str, h1: str) -> str:
    e = lambda x: _h.escape(x, quote=True)
    body = f"<h1>{e(h1)}</h1>" if h1 else ""
    meta = f'<meta name="description" content="{e(desc)}">' if desc else ""
    ttl = f"<title>{e(title)}</title>" if title else ""
    return f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">{ttl}{meta}</head><body>{body}</body></html>'


def estimate(rec: dict, title: str, desc: str, h1: str, brand: str) -> dict:
    blob = S.score_page(page_html(title, desc, h1), path=Path(rec["rel"]), root=Path("."), brand=brand,
                        page_name=rec["page_name"], folder=rec["folder"],
                        topic=S.topic_terms(rec["page_name"], rec["folder"], h1, []))
    for i, (cid, *_rest) in enumerate(S.CHECKS):
        if cid not in USED:
            blob["f"][i] = -1                                     # needs the page HTML — not scored here
    blob["crit"] = [c for c in blob["crit"] if c in ("no_title", "no_h1")]
    blob["man"] = [c for c in blob.get("man", []) if c in USED]
    blob["r"] = {k: v for k, v in blob.get("r", {}).items() if k in USED}
    blob["sc"] = SCALE
    return S.recompute(blob)


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    opt = {a: argv[argv.index(a) + 1] for a in argv if a in ("--client", "--site", "--out")}
    args = [a for a in args if a not in opt.values()]
    src = Path(args[0])
    recs = normalise(read_rows(src))
    scored = [r for r in recs if r["status"] in ("updated", "done", "preview")]
    if not scored:
        raise SystemExit("No updated pages found in this CSV.")

    brand = opt.get("--client")
    if not brand:
        tails = Counter(t.rsplit("|", 1)[-1].strip() for t in (r["new_title"] for r in scored) if "|" in t)
        brand = tails.most_common(1)[0][0] if tails else src.stem
    site = opt.get("--site", "")
    if not site:
        hosts = Counter(re.match(r"^(https?://[^/]+)", r["canonical"]).group(1) for r in scored
                        if re.match(r"^https?://", r["canonical"] or ""))
        site = hosts.most_common(1)[0][0] if hosts else ""

    items = []
    for r in scored:
        before = estimate(r, r["old_title"], r["old_description"], r["old_h1"], brand)
        after = estimate(r, r["new_title"], r["new_description"], r["new_h1"] or r["old_h1"], brand)
        items.append({"rel": r["rel"], "before": before, "after": after,
                      "chg": {"h": int(bool(r["new_h1"] and r["new_h1"] != r["old_h1"])), "s": r["schema"],
                              "o": int(r["og"]), "i": r["imgs"], "n": r["page_name"], "oh": r["old_h1"],
                              "nh": r["new_h1"] or r["old_h1"]},
                      "old_title": r["old_title"], "new_title": r["new_title"],
                      "old_description": r["old_description"], "new_description": r["new_description"],
                      "result": None})
    S.apply_site_checks(items)
    summary = S.aggregate(items, 90.0, False)
    not_scored = [(r["rel"], r["status"], r["reason"] or "not changed in this run") for r in recs if r not in scored]

    stamp = (re.search(r"(\d{8}_\d{6})", src.name) or re.search(r"()", "")).group(1) or "estimated"
    out_dir = Path(opt.get("--out", src.parent))
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_name = f"seo_results_{stamp}_with_scores.csv"
    cats = [(c, S.CAT_MAX[c] * SCALE) for c in PARTIAL_CATS]
    fields = ["rel", "status", "page_name", "old_title", "new_title", "old_description", "new_description",
              "old_h1", "new_h1", "score_before", "score_after", "score_change", "rating_before", "rating_after"] + \
             [f"{k}_{c}" for c, _ in cats for k in ("before", "after")]
    with open(out_dir / csv_name, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        by_rel = {r["rel"]: r for r in scored}
        for it in items:
            r, b, a = by_rel[it["rel"]], it["before"], it["after"]
            row = {k: r.get(k, "") for k in ("rel", "status", "page_name", "old_title", "new_title",
                                             "old_description", "new_description", "old_h1", "new_h1")}
            row.update(score_before=b["t"], score_after=a["t"], score_change=round(a["t"] - b["t"], 2),
                       rating_before=b["l"], rating_after=a["l"])
            for c, _ in cats:
                row[f"before_{c}"], row[f"after_{c}"] = b["cats"][c], a["cats"][c]
            w.writerow(row)

    meta = {"site": site, "brand": brand, "dry": False, "engine": f"SEO Meta Tag Updater v{APP_VERSION}",
            "run_id": stamp, "csv_file": csv_name, "partial_cats": PARTIAL_CATS, "estimate_note": NOTE}
    out = out_dir / f"seo_report_{stamp}.html"
    R.write_report(out, summary, items, not_scored, meta)
    print(f"Report: {out}\nCSV:    {out_dir / csv_name}")
    print(f"  pages scored: {summary['pages']}  |  avg {summary['avg_before']:.2f} -> {summary['avg_after']:.2f} "
          f"({summary['delta']:+.2f})  |  not scored: {len(not_scored)}  |  went down: {len(summary['decreased'])}")


if __name__ == "__main__":
    main(sys.argv[1:])
