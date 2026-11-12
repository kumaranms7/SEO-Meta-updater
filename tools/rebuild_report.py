"""
Rebuild the HTML report for a folder that was already optimised — from the saved progress file, with NO AI calls,
NO API cost and NO changes to the website pages.  Handy for trying a new report template on real data.

    python tools/rebuild_report.py "E:/site/HTML files" "E:/site/HTML files/seo_report_20261016_164913.html"
        [--old-report path/to/earlier_report.html]     # optional: re-use the per-page details stored in an older report
        [--show-cost]
"""
import json
import re
import shutil
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import seo_core as C  # noqa: E402
import seo_pipeline as P  # noqa: E402
import seo_report as R  # noqa: E402
import seo_score as S  # noqa: E402

APP_VERSION = "5.2.0"


def load_settings() -> dict:
    p = Path.home() / ".ttm_seo_updater_settings.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def old_report_rows(path) -> dict:
    """Per-page details from the audit data embedded in an earlier report (keyed by page)."""
    txt = Path(path).read_text(encoding="utf-8")
    m = re.search(r"const auditData = (\[.*?\]);\s*\n", txt, re.S)
    return {d["rel"]: d for d in json.loads(m.group(1))} if m else {}


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    old = None
    if "--old-report" in argv:
        old = argv[argv.index("--old-report") + 1]
        args = [a for a in args if a != old]
    folder, out = args[0], Path(args[1])
    cfg = load_settings()

    opts = P.RunOptions(folder=folder, mode="per_page", site_url=cfg.get("site_url", ""), brand=cfg.get("site_name", ""))
    pipe = P.Pipeline(opts, None, [], lambda m, t="": None, lambda r: None, lambda d, t: None, threading.Event())
    items = pipe._collect_score_items()
    if not items:
        raise SystemExit("No saved scores found for this folder — run the tool on it first.")

    details = old_report_rows(old) if old else {}
    for it in items:
        d = details.get(it["rel"])
        if d:                                    # fill in what the saved progress file does not keep
            it["chg"] = {"h": int(bool(d.get("h1_changed"))), "s": d.get("schema_action") or "",
                         "o": int(bool(d.get("og_changed"))), "i": (d.get("lazy_added") or 0) + (d.get("alts_added") or 0),
                         "n": d.get("page_name", ""), "oh": d.get("old_h1", ""), "nh": d.get("new_h1", "")}
            it["result"] = {"page_name": d.get("page_name", ""), "old_h1": d.get("old_h1", ""),
                            "new_h1": d.get("new_h1", ""),
                            "notes": [n for n in (d.get("notes") or "").split("; ") if n]}

    S.apply_site_checks(items)
    use_ai = any(it["before"].get("ai") for it in items)
    summary = S.aggregate(items, float(cfg.get("score_target", 90)), use_ai)

    seen = {it["rel"] for it in items}
    not_scored = []
    for fp in C.collect_html_files(folder):
        rel = C.rel_posix(fp, folder)
        if rel in seen:
            continue
        try:
            html, _, _ = C.read_html(fp)
            why = C.classify_skip(html, rel) or "not part of the saved run"
        except Exception as e:                       # noqa: BLE001
            why = f"unreadable: {e}"
        not_scored.append((rel, "skipped", why))

    stamp = re.search(r"(\d{8}_\d{6})", out.name)
    meta = {"site": cfg.get("site_url", ""), "brand": cfg.get("site_name", ""), "dry": False,
            "engine": f"SEO Meta Tag Updater v{APP_VERSION}", "show_cost": "--show-cost" in flags,
            "cost": sum((details.get(i["rel"]) or {}).get("cost", 0) for i in items) if "--show-cost" in flags else None}
    if stamp:
        meta["run_id"] = stamp.group(1)
        if (out.parent / f"seo_results_{stamp.group(1)}.csv").exists():      # link to the CSV saved with the report
            meta["csv_file"] = f"seo_results_{stamp.group(1)}.csv"
    R.write_report(out, summary, items, not_scored, meta)
    print(f"Report written: {out}")
    print(f"  pages scored: {summary['pages']}  |  avg {summary['avg_before']} -> {summary['avg_after']} "
          f"({summary['delta']:+.1f})  |  not scored: {len(not_scored)}")


if __name__ == "__main__":
    main(sys.argv[1:])
