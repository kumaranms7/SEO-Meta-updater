"""
Re-render an existing report with the CURRENT report template — no scan, no AI calls, no website files needed.

    python tools/refresh_report.py "E:/site/seo_report_20261020_104234.html" [--backup-dir path]

The report's own data block (<script id="report-data">) is read, brought up to date (average character counts, link to the
CSV saved with it) and poured into report_template.html.  The original file is copied to the backup folder first.
"""
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import seo_report as R  # noqa: E402

DATA_RE = re.compile(r'<script id="report-data" type="application/json">(.*?)</script>', re.S)


def upgrade(data: dict, csv_name: str) -> dict:
    pages = data.get("pages", [])
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else 0
    plain = R._plain
    tb = [len(plain(p.get("old_title"))) for p in pages]
    ta = [len(plain(p.get("new_title"))) for p in pages]
    db = [len(plain(p.get("old_desc"))) for p in pages]
    da = [len(plain(p.get("new_desc"))) for p in pages]
    lim = data.setdefault("limits", {})
    lim.update(title_avg_before=avg(tb), title_avg_after=avg(ta), desc_avg_before=avg(db), desc_avg_after=avg(da))
    meta = data.setdefault("meta", {})
    meta.setdefault("estimate_note", "")
    if csv_name:
        meta["csv_file"] = csv_name
    return data


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    bdir = Path(argv[argv.index("--backup-dir") + 1]) if "--backup-dir" in argv else None
    if bdir:
        args = [a for a in args if a != str(bdir)]
    path = Path(args[0])
    old = path.read_text(encoding="utf-8")
    m = DATA_RE.search(old)
    if not m:
        raise SystemExit("This file has no embedded report data (it is not a dynamic report).")
    data = json.loads(m.group(1).replace("<\\/", "</").replace("\\u0021--", "!--"))

    stamp = re.search(r"(\d{8}_\d{6})", path.name)
    csv_name = ""
    if stamp and (path.parent / f"seo_results_{stamp.group(1)}.csv").exists():
        csv_name = f"seo_results_{stamp.group(1)}.csv"
    data = upgrade(data, csv_name)

    if bdir:
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, bdir / f"{path.stem}_BEFORE_REFRESH{path.suffix}")
    tpl = R._tpl_path().read_text(encoding="utf-8")
    tpl = re.sub(r"<!--\s*DYNAMIC REPORT TEMPLATE.*?-->", "", tpl, flags=re.S)
    title = f"SEO & AI Optimization Report | {R.AGENCY}"
    out = tpl.replace("{{TITLE}}", R.e(title)).replace("{{REPORT_JSON}}", R._json_for_html(data))
    path.write_text(out, encoding="utf-8")
    s = data["summary"]
    print(f"Refreshed: {path}\n  pages {s['pages']}  |  avg {s['avg_before']} -> {s['avg_after']} ({s['delta']:+})  |  "
          f"csv link: {csv_name or 'none'}")


if __name__ == "__main__":
    main(sys.argv[1:])
