"""
Dev check: drives the REAL Tk app against a temporary test site with a fake AI (no network, no API cost).

    python tools/e2e_ui.py [screenshot_dir]
"""
import csv
import os
import sys
import tempfile
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "tests"))
os.chdir(APP)

import seo_core as C  # noqa: E402
import seo_meta_updater as M  # noqa: E402
import test_pipeline as TP  # noqa: E402
from tkinter import messagebox  # noqa: E402

SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if SHOTS:
    SHOTS.mkdir(parents=True, exist_ok=True)

td = tempfile.mkdtemp()
C.STATE_DIR = Path(td) / "state"
M.SETTINGS_FILE = Path(td) / "settings.json"
M.REPORTS_DIR = Path(td) / "reports"
root = TP.make_site(td, ["Roof Repair", "Gutter Cleaning", "Window Washing"])

M.anthropic.Anthropic = lambda api_key=None: TP.ScriptedClient()
messagebox.askyesno = lambda *a, **k: True
messagebox.showinfo = messagebox.showwarning = messagebox.showerror = lambda *a, **k: None

canon_popups = []


def fake_canonical_dialog(app, stats):                   # the real window is modal; the test answers for the user
    from types import SimpleNamespace
    canon_popups.append(stats)
    return SimpleNamespace(go=True)


M.CanonicalDoneDialog = fake_canonical_dialog
app = M.SEOUpdaterApp()
app.geometry("1360x880+20+20")
app.folder_var.set(str(root))
assert not app.report_btn._enabled, "View Report must be disabled before any scan"
app.api_key_var.set("sk-test")
app.site_name_var.set("Acme")
app.site_url_var.set("site.com")
app.mode_var.set("batch")
app.business = {"name": "Acme", "phone": "305-555-0100", "street": "1 Main St", "city": "Miami", "region": "FL", "country": "US"}
app.business_verified = True


def pump(seconds, until=None):
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        if until and until():
            return True
        time.sleep(0.02)
    return False


def shot(name):
    if not SHOTS:
        return
    from PIL import ImageGrab
    # Bring OUR windows to the front first, otherwise the grab captures whatever else is on the screen.
    wins = [app] + [w for w in app.winfo_children() if w.winfo_class() == "Toplevel"]
    for w in wins:
        w.attributes("-topmost", True)
        w.lift()
    pump(0.6)
    full = ImageGrab.grab()
    k = full.size[0] / app.winfo_screenwidth()            # Windows display scaling (e.g. 125 %)
    x, y, w_, h_ = [int(v * k) for v in (app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height())]
    full.crop((x, y, x + w_, y + h_)).save(SHOTS / f"{name}.png")
    for w in wins:
        w.attributes("-topmost", False)


pump(0.8)
assert len(app.files) == 3, app.files

# untick one file -> must be left alone
first = next(iter(app.ftree.checked))
app.ftree.toggle(first)
before_unticked = Path(root, app.ftree.nodes[first]["rel"]).read_text(encoding="utf-8")

app._run()
ok = pump(20, until=lambda: not app._running and len(app.results) >= 2 and app.score_summary)
print("finished:", ok, [(r["rel"], r["status"], r["score_before"]["t"], r["score_after"]["t"]) for r in app.results])
assert ok and all(r["status"] == "updated" for r in app.results), app.results
assert Path(root, app.ftree.nodes[first]["rel"]).read_text(encoding="utf-8") == before_unticked
assert app.stat_vars["updated"].get() == "2"
assert len(app.tree.get_children()) == 2

# score columns + strip + summary
vals = app.tree.item(app.tree.get_children()[0])["values"]
assert vals[2] != "–" and vals[3] != "–" and float(str(vals[4]).lstrip("+")) > 0, vals   # Tk hands numbers back as numbers
first_row = app.results[int(app.tree.get_children()[0])]
assert float(vals[2]) == round(first_row["score_before"]["t"], 2) and float(vals[3]) == round(first_row["score_after"]["t"], 2), vals   # real values, two decimals
assert round(float(vals[3]) - float(vals[2]), 2) == float(str(vals[4]).lstrip("+")), vals                                          # change = difference of the numbers shown
assert app.score_strip.winfo_ismapped() and "SEO score" in app.score_strip.cget("text")
assert app.score_summary["pages"] == 2 and app.score_summary["delta"] > 5

# step 1 (canonical) is on by default and shows in the table, the stats and the CSV
assert app.canon_var.get() and app.skip_ht_var.get()
assert all(r["canonical_action"] in ("added", "fixed", "ok") for r in app.results), [r["canonical_action"] for r in app.results]
assert vals[8] in M.CANON_TXT.values(), vals
assert len(canon_popups) == 1 and canon_popups[0]["checked"] == 2, canon_popups      # shown once, after step 1
assert app.stat_vars["canon"].get() == str(sum(1 for r in app.results if r["canonical_action"] in ("added", "fixed")))
print(app.score_strip.cget("text"))
shot("results_with_scores")

# detail dialog shows the score breakdown
app._show_detail(app.results[0])
pump(0.4)
shot("detail_with_scores")

# the CSV and the HTML report were saved AUTOMATICALLY at the end of the run (no button clicked)
rep_dir = root                                          # saved in the SAME folder that was scanned
auto_csv = sorted(rep_dir.glob("seo_results_*.csv"))
auto_html = sorted(rep_dir.glob("seo_report_*.html"))
assert auto_csv and auto_html, (rep_dir, list(rep_dir.glob("*")) if rep_dir.exists() else "no folder")
rows = list(csv.DictReader(open(auto_csv[-1], encoding="utf-8-sig")))
assert rows and rows[0]["score_before"] and rows[0]["score_after"] and rows[0]["after_title"], rows[0].keys()
assert not ({"cost", "error", "file"} & set(rows[0].keys())), "cost/error/file columns must be absent unless show-cost is on"
html = auto_html[-1].read_text(encoding="utf-8")
import json as _json, re as _re  # noqa: E402
data = _json.loads(_re.search(r'<script id="report-data" type="application/json">(.*?)</script>', html, _re.S).group(1))
assert "Optimization Report" in html and abs(data["summary"]["avg_after"] - app.score_summary["avg_after"]) < 0.06, data["summary"]
assert "AI cost" not in html, "cost must be hidden in the report by default"
assert app.report_cost_var.get() is False
assert app.report_btn._enabled, "View Report must be enabled after a run"
assert auto_html[-1].parent == root and auto_csv[-1].parent == root
app._on_folder_change()
assert not any(f.lower().endswith(".html") and Path(f).name.startswith("seo_report_") for f in app.files), "report must never be scanned as a page"
assert len(app.files) == 3, app.files

# "View Report" opens the newest report in the browser
import webbrowser  # noqa: E402
opened = []
webbrowser.open = lambda url, *a, **k: opened.append(url) or True
app._view_report()
assert opened and opened[0].startswith("file:///") and opened[0].endswith(auto_html[-1].name), opened
# after an app restart the button still finds the latest report on disk
app.last_report_path = ""
assert app._latest_report() == auto_html[-1]

# switching the option on puts the cost into the NEXT report
app.report_cost_var.set(True)
app._auto_export()
newest = sorted(rep_dir.glob("seo_report_*.html"))[-1]
assert "AI cost of this run" in newest.read_text(encoding="utf-8")
app.report_cost_var.set(False)

# there is no CSV button any more (the CSV is saved automatically) and View Report follows the scan state
assert not hasattr(app, "export_btn"), "the manual CSV button must be gone"
app._on_folder_change()
assert not app.report_btn._enabled, "View Report must be disabled until a scan completes"

# filter chips still work with the extra columns
app._set_filter("review")
assert len(app.tree.get_children()) == 0
app._set_filter("all")
assert len(app.tree.get_children()) == 2

# resume: only the previously unticked page is processed, but the summary covers all pages
class FakeChoice:
    def __init__(self, *a, **k):
        self.choice = "resume"


M.ChoiceDialog = FakeChoice
app.ftree.set_all(True)
app._run()
pump(20, until=lambda: not app._running and app.score_summary and app.score_summary["pages"] == 3)
print("resume results:", [(r["rel"], r["status"]) for r in app.results], "| summary pages:", app.score_summary["pages"])
assert [r["rel"] for r in app.results] == [app.ftree.nodes[first]["rel"]]
assert app.score_summary["pages"] == 3

# business dialog opens and saves
app._edit_business(["phone not found"], {"name": "Acme", "phone": ""})
pump(0.4)
dlg = [w for w in app.winfo_children() if isinstance(w, M.BusinessDialog)][0]
dlg.vars["phone"].set("555-9999")
dlg._save()
assert app.business["phone"] == "555-9999" and app.business_verified

app._show_tab("④ Options")
shot("options")
app.destroy()
print("E2E OK")
