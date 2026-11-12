#!/usr/bin/env python3
"""
Top Ten Website Marketing — SEO Meta Tag Updater  (v4)
==============================================
Desktop tool that scans a static HTML website and, with Claude, writes unique
meta titles, descriptions and H1s, merges JSON-LD schema, adds social tags and
fixes image attributes — safely (original encoding kept, backups outside the
site folder, resumable, nothing ever trimmed mid-sentence).

Modules:  seo_core.py (HTML logic)  ·  seo_ai.py (Claude)  ·  seo_pipeline.py (run engine)

Run:      python seo_meta_updater.py
"""

import os
import sys
import csv
import json
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from datetime import datetime


# ─────────────────────────────────────────────────────────────────────────────
# Dependency check & auto-install
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_packages():
    missing = []
    try:
        import anthropic  # noqa
    except ImportError:
        missing.append("anthropic")
    try:
        import keyring  # noqa
    except ImportError:
        missing.append("keyring")
    if missing:
        import subprocess
        root = tk.Tk(); root.withdraw()
        if messagebox.askyesno("Missing packages",
                               "These packages are not installed:\n\n  " + ", ".join(missing) +
                               "\n\nInstall them now with pip?"):
            for pkg in missing:
                subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])
        else:
            sys.exit(1)
        root.destroy()


_ensure_packages()

import anthropic  # noqa: E402
try:
    import keyring as _keyring
    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False

import seo_icons              # noqa: E402
import seo_core as C            # noqa: E402
import seo_ai as A              # noqa: E402
import seo_canonical as K       # noqa: E402
import seo_sitemap as SM        # noqa: E402
import seo_pipeline as P        # noqa: E402
import seo_score as S           # noqa: E402
import seo_report as R          # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
APP_NAME = "Top Ten Website Marketing — SEO Meta Tag Updater"
APP_VERSION = "5.2.0"
KEYRING_SVC = "TopTenMarketing_SEOUpdater"
KEYRING_USR = "anthropic_api_key"
SETTINGS_FILE = Path.home() / ".ttm_seo_updater_settings.json"
REPORTS_DIR = Path.home() / "TTM_SEO_Reports"       # fallback only, if the scanned folder cannot be written to
SAME_AS_MAIN = "Same as main model"
SEPARATORS = ["|", "-", "–", "—", "•", ":", "·"]
BUSINESS_TYPES = ["LocalBusiness", "Organization", "ProfessionalService", "HomeAndConstructionBusiness",
                  "Plumber", "Electrician", "RoofingContractor", "Locksmith", "HVACBusiness",
                  "AutoRepair", "Dentist", "LegalService", "RealEstateAgent", "Restaurant", "Store"]

# ── Light theme palette (high contrast, easy to read) ────────────────────────
DARK_BG = "#eef1f6"          # page background (name kept from the old dark theme)
PANEL_BG = "#ffffff"
CARD_BG = "#ffffff"
CARD_BORDER = "#c9d1e0"
INPUT_BG = "#ffffff"
ACCENT = "#1f5fd1"
ACCENT2 = "#6a3fd1"
ACCENT_HOVER = "#184fae"
SUCCESS = "#0b7a4b"
WARNING = "#9a5b00"
DANGER = "#c0262d"
INFO = "#0a6fa3"
TEXT_PRIMARY = "#1b2333"
TEXT_SECONDARY = "#3a4461"
TEXT_MUTED = "#566079"
SELECT_BG = "#cfe0ff"        # selected row / selected option
SOFT_ACCENT = "#e6efff"
WARN_BG = "#fff3d1"
CONSOLE_BG = "#f6f8fc"
CONSOLE_FG = "#2b3350"

FONT_MAIN = ("Segoe UI", 11)
FONT_SMALL = ("Segoe UI", 10)
FONT_TINY = ("Segoe UI", 9)
FONT_MONO = ("Cascadia Code", 10) if sys.platform == "win32" else ("Courier New", 10)
FONT_HEADING = ("Segoe UI Semibold", 12)
FONT_CARD = ("Segoe UI Semibold", 11)
FONT_LOGO = ("Segoe UI Semibold", 14)

DEFAULT_SETTINGS = {
    "last_folder": "", "recursive": True, "mode": "dry_run",
    "model": list(A.MODELS.keys())[0], "rewrite_model": SAME_AS_MAIN,
    "site_url": "", "site_name": "", "site_context": "", "separator": "|", "default_image": "",
    "business": {}, "business_verified": False,
    "do_h1": True, "do_schema": True, "do_og": True, "do_robots": True,
    "do_lazy": True, "do_alt": True, "ai_alt": True, "backup": True,
    "skip_partial": True, "skip_noindex": True, "skip_redirect": True, "skip_error": True,
    "exclude_patterns": "", "excluded": {},
    "do_score": True, "deep_check": False, "score_model": SAME_AS_MAIN, "score_target": 90,
    "report_show_cost": False, "protect_score": True, "max_retries": 3,
    "do_canonical": True, "skip_htaccess": True, "canon_remove_redirected": True, "clean_url_match": False,
    "htaccess_path": "", "fix_multi_h1": False, "do_breadcrumb": True, "do_faq": True, "fix_links": True,
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            s.update(data)
            if data.get("site_logo") and not data.get("default_image"):
                s["default_image"] = data["site_logo"]
    except Exception:
        pass
    return s


def save_settings(data: dict):
    try:
        SETTINGS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def load_api_key():
    if KEYRING_AVAILABLE:
        try:
            return _keyring.get_password(KEYRING_SVC, KEYRING_USR) or ""
        except Exception:
            pass
    return os.environ.get("ANTHROPIC_API_KEY", "")


def store_api_key(key: str):
    if KEYRING_AVAILABLE:
        try:
            _keyring.set_password(KEYRING_SVC, KEYRING_USR, key)
            return True
        except Exception:
            pass
    os.environ["ANTHROPIC_API_KEY"] = key
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Colourful icons (pre-rendered PNGs in seo_icons.py — no extra libraries needed)
# ─────────────────────────────────────────────────────────────────────────────
_ICON_CACHE = {}
EMOJI_ICON = {
    "📁": "folder", "⚙️": "gear", "⚙": "gear", "🔍": "search", "📄": "page", "⚡": "bolt", "⏯": "progress",
    "🌐": "globe", "✏️": "pencil", "✏": "pencil", "✎": "pencil", "🖼": "image", "🏢": "office",
    "🛠️": "tools", "🛠": "tools", "🛡": "shield", "⏭": "skip", "🔑": "key", "🤖": "robot", "💰": "money",
    "💾": "save", "▶": "play", "⏹": "stop", "📊": "chart", "↻": "retry", "🗑": "trash", "⟲": "restore",
    "✓": "check", "📂": "open", "📋": "copy", "🚀": "rocket",
}
TAB_ICONS = {"① Project": "folder", "② Business": "office", "③ Files": "files",
             "④ Options": "gear", "⑤ AI & Cost": "robot"}


def icon(name, large=False):
    """Cached full-colour icon (needs a Tk root, so call it after the window exists)."""
    key = (name, large)
    if key not in _ICON_CACHE:
        _ICON_CACHE[key] = tk.PhotoImage(data=seo_icons.ICONS[name][1 if large else 0])
    return _ICON_CACHE[key]


def split_icon(text):
    """'🔍  Fetch' -> ('search', 'Fetch').  Text without a known leading glyph is returned unchanged."""
    t = text.lstrip()
    for k in sorted(EMOJI_ICON, key=len, reverse=True):
        if t.startswith(k):
            return EMOJI_ICON[k], t[len(k):].strip()
    return None, text


# ─────────────────────────────────────────────────────────────────────────────
# Small UI toolkit
# ─────────────────────────────────────────────────────────────────────────────
class Btn(tk.Label):
    """Flat, hover-aware button (a Label, so colours behave on every platform)."""
    STYLES = {
        "primary":   (ACCENT, "white", ACCENT_HOVER),
        "accent2":   (ACCENT2, "white", "#5630b5"),
        "secondary": ("#e3e8f2", TEXT_PRIMARY, "#d2daea"),
        "ghost":     ("#f1f4f9", TEXT_SECONDARY, "#e3e8f2"),
        "danger":    ("#fde8e8", DANGER, "#fbd3d3"),
        "success":   ("#dcf5e8", SUCCESS, "#c6ecd9"),
    }

    def __init__(self, parent, text, command, style="secondary", **kw):
        bg, fg, hover = self.STYLES[style]
        ic, text = split_icon(text)
        if ic:
            kw.update(image=icon(ic), compound="left")
            text = "  " + text
        super().__init__(parent, text=text, bg=bg, fg=fg, font=kw.pop("font", FONT_MAIN),
                         padx=kw.pop("padx", 14), pady=kw.pop("pady", 7), cursor="hand2", **kw)
        self._cmd, self._bg, self._fg, self._hover = command, bg, fg, hover
        self._enabled = True
        self.bind("<Enter>", lambda e: self._enabled and self.config(bg=self._hover))
        self.bind("<Leave>", lambda e: self.config(bg=self._bg if self._enabled else "#e9ecf3"))
        self.bind("<Button-1>", lambda e: self._enabled and self._cmd and self._cmd())

    def set_enabled(self, on: bool):
        self._enabled = on
        self.config(bg=self._bg if on else "#e9ecf3", fg=self._fg if on else "#98a1b8",
                    cursor="hand2" if on else "arrow")

    def set_text(self, text):
        ic, text = split_icon(text)
        if ic:
            self.config(text="  " + text, image=icon(ic), compound="left")
        else:
            self.config(text=text, image="")


class ScrollFrame(tk.Frame):
    """A vertically scrollable frame; wheel scrolling is routed by the app."""

    def __init__(self, parent, bg=DARK_BG):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(win, width=e.width))


def card(parent, title=None, subtitle=None):
    """Bordered card; returns the inner frame to pack content into."""
    outer = tk.Frame(parent, bg=CARD_BG, highlightbackground=CARD_BORDER, highlightthickness=1)
    outer.pack(fill="x", pady=(0, 10))
    inner = tk.Frame(outer, bg=CARD_BG)
    inner.pack(fill="x", padx=14, pady=12)
    if title:
        ic, clean = split_icon(title)
        if ic:
            tk.Label(inner, text="  " + clean, image=icon(ic, True), compound="left", font=FONT_CARD,
                     fg=TEXT_PRIMARY, bg=CARD_BG).pack(anchor="w")
        else:
            tk.Label(inner, text=title, font=FONT_CARD, fg=TEXT_PRIMARY, bg=CARD_BG).pack(anchor="w")
    if subtitle:
        tk.Label(inner, text=subtitle, font=FONT_TINY, fg=TEXT_MUTED, bg=CARD_BG, justify="left",
                 wraplength=430).pack(anchor="w", pady=(1, 6))
    elif title:
        tk.Frame(inner, bg=CARD_BG, height=6).pack()
    return inner


def entry(parent, var, show=None, width=None):
    e = tk.Entry(parent, textvariable=var, font=FONT_MAIN, bg=INPUT_BG, fg=TEXT_PRIMARY,
                 insertbackground=TEXT_PRIMARY, relief="flat", bd=0, show=show or "",
                 highlightthickness=1, highlightbackground=CARD_BORDER, highlightcolor=ACCENT,
                 **({"width": width} if width else {}))
    return e


def labeled(parent, label, var, hint=None, show=None):
    tk.Label(parent, text=label, font=FONT_SMALL, fg=TEXT_SECONDARY, bg=parent["bg"]).pack(anchor="w")
    entry(parent, var, show=show).pack(fill="x", ipady=6, pady=(2, 2))
    if hint:
        tk.Label(parent, text=hint, font=FONT_TINY, fg=TEXT_MUTED, bg=parent["bg"],
                 justify="left", wraplength=430).pack(anchor="w", pady=(0, 6))
    else:
        tk.Frame(parent, bg=parent["bg"], height=6).pack()


def toggle(parent, text, var, hint=None, command=None, indent=0):
    f = tk.Frame(parent, bg=CARD_BG)
    f.pack(fill="x", pady=2, padx=(indent, 0))
    tk.Checkbutton(f, text=text, variable=var, command=command, bg=CARD_BG, fg=TEXT_PRIMARY,
                   selectcolor=INPUT_BG, activebackground=CARD_BG, activeforeground=TEXT_PRIMARY,
                   font=FONT_MAIN, anchor="w", bd=0, highlightthickness=0).pack(anchor="w")
    if hint:
        tk.Label(f, text=hint, font=FONT_TINY, fg=TEXT_MUTED, bg=CARD_BG, justify="left",
                 wraplength=410).pack(anchor="w", padx=(24, 0))
    return f


def text_box(parent, height=4, bg=INPUT_BG):
    t = tk.Text(parent, height=height, font=FONT_MAIN, bg=bg, fg=TEXT_PRIMARY, insertbackground=TEXT_PRIMARY,
                relief="flat", bd=0, highlightthickness=1, highlightbackground=CARD_BORDER,
                highlightcolor=ACCENT, wrap="word", padx=6, pady=4)
    return t


def center(win, parent, w, h):
    win.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
    y = parent.winfo_rooty() + max((parent.winfo_height() - h) // 3, 20)
    win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")


# ─────────────────────────────────────────────────────────────────────────────
# Dialogs
# ─────────────────────────────────────────────────────────────────────────────
class BusinessDialog(tk.Toplevel):
    """Review / edit the business facts (phone, address, hours …) used in schema."""

    def __init__(self, app, data: dict, warnings: list, on_save, on_refetch=None):
        super().__init__(app)
        self.app, self.on_save, self.on_refetch = app, on_save, on_refetch
        self.title("Business details — verify before use")
        self.configure(bg=PANEL_BG)
        self.transient(app)
        center(self, app, 680, 780)
        self.minsize(600, 600)
        self.vars = {}

        tk.Label(self, text="Business details", font=("Segoe UI Semibold", 14), fg=TEXT_PRIMARY,
                 bg=PANEL_BG).pack(anchor="w", padx=20, pady=(16, 0))
        tk.Label(self, text="These facts are written into your pages' schema exactly as shown. "
                            "The AI never invents them. Please check every field.",
                 font=FONT_SMALL, fg=TEXT_MUTED, bg=PANEL_BG, wraplength=590, justify="left"
                 ).pack(anchor="w", padx=20, pady=(2, 8))

        if warnings:
            wb = tk.Frame(self, bg=WARN_BG)
            wb.pack(fill="x", padx=20, pady=(0, 8))
            tk.Label(wb, text="⚠  " + "\n⚠  ".join(warnings), font=FONT_TINY, fg=WARNING, bg=WARN_BG,
                     justify="left", wraplength=580).pack(anchor="w", padx=10, pady=6)

        body = ScrollFrame(self, bg=PANEL_BG)
        body.pack(fill="both", expand=True, padx=(20, 8))
        app.register_scroll(body)
        f = body.inner

        def row(label, key, width=None, combo=None):
            r = tk.Frame(f, bg=PANEL_BG)
            r.pack(fill="x", pady=3)
            tk.Label(r, text=label, width=16, anchor="w", font=FONT_SMALL, fg=TEXT_SECONDARY,
                     bg=PANEL_BG).pack(side="left")
            v = tk.StringVar(value=str(data.get(key, "") or ""))
            self.vars[key] = v
            if combo:
                ttk.Combobox(r, textvariable=v, values=combo, font=FONT_MAIN).pack(side="left", fill="x", expand=True)
            else:
                entry(r, v).pack(side="left", fill="x", expand=True, ipady=5)

        row("Business name", "name")
        tk.Label(f, text="Business summary — what it is, specialty, main region, other facts", font=FONT_SMALL,
                 fg=TEXT_SECONDARY, bg=PANEL_BG).pack(anchor="w", pady=(6, 2))
        self.context = text_box(f, 3)
        self.context.pack(fill="x", pady=(0, 6))
        self.context.insert("1.0", data.get("context", "") or "")
        tk.Label(f, text="ALL services offered — one per line (add any that are missing)", font=FONT_SMALL,
                 fg=TEXT_SECONDARY, bg=PANEL_BG).pack(anchor="w", pady=(6, 2))
        self.services = text_box(f, 8)
        self.services.pack(fill="x", pady=(0, 6))
        self.services.insert("1.0", "\n".join(data.get("services") or []))
        tk.Label(f, text="ALL service areas / locations — one per line", font=FONT_SMALL,
                 fg=TEXT_SECONDARY, bg=PANEL_BG).pack(anchor="w", pady=(6, 2))
        self.areas = text_box(f, 6)
        self.areas.pack(fill="x", pady=(0, 6))
        self.areas.insert("1.0", "\n".join(data.get("areas") or []))
        row("Schema type", "business_type", combo=BUSINESS_TYPES)
        row("Phone", "phone")
        row("Email", "email")
        row("Street address", "street")
        row("City", "city")
        row("State / region", "region")
        row("Postal code", "postal")
        row("Country", "country")
        row("Price range", "price_range")
        row("Logo URL", "logo")

        tk.Label(f, text="Opening hours — one per line, e.g.  Mo-Fr 09:00-17:00", font=FONT_SMALL,
                 fg=TEXT_SECONDARY, bg=PANEL_BG).pack(anchor="w", pady=(10, 2))
        self.hours = text_box(f, 3)
        self.hours.pack(fill="x")
        self.hours.insert("1.0", "\n".join(data.get("hours") or []))
        tk.Label(f, text="Social profile URLs — one per line", font=FONT_SMALL, fg=TEXT_SECONDARY,
                 bg=PANEL_BG).pack(anchor="w", pady=(10, 2))
        self.social = text_box(f, 4)
        self.social.pack(fill="x", pady=(0, 10))
        self.social.insert("1.0", "\n".join(data.get("same_as") or []))

        bar = tk.Frame(self, bg=PANEL_BG)
        bar.pack(fill="x", padx=20, pady=12)
        Btn(bar, "✓  Verified — Save & use", self._save, "success").pack(side="right")
        Btn(bar, "Cancel", self.destroy, "ghost").pack(side="right", padx=8)
        if on_refetch:
            Btn(bar, "↻  Re-fetch from website", self._refetch, "secondary").pack(side="left")
        self.grab_set()

    def _collect(self):
        d = {k: v.get().strip() for k, v in self.vars.items()}
        d["hours"] = [x.strip() for x in self.hours.get("1.0", "end").splitlines() if x.strip()]
        d["same_as"] = [x.strip() for x in self.social.get("1.0", "end").splitlines() if x.strip()]
        d["business_type"] = d.get("business_type") or "LocalBusiness"
        d["context"] = " ".join(self.context.get("1.0", "end").split())
        d["services"] = [x.strip() for x in self.services.get("1.0", "end").splitlines() if x.strip()]
        d["areas"] = [x.strip() for x in self.areas.get("1.0", "end").splitlines() if x.strip()]
        return d

    def _save(self):
        d = self._collect()
        if not d.get("name"):
            messagebox.showwarning("Business name needed", "Please enter the business name.", parent=self)
            return
        self.on_save(d)
        self.destroy()

    def _refetch(self):
        self.destroy()
        self.on_refetch()


class ChoiceDialog(tk.Toplevel):
    """Resume / retry / start over."""

    def __init__(self, app, summary: str, backup_hint: str):
        super().__init__(app)
        self.choice = None
        self.title("Previous progress found")
        self.configure(bg=PANEL_BG)
        self.transient(app)
        center(self, app, 520, 330)
        self.resizable(False, False)
        tk.Label(self, text="Continue where you left off?", font=("Segoe UI Semibold", 14),
                 fg=TEXT_PRIMARY, bg=PANEL_BG).pack(anchor="w", padx=22, pady=(18, 4))
        tk.Label(self, text=summary, font=FONT_MAIN, fg=TEXT_SECONDARY, bg=PANEL_BG, justify="left",
                 wraplength=470).pack(anchor="w", padx=22)
        tk.Label(self, text=backup_hint, font=FONT_TINY, fg=TEXT_MUTED, bg=PANEL_BG, justify="left",
                 wraplength=470).pack(anchor="w", padx=22, pady=(6, 10))

        def pick(c):
            self.choice = c
            self.destroy()
        Btn(self, "▶  Resume — only unfinished pages", lambda: pick("resume"), "primary").pack(fill="x", padx=22, pady=3)
        Btn(self, "↻  Retry only failed / needs-review pages", lambda: pick("retry"), "secondary").pack(fill="x", padx=22, pady=3)
        Btn(self, "⟲  Start over (clear progress)", lambda: pick("fresh"), "danger").pack(fill="x", padx=22, pady=3)
        Btn(self, "Cancel", lambda: pick(None), "ghost").pack(fill="x", padx=22, pady=(3, 14))
        self.grab_set()
        self.wait_window()


class CanonicalDoneDialog(tk.Toplevel):
    """Shown when step 1 (canonical tags) has finished: the numbers, then Continue or Cancel."""

    def __init__(self, app, st: dict):
        super().__init__(app)
        self.go = False                                          # closing the window = Cancel (no surprise AI spend)
        self.title("Step 1 finished — canonical tags")
        self.configure(bg=PANEL_BG)
        self.transient(app)
        self.resizable(False, False)
        dry = st.get("dry")
        w = "would be " if dry else ""
        center(self, app, 520, 720 if st.get("links") else 640)
        tk.Label(self, text="Canonical tags — preview" if dry else "Canonical tags are done",
                 font=("Segoe UI Semibold", 15), fg=TEXT_PRIMARY, bg=PANEL_BG).pack(anchor="w", padx=24, pady=(18, 2))
        tk.Label(self, text=("Dry run: nothing was written to your files." if dry else
                             "The canonical tags were written to your pages (originals are backed up)."),
                 font=FONT_SMALL, fg=TEXT_MUTED, bg=PANEL_BG).pack(anchor="w", padx=24)
        box = tk.Frame(self, bg=CARD_BG, highlightbackground=CARD_BORDER, highlightthickness=1)
        box.pack(fill="x", padx=24, pady=14)
        rows = [("Pages checked", st.get("checked", 0), TEXT_PRIMARY)]
        if st.get("tags", True):
            rows += [(f"Canonical {w}added", st.get("insert", 0), SUCCESS),
                     (f"Canonical {w}fixed", st.get("replace", 0), SUCCESS),
                     ("Already correct", st.get("ok", 0), TEXT_SECONDARY),
                     ("Points to another domain (kept)", st.get("keep_foreign", 0), WARNING),
                     (f"Removed from redirected / gone pages", st.get("removed", 0), TEXT_SECONDARY),
                     ("Skipped (copies, backups, noindex, partials)", st.get("junk", 0), TEXT_MUTED),
                     ("Could not be written", st.get("error", 0), DANGER if st.get("error") else TEXT_MUTED)]
        rows += [("Redirected / gone pages (301 / 302 / 410)",
                  f"{st.get('redirected', 0)}   ({st.get('301', 0)} / {st.get('302', 0)} / {st.get('410', 0)})", INFO)]
        if st.get("links"):
            rows += [(f"Internal links {w}fixed", f"{st.get('links_fixed', 0)}  on {st.get('link_pages', 0)} page(s)", SUCCESS),
                     ("Links to gone (410) pages — to review", st.get("links_gone", 0),
                      WARNING if st.get("links_gone") else TEXT_MUTED),
                     ("Other links to review (302, other site, loops)", st.get("links_other", 0), TEXT_MUTED)]
        for i, (label, val, col) in enumerate(rows):
            r = tk.Frame(box, bg=CARD_BG)
            r.pack(fill="x", padx=14, pady=(8 if i == 0 else 3, 8 if i == len(rows) - 1 else 0))
            tk.Label(r, text=label, font=FONT_MAIN, fg=TEXT_SECONDARY, bg=CARD_BG).pack(side="left")
            tk.Label(r, text=str(val), font=("Segoe UI Semibold", 11), fg=col, bg=CARD_BG).pack(side="right")
        tk.Label(self, text=f"The SEO update (meta title, description, H1…) will run on "
                            f"{st.get('next_pages', 0)} page(s).", font=FONT_MAIN, fg=TEXT_PRIMARY, bg=PANEL_BG,
                 wraplength=470, justify="left").pack(anchor="w", padx=24)

        def pick(go):
            self.go = go
            self.destroy()
        Btn(self, "▶  Continue SEO update", lambda: pick(True), "primary").pack(fill="x", padx=24, pady=(14, 4))
        Btn(self, "Cancel", lambda: pick(False), "secondary").pack(fill="x", padx=24, pady=4)
        tk.Label(self, text="Cancel stops here: the canonical tags stay as they are and no SEO update is done "
                            "(use this to add canonical tags only).", font=FONT_TINY, fg=TEXT_MUTED, bg=PANEL_BG,
                 wraplength=470, justify="left").pack(anchor="w", padx=24, pady=(2, 14))
        self.protocol("WM_DELETE_WINDOW", lambda: pick(False))
        self.grab_set()
        self.wait_window()


class SitemapDialog(tk.Toplevel):
    """Checks sitemap.xml against the real pages and can write a clean one (the old file is backed up first)."""

    def __init__(self, app, folder: str, site_url: str):
        super().__init__(app)
        self.app, self.folder, self.site_url = app, folder, site_url
        self.rep = None
        self.title("Sitemap check")
        self.configure(bg=PANEL_BG)
        center(self, app, 860, 660)
        top = tk.Frame(self, bg=PANEL_BG)
        top.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(top, text="Sitemap file", font=FONT_SMALL, fg=TEXT_SECONDARY, bg=PANEL_BG).pack(side="left")
        self.path_var = tk.StringVar(value=str(Path(folder) / "sitemap.xml"))
        entry(top, self.path_var).pack(side="left", fill="x", expand=True, padx=8, ipady=3)
        Btn(top, "Browse…", self._browse, "secondary", padx=10, pady=4).pack(side="left")
        Btn(top, "Check again", self._start, "primary", padx=12, pady=4).pack(side="left", padx=(8, 0))
        self.status = tk.Label(self, text="", font=FONT_SMALL, fg=TEXT_MUTED, bg=PANEL_BG, anchor="w")
        self.status.pack(fill="x", padx=16)
        frm = tk.Frame(self, bg=PANEL_BG)
        frm.pack(fill="both", expand=True, padx=14, pady=6)
        sb = tk.Scrollbar(frm)
        sb.pack(side="right", fill="y")
        self.txt = tk.Text(frm, wrap="none", font=("Consolas", 10), bg=INPUT_BG, fg=TEXT_PRIMARY, relief="flat",
                           yscrollcommand=sb.set, padx=10, pady=8)
        self.txt.pack(fill="both", expand=True)
        sb.config(command=self.txt.yview)
        bar = tk.Frame(self, bg=PANEL_BG)
        bar.pack(fill="x", padx=14, pady=(0, 12))
        self.write_btn = Btn(bar, "Write a clean sitemap.xml", self._write, "success")
        self.write_btn.pack(side="left")
        self.write_btn.set_enabled(False)
        self.csv_btn = Btn(bar, "Save the findings (CSV)", self._save_csv, "secondary")
        self.csv_btn.pack(side="left", padx=8)
        self.csv_btn.set_enabled(False)
        Btn(bar, "Close", self.destroy, "ghost").pack(side="right")
        self._start()

    def _browse(self):
        f = filedialog.askopenfilename(title="Choose the sitemap file", initialdir=self.folder,
                                       filetypes=[("Sitemap", "*.xml"), ("All files", "*.*")])
        if f:
            self.path_var.set(f)
            self._start()

    def _set(self, text):
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", text)
        self.txt.config(state="disabled")

    def _start(self):
        self.status.config(text="Reading the pages…")
        self._set("")
        self.write_btn.set_enabled(False)
        self.csv_btn.set_enabled(False)

        ht, files = self.app._htaccess_path(), list(self.app.files)          # Tk variables are read on the main thread
        clean, sm_path = self.app.clean_var.get(), self.path_var.get().strip()

        def work():
            try:
                rules = K.load_htaccess(ht)
                pages = SM.read_pages(self.folder, files, rules, self.site_url, clean)
                rep = SM.analyse(self.folder, self.site_url, pages, sm_path)
                self.app._ui(lambda: self._show(rep))
            except Exception as e:                               # noqa: BLE001
                msg = f"Could not check the sitemap: {e}"
                self.app._ui(lambda: self.status.config(text=msg))
        threading.Thread(target=work, daemon=True).start()

    def _show(self, rep):
        if not self.winfo_exists():
            return
        self.rep = rep
        out = []
        if not rep.found:
            out.append(f"No sitemap found at {rep.path}")
            out.append("A new one can be written from the pages below.")
        else:
            out.append(f"Sitemap     : {rep.path}")
            if rep.error:
                out.append(f"Note        : {rep.error}")
            out.append(f"Addresses   : {rep.entries}    correct: {rep.ok}    with a problem: "
                       f"{len({loc for loc, _ in rep.issues})}")
        out.append(f"Live pages  : {rep.live_pages}    missing from the sitemap: {len(rep.missing)}")
        out.append(f"A clean sitemap would list {rep.new_count} page(s).")
        if rep.extras:
            out.append("Warning     : this sitemap has image / video / news / language entries — a rewritten file "
                       "would not keep them.")
        if rep.issues:
            out += ["", f"Problems in the sitemap ({len(rep.issues)})"]
            for loc, why in rep.issues:
                out += [f"  {loc}", f"      → {why}"]
        if rep.missing:
            out += ["", f"Live pages that are not in the sitemap ({len(rep.missing)})"]
            out += [f"  {url}   ({rel})" for rel, url in rep.missing]
        self._set("\n".join(out))
        self.status.config(text="Done." if not rep.error or rep.kind == "urlset" else rep.error)
        can_write = rep.new_count > 0 and rep.kind != "index" and not (rep.error and rep.found and not rep.kind)
        self.write_btn.set_enabled(can_write)
        self.csv_btn.set_enabled(bool(rep.issues or rep.missing))

    def _save_csv(self):
        import csv
        path = Path(self.folder) / f"seo_sitemap_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["where", "address", "note"])
                w.writerows(SM.report_rows(self.rep))
            messagebox.showinfo("Saved", f"Saved in the website folder:\n{path.name}")
        except OSError as e:
            messagebox.showerror("Could not save", str(e))

    def _write(self):
        rep = self.rep
        target = Path(self.path_var.get().strip() or rep.path)
        msg = (f"Write a new sitemap with {rep.new_count} page(s) to\n{target}?\n\n"
               + ("The old file is backed up first (outside the website folder).\n" if target.exists() else "")
               + ("\nThe old file has image / video / language entries that will NOT be kept.\n" if rep.extras else ""))
        if not messagebox.askyesno("Write sitemap.xml", msg):
            return
        try:
            if target.exists():
                bdir = C.default_backup_dir(self.folder)
                C.backup_file(target, self.folder if Path(self.folder) in target.parents else target.parent, bdir)
            target.write_text(rep.new_xml, encoding="utf-8", newline="\n")
        except OSError as e:
            messagebox.showerror("Could not write", str(e))
            return
        self.app._log(f"Sitemap written: {target}  ({rep.new_count} pages)", "ok")
        messagebox.showinfo("Done", f"{target.name} was written with {rep.new_count} page(s).")


class ReviewWindow(tk.Toplevel):
    """Review before writing: pages that failed the checks. For each one the title, description and H1 can be edited
    by hand or regenerated (one field at a time, with an optional instruction), then approved — only then is the page
    written (the original is backed up first)."""

    def __init__(self, app, items: list, pipe, select_rel=None):
        super().__init__(app)
        self.app, self.pipe = app, pipe
        self.items = {it["rel"]: it for it in items}
        self.order = [it["rel"] for it in items]
        self.preps, self.drafts, self.state = {}, {}, {r: "waiting" for r in self.order}
        self.cur, self.busy, self.approved = None, False, []
        self.dry = pipe.dry
        self.title("Review before writing")
        self.configure(bg=PANEL_BG)
        center(self, app, 1200, 800)
        self.protocol("WM_DELETE_WINDOW", self._close)

        left = tk.Frame(self, bg=PANEL_BG, width=300)
        left.pack(side="left", fill="y", padx=(14, 6), pady=14)
        left.pack_propagate(False)
        tk.Label(left, text="Pages waiting", font=FONT_HEADING, fg=TEXT_PRIMARY, bg=PANEL_BG).pack(anchor="w")
        self.count_lbl = tk.Label(left, text="", font=FONT_SMALL, fg=TEXT_MUTED, bg=PANEL_BG)
        self.count_lbl.pack(anchor="w", pady=(0, 6))
        self.tree = ttk.Treeview(left, columns=("page", "state"), show="headings", style="TTM.Treeview", selectmode="browse")
        self.tree.heading("page", text="Page")
        self.tree.heading("state", text="")
        self.tree.column("page", width=190)
        self.tree.column("state", width=80, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        for r in self.order:
            self.tree.insert("", "end", iid=r, values=(r, "waiting"))

        right = tk.Frame(self, bg=PANEL_BG)
        right.pack(side="left", fill="both", expand=True, padx=(6, 14), pady=14)
        self.head = tk.Label(right, text="", font=("Segoe UI Semibold", 13), fg=TEXT_PRIMARY, bg=PANEL_BG, anchor="w")
        self.head.pack(fill="x")
        self.why = tk.Label(right, text="", font=FONT_SMALL, fg=WARNING, bg=PANEL_BG, anchor="w", justify="left",
                            wraplength=820)
        self.why.pack(fill="x", pady=(0, 6))
        self.fields = {}
        self.counters = {}
        for key, label, h, limit in (("title", "Meta title", 2, f"{C.MIN_TITLE_LEN}–{C.MAX_TITLE_LEN}"),
                                     ("desc", "Meta description", 4, f"{C.MIN_DESC_LEN}–{C.MAX_DESC_LEN}"),
                                     ("h1", "H1 heading", 2, f"up to {C.MAX_H1_LEN}")):
            box = tk.Frame(right, bg=CARD_BG, highlightbackground=CARD_BORDER, highlightthickness=1)
            box.pack(fill="x", pady=4)
            top = tk.Frame(box, bg=CARD_BG)
            top.pack(fill="x", padx=10, pady=(8, 2))
            tk.Label(top, text=label, font=("Segoe UI Semibold", 11), fg=TEXT_PRIMARY, bg=CARD_BG).pack(side="left")
            cnt = tk.Label(top, text="", font=FONT_SMALL, fg=TEXT_MUTED, bg=CARD_BG)
            cnt.pack(side="left", padx=10)
            self.counters[key] = (cnt, limit)
            Btn(top, "Regenerate", lambda k=key: self._regen(k), "secondary", padx=10, pady=3).pack(side="right")
            now = tk.Label(box, text="", font=FONT_TINY, fg=TEXT_MUTED, bg=CARD_BG, anchor="w", justify="left",
                           wraplength=820)
            now.pack(fill="x", padx=10)
            setattr(self, f"now_{key}", now)
            t = tk.Text(box, height=h, wrap="word", font=FONT_MAIN, bg=INPUT_BG, fg=TEXT_PRIMARY, relief="flat",
                        highlightthickness=1, highlightbackground=CARD_BORDER, highlightcolor=ACCENT, padx=6, pady=4,
                        undo=True)
            t.pack(fill="x", padx=10, pady=(2, 10))
            t.bind("<KeyRelease>", lambda e: self._counts())
            self.fields[key] = t

        irow = tk.Frame(right, bg=PANEL_BG)
        irow.pack(fill="x", pady=(6, 0))
        tk.Label(irow, text="Tell the AI what to change (optional)", font=FONT_SMALL, fg=TEXT_SECONDARY,
                 bg=PANEL_BG).pack(side="left")
        self.instr = tk.StringVar()
        entry(irow, self.instr).pack(side="left", fill="x", expand=True, padx=8, ipady=3)
        self.problems = tk.Label(right, text="", font=FONT_SMALL, fg=DANGER, bg=PANEL_BG, anchor="w", justify="left",
                                 wraplength=820)
        self.problems.pack(fill="x", pady=(8, 0))
        self.score = tk.Label(right, text="", font=("Segoe UI Semibold", 11), fg=TEXT_PRIMARY, bg=PANEL_BG, anchor="w",
                              justify="left", wraplength=820)
        self.score.pack(fill="x", pady=(2, 0))
        self.msg = tk.Label(right, text="", font=FONT_SMALL, fg=TEXT_MUTED, bg=PANEL_BG, anchor="w")
        self.msg.pack(fill="x")
        bar = tk.Frame(right, bg=PANEL_BG)
        bar.pack(fill="x", side="bottom", pady=(8, 0))
        self.approve_btn = Btn(bar, "✓  Approve and write" if not self.dry else "Approve (dry run — nothing is written)",
                               self._approve, "primary")
        self.approve_btn.pack(side="left")
        self.check_btn = Btn(bar, "Check and show the score", self._check, "secondary")
        self.check_btn.pack(side="left", padx=8)
        self.skip_btn = Btn(bar, "Skip for now", self._skip, "ghost")
        self.skip_btn.pack(side="left")
        Btn(bar, "Close", self._close, "ghost").pack(side="right")
        self._refresh_count()
        first = select_rel if select_rel in self.items else self.order[0]
        self.tree.selection_set(first)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _text(self, key) -> str:
        return self.fields[key].get("1.0", "end").strip()

    def _set_text(self, key, value):
        t = self.fields[key]
        t.delete("1.0", "end")
        t.insert("1.0", value or "")

    def _refresh_count(self):
        left = sum(1 for v in self.state.values() if v == "waiting")
        self.count_lbl.config(text=f"{left} waiting · {len(self.approved)} approved")

    def _busy_ui(self, on):
        self.busy = on
        for b in (self.approve_btn, self.check_btn, self.skip_btn):
            b.set_enabled(not on and not (b is self.approve_btn and self.dry))

    def _counts(self):
        o = self.pipe.o
        title = C.build_title(C.strip_brand(self._text("title"), o.brand), o.sep, o.brand) if self._text("title") else ""
        for key, text, lo, hi in (("title", title, C.MIN_TITLE_LEN, C.MAX_TITLE_LEN),
                                  ("desc", self._text("desc"), C.MIN_DESC_LEN, C.MAX_DESC_LEN),
                                  ("h1", self._text("h1"), 1, C.MAX_H1_LEN)):
            cnt, limit = self.counters[key]
            n = len(text)
            ok = lo <= n <= hi
            cnt.config(text=f"{n} characters  (aim for {limit})", fg=SUCCESS if ok else DANGER)

    def _bg(self, fn, done):
        if self.busy:
            return
        self._busy_ui(True)
        self.msg.config(text="Working…", fg=TEXT_MUTED)

        def work():
            try:
                out = ("ok", fn())
            except Exception as e:                               # noqa: BLE001
                out = ("err", str(e))
            self.app._ui(lambda: self._after(out, done))
        threading.Thread(target=work, daemon=True).start()

    def _after(self, out, done):
        if not self.winfo_exists():
            return
        self._busy_ui(False)
        if out[0] == "ok":
            self.msg.config(text="", fg=TEXT_MUTED)
            done(out[1])
        else:
            self.msg.config(text=out[1], fg=DANGER)

    # ── selecting a page ─────────────────────────────────────────────────────
    def _save_draft(self):
        if self.cur and self.cur in self.preps:
            self.drafts[self.cur] = {k: self._text(k) for k in self.fields}

    def _on_select(self):
        sel = self.tree.selection()
        if not sel or sel[0] == self.cur or self.busy:
            return
        self._save_draft()
        self.cur = sel[0]
        rel = self.cur
        self.head.config(text=rel)
        item = self.items[rel]
        self.why.config(text="Held back because: " + (item.get("reason") or "needs a decision"))
        self.problems.config(text="")
        self.score.config(text="")
        if rel in self.preps:
            self._fill(rel)
            return
        for k in self.fields:
            self._set_text(k, "")

        def load():
            p, err = self.pipe.review_open(rel)
            if err:
                raise RuntimeError(err)
            p["res"]["review"] = item["review"]
            return p

        def done(p):
            self.preps[rel] = p
            if self.cur == rel:
                self._fill(rel)
        self._bg(load, done)

    def _fill(self, rel):
        p, item = self.preps[rel], self.items[rel]
        o, res = self.pipe.o, self.preps[rel]["res"]
        self.now_title.config(text="On the page now: " + (res["old_title"] or "(none)"))
        self.now_desc.config(text="On the page now: " + (res["old_description"] or "(none)"))
        self.now_h1.config(text="On the page now: " + (p["h1"] or "(none)") + ("" if o.do_h1 else "   (H1 updates are switched off)"))
        d = self.drafts.get(rel)
        if d:
            for k, v in d.items():
                self._set_text(k, v)
        else:
            rv = item["review"]
            self._set_text("title", C.build_title(rv["core"], o.sep, o.brand) if rv.get("core") else "")
            self._set_text("desc", rv.get("desc", ""))
            self._set_text("h1", rv.get("h1") or p["h1"])
        self._counts()
        self.problems.config(text="  •  ".join(item["review"].get("problems", [])))
        self.msg.config(text="")

    # ── actions ──────────────────────────────────────────────────────────────
    def _current(self):
        rel = self.cur
        p = self.preps.get(rel) if rel else None
        return rel, p

    def _regen(self, field):
        rel, p = self._current()
        if p is None or self.busy:
            return
        texts = (self._text("title"), self._text("desc"), self._text("h1"))
        instr = self.instr.get().strip()

        def work():
            vals, probs = self.pipe.review_check(p, *texts)
            if vals["title"] is None:
                vals["title"] = ""
            return self.pipe.review_regen(p, field, vals, probs.get(field, []), instr)

        def done(res):
            text, probs = res
            if text:
                self._set_text(field, C.build_title(text, self.pipe.o.sep, self.pipe.o.brand) if field == "title" else text)
                self._counts()
            self.problems.config(text=("  •  ".join(probs)) if probs else "")
            self.msg.config(text=("The new text did not pass every check — edit it or regenerate again." if probs else
                                  "New version inserted. Check it, edit it if you like, then approve."),
                            fg=WARNING if probs else SUCCESS)
        self._bg(work, done)

    def _evaluate(self, p):
        texts = (self._text("title"), self._text("desc"), self._text("h1"))
        vals, probs = self.pipe.review_check(p, *texts)
        html, info, blob = self.pipe.review_preview(p, vals)
        return vals, probs, blob

    def _show_eval(self, p, probs, blob):
        flat = [f"{k}: {x}" for k in ("title", "desc", "h1") for x in probs.get(k, [])]
        self.problems.config(text="  •  ".join(flat))
        base = p.get("gate") or p["res"].get("score_before")
        if blob and base:
            d = blob["t"] - base["t"]
            lower = [f"{S.CAT_NAME[c]} {blob['cats'][c] - v:+.2f}" for c, v in base["cats"].items()
                     if blob["cats"][c] < v - 0.005]
            txt = f"Score   {p['res']['score_before']['t']:.2f}  →  {blob['t']:.2f}   ({blob['t'] - p['res']['score_before']['t']:+.2f})"
            if lower:
                txt += "\nLower than before: " + ", ".join(lower)
            self.score.config(text=txt, fg=SUCCESS if not lower and d >= -0.005 else WARNING)
        else:
            self.score.config(text="")

    def _check(self):
        rel, p = self._current()
        if p is None or self.busy:
            return
        self._bg(lambda: self._evaluate(p), lambda r: self._show_eval(p, r[1], r[2]))

    def _approve(self):
        rel, p = self._current()
        if p is None or self.busy or self.dry:
            return

        def work():
            vals, probs, blob = self._evaluate(p)
            if any(probs.values()):
                return ("problems", probs, blob, None)
            return ("ok", probs, blob, self.pipe.review_apply(p, vals))

        def done(r):
            kind, probs, blob, res = r
            if kind == "problems":
                self._show_eval(p, probs, blob)
                self.msg.config(text="Fix the points above first, then approve again.", fg=DANGER)
                return
            self.state[rel] = "approved"
            self.approved.append(res)
            self.tree.item(rel, values=(rel, "approved ✓"))
            self.app._log(f"{rel}: approved after review", "ok")
            self._next()
        self._bg(work, done)

    def _skip(self):
        rel = self.cur
        if rel and not self.busy:
            self.state[rel] = "skipped"
            self.tree.item(rel, values=(rel, "skipped"))
            self._next()

    def _next(self):
        self._refresh_count()
        for r in self.order:
            if self.state[r] == "waiting":
                self.cur = None
                self.tree.selection_set(r)
                return
        self.msg.config(text="All pages are done — close this window to refresh the table and the report.", fg=SUCCESS)

    def _close(self):
        if self.busy:
            return
        self.app._review_closed(self.approved, self.pipe)
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# Files tab — checkbox tree
# ─────────────────────────────────────────────────────────────────────────────
class FileTree(tk.Frame):
    ON, OFF, MIX = "☑", "☐", "◪"

    def __init__(self, parent, on_change):
        super().__init__(parent, bg=DARK_BG)
        self.on_change = on_change
        self.tree = ttk.Treeview(self, show="tree", style="Files.Treeview", selectmode="browse")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<Double-1>", lambda e: "break")
        self.tree.bind("<space>", self._space)
        self.nodes = {}
        self.checked = {}
        self._labels = {}

    def load(self, root, files, excluded: set):
        self.tree.delete(*self.tree.get_children())
        self.nodes, self.checked, self._labels = {}, {}, {}
        for fp in files:
            rel = C.rel_posix(fp, root)
            parts = rel.split("/")
            parent = ""
            for i, name in enumerate(parts[:-1]):
                did = "d:" + "/".join(parts[:i + 1])
                if did not in self.nodes:
                    self.tree.insert(parent, "end", iid=did, text=name, open=(i == 0 and False))
                    self.nodes[did] = {"type": "dir", "name": name, "parent": parent, "children": []}
                    if parent:
                        self.nodes[parent]["children"].append(did)
                parent = did
            fid = "f:" + rel
            self.tree.insert(parent, "end", iid=fid, text=parts[-1])
            self.nodes[fid] = {"type": "file", "name": parts[-1], "parent": parent, "children": [], "rel": rel}
            if parent:
                self.nodes[parent]["children"].append(fid)
            self.checked[fid] = rel not in excluded
        self._refresh()

    # state helpers
    def _counts(self):
        tot, chk = {}, {}
        for fid, on in self.checked.items():
            p = self.nodes[fid]["parent"]
            while p:
                tot[p] = tot.get(p, 0) + 1
                chk[p] = chk.get(p, 0) + (1 if on else 0)
                p = self.nodes[p]["parent"]
        return tot, chk

    def _refresh(self):
        tot, chk = self._counts()
        for iid, nd in self.nodes.items():
            if nd["type"] == "file":
                label = f"{self.ON if self.checked[iid] else self.OFF}  {nd['name']}"
            else:
                t, c = tot.get(iid, 0), chk.get(iid, 0)
                mark = self.ON if c == t else (self.OFF if c == 0 else self.MIX)
                label = f"{mark}  📁 {nd['name']}  ({c}/{t})"
            if self._labels.get(iid) != label:
                self._labels[iid] = label
                self.tree.item(iid, text=label)
        self.on_change()

    def _descendant_files(self, iid):
        nd = self.nodes[iid]
        if nd["type"] == "file":
            return [iid]
        out = []
        for ch in nd["children"]:
            out.extend(self._descendant_files(ch))
        return out

    def toggle(self, iid):
        files = self._descendant_files(iid)
        if not files:
            return
        new = not all(self.checked[f] for f in files)
        for f in files:
            self.checked[f] = new
        self._refresh()

    def set_all(self, on: bool):
        for f in self.checked:
            self.checked[f] = on
        self._refresh()

    def _click(self, e):
        if self.tree.identify_region(e.x, e.y) not in ("tree", "cell"):
            return
        if "indicator" in self.tree.identify_element(e.x, e.y):
            return
        iid = self.tree.identify_row(e.y)
        if iid:
            self.tree.selection_set(iid)
            self.toggle(iid)
            return "break"

    def _space(self, e):
        sel = self.tree.selection()
        if sel:
            self.toggle(sel[0])
            return "break"

    def expand_all(self, on=True):
        for iid, nd in self.nodes.items():
            if nd["type"] == "dir":
                self.tree.item(iid, open=on)

    def find(self, text):
        text = text.lower().strip()
        if not text:
            return
        for iid, nd in self.nodes.items():
            if text in nd["name"].lower():
                p = nd["parent"]
                while p:
                    self.tree.item(p, open=True)
                    p = self.nodes[p]["parent"]
                self.tree.see(iid)
                self.tree.selection_set(iid)
                return

    def excluded(self) -> set:
        return {self.nodes[f]["rel"] for f, on in self.checked.items() if not on}

    def selected_count(self):
        return sum(1 for on in self.checked.values() if on)


# ─────────────────────────────────────────────────────────────────────────────
# Main application
# ─────────────────────────────────────────────────────────────────────────────
CANON_TXT = {"added": "✓ added", "fixed": "✓ fixed", "ok": "ok", "other domain": "other domain",
             "removed": "removed", "no <head>": "no <head>", "error": "error"}
STATUS_TXT = {"kept": "🛡 Kept", "updated": "✅ Updated", "preview": "🔍 Preview", "review": "⚠ Review",
              "failed": "❌ Failed", "skipped": "⏭ Skipped"}
STATUS_TAG = {"updated": "ok", "preview": "dry", "review": "warning", "failed": "error", "skipped": "muted",
              "kept": "kept"}


class SEOUpdaterApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        # Fit the screen: a fixed 880 px height hid the bottom bar (Run / View Report) on 768–864 px laptop screens.
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1360, sw - 60), min(880, sh - 120)
        self.geometry(f"{w}x{h}+{max((sw - w) // 2, 0)}+{max((sh - h) // 2 - 20, 0)}")
        self.minsize(min(1100, w), min(640, h))
        if sh < 950 and sys.platform == "win32":
            self.state("zoomed")                      # small screens: use the whole working area
        self.configure(bg=DARK_BG)
        for ico in (Path(__file__).parent / "icon.ico", Path.cwd() / "icon.ico"):
            if ico.exists():
                try:
                    self.iconbitmap(str(ico))
                except Exception:
                    pass
                break

        self.files, self.results = [], []
        self.filter = "all"
        self._stop_event = threading.Event()
        self._settings = load_settings()
        self._api_key = load_api_key()
        self._scrollers = []
        self._run_started = 0.0
        self._running = False
        self.business = dict(self._settings.get("business") or {})
        self.business_verified = bool(self._settings.get("business_verified"))
        self._excluded_by_folder = dict(self._settings.get("excluded") or {})
        self.last_report_path = ""
        self._scan_done = False
        self.score_items, self.score_summary = [], None
        self._sc = {"n": 0, "b": 0.0, "a": 0.0}
        self._q = queue.Queue()          # worker threads hand UI updates to the main thread here

        self._setup_style()
        self._build_ui()
        self._load_settings_to_ui()
        self.bind_all("<MouseWheel>", self._on_wheel)
        self.after(40, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── styling ──────────────────────────────────────────────────────────────
    def _setup_style(self):
        st = ttk.Style()
        st.theme_use("clam")
        st.configure("Vertical.TScrollbar", background=CARD_BORDER, troughcolor=DARK_BG, bordercolor=DARK_BG,
                     arrowcolor=TEXT_MUTED, lightcolor=CARD_BORDER, darkcolor=CARD_BORDER, relief="flat")
        st.map("Vertical.TScrollbar", background=[("active", "#aab4cc")])
        st.configure("TCombobox", fieldbackground=INPUT_BG, background=CARD_BORDER, foreground=TEXT_PRIMARY,
                     arrowcolor=TEXT_SECONDARY, bordercolor=CARD_BORDER, lightcolor=INPUT_BG,
                     darkcolor=INPUT_BG, padding=5)
        st.map("TCombobox", fieldbackground=[("readonly", INPUT_BG)], foreground=[("readonly", TEXT_PRIMARY)],
               selectbackground=[("readonly", INPUT_BG)], selectforeground=[("readonly", TEXT_PRIMARY)])
        self.option_add("*TCombobox*Listbox.background", PANEL_BG)
        self.option_add("*TCombobox*Listbox.foreground", TEXT_PRIMARY)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.font", FONT_MAIN)
        st.configure("TTM.Horizontal.TProgressbar", troughcolor=CARD_BG, background=ACCENT, thickness=8,
                     borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT, bordercolor=CARD_BG)
        st.configure("TTM.Treeview", background=CARD_BG, foreground=TEXT_PRIMARY, fieldbackground=CARD_BG,
                     borderwidth=0, rowheight=27, font=FONT_SMALL)
        st.configure("TTM.Treeview.Heading", background="#e3e8f2", foreground=TEXT_PRIMARY,
                     font=("Segoe UI Semibold", 9), relief="flat", borderwidth=0)
        st.map("TTM.Treeview", background=[("selected", SELECT_BG)], foreground=[("selected", TEXT_PRIMARY)])
        st.map("TTM.Treeview.Heading", background=[("active", "#d2daea")])
        st.configure("Files.Treeview", background=CARD_BG, foreground=TEXT_PRIMARY, fieldbackground=CARD_BG,
                     borderwidth=0, rowheight=25, font=FONT_SMALL)
        st.map("Files.Treeview", background=[("selected", SELECT_BG)], foreground=[("selected", TEXT_PRIMARY)])

    # ── scroll routing (one global binding; works inside popups too) ─────────
    def register_scroll(self, sf: ScrollFrame):
        self._scrollers.append(sf)

    def _on_wheel(self, e):
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None:
            if isinstance(w, (tk.Text, ttk.Treeview, tk.Listbox, ttk.Combobox)):
                return
            for sf in self._scrollers:
                try:
                    if w is sf or w is sf.canvas or w is sf.inner:
                        sf.canvas.yview_scroll(-1 * (e.delta // 120), "units")
                        return
                except tk.TclError:
                    continue
            w = getattr(w, "master", None)

    # ── layout ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        top = tk.Frame(self, bg=PANEL_BG, height=60)
        top.pack(fill="x", side="top")
        top.pack_propagate(False)
        lf = tk.Frame(top, bg=PANEL_BG)
        lf.pack(side="left", padx=20)
        tk.Label(lf, text=" Top Ten Website Marketing", image=icon("rocket"), compound="left",
                 font=("Segoe UI Semibold", 10), fg=ACCENT2, bg=PANEL_BG).pack(anchor="w", pady=(8, 0))
        tk.Label(lf, text="SEO Meta Tag Updater", font=FONT_LOGO, fg=TEXT_PRIMARY, bg=PANEL_BG).pack(anchor="w")
        tk.Label(top, text=f" v{APP_VERSION} ", font=FONT_TINY, fg=ACCENT, bg=CARD_BORDER).pack(side="left", pady=22)
        self.status_dot = tk.Label(top, text="●", font=("Segoe UI", 13), fg=TEXT_MUTED, bg=PANEL_BG)
        self.status_dot.pack(side="right", padx=(6, 20))
        self.status_lbl = tk.Label(top, text="Ready", font=FONT_MAIN, fg=TEXT_MUTED, bg=PANEL_BG)
        self.status_lbl.pack(side="right")

        bar = tk.Frame(self, bg=PANEL_BG, height=62)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.run_btn = Btn(bar, "▶   Run SEO Update", self._run, "primary", font=("Segoe UI Semibold", 11), padx=22)
        self.run_btn.pack(side="right", padx=(6, 16), pady=11)
        self.stop_btn = Btn(bar, "⏹  Stop", self._stop, "danger")
        self.stop_btn.pack(side="right", pady=11)
        self.stop_btn.set_enabled(False)
        self.report_btn = Btn(bar, "🌐  View Report", self._view_report, "accent2")
        self.report_btn.pack(side="right", padx=8, pady=11)
        self.report_btn.set_enabled(False)          # becomes available once a scan has completed
        self.retry_btn = Btn(bar, "↻  Retry failed", self._retry_failed, "secondary")
        self.retry_btn.pack(side="right", pady=11)
        self.review_btn = Btn(bar, "Review pages…", self._open_review, "secondary")
        self.review_btn.pack(side="right", padx=(0, 8), pady=11)
        Btn(bar, "🗑  Clear log", self._clear_log, "ghost").pack(side="left", padx=(16, 4), pady=11)
        Btn(bar, "⟲  Restore backup…", self._restore_backup, "ghost").pack(side="left", pady=11)
        self.bottom_var = tk.StringVar()
        tk.Label(bar, textvariable=self.bottom_var, font=FONT_SMALL, fg=WARNING, bg=PANEL_BG).pack(side="left", padx=14)

        body = tk.Frame(self, bg=DARK_BG)
        body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg=DARK_BG, width=520)
        left.pack(side="left", fill="y", padx=(14, 7), pady=12)
        left.pack_propagate(False)
        right = tk.Frame(body, bg=DARK_BG)
        right.pack(side="left", fill="both", expand=True, padx=(7, 14), pady=12)
        self._build_left(left)
        self._build_right(right)

    # ── left: tabs ───────────────────────────────────────────────────────────
    def _build_left(self, parent):
        self.tab_names = ["① Project", "② Business", "③ Files", "④ Options", "⑤ AI & Cost"]
        tabbar = tk.Frame(parent, bg=DARK_BG)
        tabbar.pack(fill="x")
        self.tab_btns, self.tab_lines, self.tab_frames = {}, {}, {}
        for name in self.tab_names:
            cell = tk.Frame(tabbar, bg=DARK_BG)
            cell.pack(side="left", expand=True, fill="x")
            lbl = tk.Label(cell, text=" " + name.split(" ", 1)[1], image=icon(TAB_ICONS[name]), compound="left",
                           font=("Segoe UI Semibold", 10), fg=TEXT_MUTED, bg=DARK_BG, cursor="hand2", pady=8)
            lbl.pack(fill="x")
            line = tk.Frame(cell, bg=CARD_BORDER, height=2)
            line.pack(fill="x")
            lbl.bind("<Button-1>", lambda e, n=name: self._show_tab(n))
            self.tab_btns[name], self.tab_lines[name] = lbl, line
        holder = tk.Frame(parent, bg=DARK_BG)
        holder.pack(fill="both", expand=True, pady=(10, 0))
        for name in self.tab_names:
            if name == "③ Files":
                fr = tk.Frame(holder, bg=DARK_BG)
            else:
                fr = ScrollFrame(holder)
                self.register_scroll(fr)
            self.tab_frames[name] = fr
        self._build_project(self.tab_frames["① Project"].inner)
        self._build_business(self.tab_frames["② Business"].inner)
        self._build_files(self.tab_frames["③ Files"])
        self._build_options(self.tab_frames["④ Options"].inner)
        self._build_ai(self.tab_frames["⑤ AI & Cost"].inner)
        self._show_tab(self.tab_names[0])

    def _show_tab(self, name):
        for n, fr in self.tab_frames.items():
            fr.pack_forget()
            on = n == name
            self.tab_btns[n].config(fg=TEXT_PRIMARY if on else TEXT_MUTED)
            self.tab_lines[n].config(bg=ACCENT if on else CARD_BORDER)
        self.tab_frames[name].pack(fill="both", expand=True)

    # ── tab 1: project ───────────────────────────────────────────────────────
    def _build_project(self, p):
        c = card(p, "📁  Website folder",
                 "Pick the folder that is the root of the website (the one that contains index.html and your images folder).")
        row = tk.Frame(c, bg=CARD_BG)
        row.pack(fill="x")
        self.folder_var = tk.StringVar()
        entry(row, self.folder_var).pack(side="left", fill="x", expand=True, ipady=6)
        Btn(row, "Browse…", self._browse_folder, "primary", pady=5).pack(side="left", padx=(8, 0))
        self.recursive_var = tk.BooleanVar(value=True)
        toggle(c, "Include sub-folders", self.recursive_var, command=self._on_folder_change)
        self.file_count_lbl = tk.Label(c, text="No folder selected", font=FONT_SMALL, fg=TEXT_MUTED, bg=CARD_BG)
        self.file_count_lbl.pack(anchor="w", pady=(6, 0))
        self.folder_var.trace_add("write", lambda *a: self._schedule_folder_change())
        self._folder_after = None

        c = card(p, "⚙️  Processing mode")
        self.mode_var = tk.StringVar(value="dry_run")
        self.mode_frames = {}
        modes = [
            ("dry_run", "🔍  Dry run", "Preview what the AI would write and estimate cost. Nothing is changed."),
            ("per_page", "📄  One page per AI call", "Most careful and accurate. Higher cost."),
            ("batch", f"⚡  Batch ({A.BATCH_SIZE} similar pages per call)", "Cheaper and faster. Similar pages are grouped so the AI can tell them apart."),
        ]
        for val, label, desc in modes:
            f = tk.Frame(c, bg=CARD_BG, cursor="hand2", highlightthickness=1, highlightbackground=CARD_BORDER)
            f.pack(fill="x", pady=3)
            ic, label_txt = split_icon(label)
            tk.Radiobutton(f, text="  " + label_txt, image=icon(ic, True), compound="left", variable=self.mode_var,
                           value=val, bg=CARD_BG, fg=TEXT_PRIMARY, selectcolor=INPUT_BG, activebackground=CARD_BG,
                           font=FONT_CARD, bd=0, highlightthickness=0,
                           command=self._on_mode_change).pack(anchor="w", padx=8, pady=(6, 0))
            tk.Label(f, text=desc, font=FONT_TINY, fg=TEXT_MUTED, bg=CARD_BG, justify="left",
                     wraplength=420).pack(anchor="w", padx=32, pady=(0, 6))
            f.bind("<Button-1>", lambda e, v=val: (self.mode_var.set(v), self._on_mode_change()))
            self.mode_frames[val] = f

        self.resume_card = card(p, "⏯  Saved progress")
        self.resume_lbl = tk.Label(self.resume_card, text="", font=FONT_SMALL, fg=TEXT_SECONDARY, bg=CARD_BG,
                                   justify="left", wraplength=430)
        self.resume_lbl.pack(anchor="w")

    # ── tab 2: business ──────────────────────────────────────────────────────
    def _build_business(self, p):
        c = card(p, "🌐  Website", "The live address of the site. Fetch Details reads your phone, address, hours, social links, services and service areas from it.")
        tk.Label(c, text="Site URL", font=FONT_SMALL, fg=TEXT_SECONDARY, bg=CARD_BG).pack(anchor="w")
        row = tk.Frame(c, bg=CARD_BG)
        row.pack(fill="x", pady=(2, 4))
        self.site_url_var = tk.StringVar()
        entry(row, self.site_url_var).pack(side="left", fill="x", expand=True, ipady=6)
        self.fetch_btn = Btn(row, "🔍  Fetch Details", self._fetch_details, "accent2", pady=5)
        self.fetch_btn.pack(side="left", padx=(8, 0))
        self.site_name_var = tk.StringVar()
        self.site_context_var = tk.StringVar()
        labeled(c, "Brand / business name", self.site_name_var, "Added to the end of every title.")
        tk.Label(c, text="Business summary — services, areas and details", font=FONT_SMALL, fg=TEXT_SECONDARY,
                 bg=CARD_BG).pack(anchor="w")
        self.context_box = text_box(c, 8)
        self.context_box.pack(fill="x", pady=(2, 2))
        tk.Label(c, text="Fetch Details fills this in with all services, service locations and other details found on the "
                         "site. It tells the AI what the business offers. Edit it freely.", font=FONT_TINY, fg=TEXT_MUTED,
                 bg=CARD_BG, justify="left", wraplength=430).pack(anchor="w", pady=(0, 6))
        self.context_box.bind("<KeyRelease>", lambda e: self.site_context_var.set(self.context_box.get("1.0", "end").strip()))
        self.site_context_var.trace_add("write", lambda *a: self._sync_context_box())

        c = card(p, "✏️  Title format", "Titles become:  AI-written title  +  separator  +  brand.  The AI is told how many characters it has left.")
        tk.Label(c, text="Separator", font=FONT_SMALL, fg=TEXT_SECONDARY, bg=CARD_BG).pack(anchor="w")
        self.sep_var = tk.StringVar(value="|")
        sep_row = tk.Frame(c, bg=CARD_BG)
        sep_row.pack(fill="x", pady=(2, 4))
        ttk.Combobox(sep_row, textvariable=self.sep_var, values=SEPARATORS, width=8, font=FONT_MAIN).pack(side="left")
        self.sep_example = tk.Label(c, text="", font=FONT_SMALL, fg=ACCENT, bg=CARD_BG, justify="left", wraplength=430)
        self.sep_example.pack(anchor="w", pady=(2, 0))
        self.sep_var.trace_add("write", lambda *a: self._update_sep_example())
        self.site_name_var.trace_add("write", lambda *a: self._update_sep_example())

        c = card(p, "🖼  Default share image",
                 "Used on pages that have no usable image of their own (for og:image and schema). "
                 "Each page first uses its own og:image or its first real content image — resolved correctly from the page's folder.")
        row = tk.Frame(c, bg=CARD_BG)
        row.pack(fill="x")
        self.default_image_var = tk.StringVar()
        entry(row, self.default_image_var).pack(side="left", fill="x", expand=True, ipady=6)
        Btn(row, "Browse…", self._browse_image, "secondary", pady=5).pack(side="left", padx=(8, 0))
        tk.Label(c, text="Site-root path (e.g. /images/share.jpg) or full URL. Best size: 1200×630.",
                 font=FONT_TINY, fg=TEXT_MUTED, bg=CARD_BG).pack(anchor="w", pady=(4, 0))

        c = card(p, "🏢  Business details for schema")
        self.biz_chip = tk.Label(c, text="", font=FONT_SMALL, bg=CARD_BG)
        self.biz_chip.pack(anchor="w")
        self.biz_summary = tk.Label(c, text="", font=FONT_SMALL, fg=TEXT_SECONDARY, bg=CARD_BG, justify="left", wraplength=430)
        self.biz_summary.pack(anchor="w", pady=(2, 8))
        Btn(c, "✎  Review & edit details", self._edit_business, "secondary").pack(anchor="w")
        tk.Label(c, text="Phone, address, hours and price range come only from these verified details — "
                         "the AI never makes them up.", font=FONT_TINY, fg=TEXT_MUTED, bg=CARD_BG,
                 wraplength=430, justify="left").pack(anchor="w", pady=(8, 0))

    # ── tab 3: files ─────────────────────────────────────────────────────────
    def _build_files(self, p):
        top = tk.Frame(p, bg=DARK_BG)
        top.pack(fill="x", pady=(0, 6))
        self.files_summary = tk.Label(top, text="Choose a folder first", font=FONT_SMALL, fg=TEXT_SECONDARY, bg=DARK_BG)
        self.files_summary.pack(side="left")
        btns = tk.Frame(p, bg=DARK_BG)
        btns.pack(fill="x", pady=(0, 6))
        Btn(btns, "Select all", lambda: self.ftree.set_all(True), "ghost", pady=4, padx=10).pack(side="left")
        Btn(btns, "Select none", lambda: self.ftree.set_all(False), "ghost", pady=4, padx=10).pack(side="left", padx=4)
        Btn(btns, "Expand", lambda: self.ftree.expand_all(True), "ghost", pady=4, padx=10).pack(side="left")
        Btn(btns, "Collapse", lambda: self.ftree.expand_all(False), "ghost", pady=4, padx=10).pack(side="left", padx=4)
        self.find_var = tk.StringVar()
        fe = entry(btns, self.find_var)
        fe.pack(side="right", ipady=4)
        tk.Label(btns, text="Find:", font=FONT_SMALL, fg=TEXT_MUTED, bg=DARK_BG).pack(side="right", padx=(0, 4))
        fe.bind("<Return>", lambda e: self.ftree.find(self.find_var.get()))
        tk.Label(p, text="Untick anything you want left untouched. Click a folder to tick/untick everything inside it.",
                 font=FONT_TINY, fg=TEXT_MUTED, bg=DARK_BG, wraplength=430, justify="left").pack(anchor="w", pady=(0, 6))
        self.ftree = FileTree(p, self._on_selection_change)
        self.ftree.pack(fill="both", expand=True)
        tk.Label(p, text="Also leave untouched (one pattern per line — e.g.  includes/   thank-you.html   */admin/*)",
                 font=FONT_SMALL, fg=TEXT_SECONDARY, bg=DARK_BG).pack(anchor="w", pady=(8, 2))
        self.patterns = text_box(p, 3)
        self.patterns.pack(fill="x")

    # ── tab 4: options ───────────────────────────────────────────────────────
    def _build_options(self, p):
        c = card(p, "🔗  Step 1 — canonical tags", "Done first, on every page, before any meta data is written.")
        self.canon_var = tk.BooleanVar(value=True)
        self.skip_ht_var = tk.BooleanVar(value=True)
        self.canon_rm_var = tk.BooleanVar(value=True)
        self.clean_var = tk.BooleanVar(value=False)
        self.links_var = tk.BooleanVar(value=True)
        self.htaccess_var = tk.StringVar(value="")
        toggle(c, "Add / fix canonical tags first", self.canon_var,
               "Adds the canonical tag to pages that have none and fixes one that points to a different page of this "
               "site (or the wrong www / https form). A canonical that points to another domain is kept and flagged. "
               "Needs the website address on the Project tab.", command=self._update_estimate)
        toggle(c, "Leave redirected and gone pages out", self.skip_ht_var,
               "Reads the .htaccess in the website folder: pages with a 301 / 302 redirect or a 410 (gone) get no "
               "canonical and are skipped in step 2, which also saves AI cost. Site-wide rules (www / https, "
               "catch-alls) are never used.", command=self._update_estimate)
        toggle(c, "Remove the canonical tag from those pages", self.canon_rm_var, indent=22)
        toggle(c, "Fix internal links to redirected pages", self.links_var,
               "Links that point at a 301-redirected page are changed to point straight at the final page (menus and "
               "include files too). Links to gone (410) pages, temporary (302) redirects and links that leave the "
               "site are only listed in seo_links_….csv. Uses the same .htaccess rules.")
        toggle(c, "Clean-URL site: also match /page and /page/ for page.html", self.clean_var,
               "Only for sites whose .htaccess rules are written without “.html”.", command=self._update_estimate)
        row = tk.Frame(c, bg=CARD_BG)
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text=".htaccess file", font=FONT_SMALL, fg=TEXT_SECONDARY, bg=CARD_BG).pack(side="left")
        entry(row, self.htaccess_var).pack(side="left", fill="x", expand=True, padx=8, ipady=3)
        Btn(row, "Browse…", self._browse_htaccess, "secondary", padx=10, pady=4).pack(side="left")
        brow = tk.Frame(c, bg=CARD_BG)
        brow.pack(anchor="w", pady=(8, 0))
        Btn(brow, "Check the rules…", self._show_htaccess_rules, "secondary").pack(side="left")
        Btn(brow, "Check sitemap.xml…", self._show_sitemap, "secondary").pack(side="left", padx=(8, 0))
        tk.Label(c, text="Leave the file box empty to use the .htaccess in the website folder. “Check the rules” lists "
                         "which pages would be left out and which rules are ignored.",
                 font=FONT_TINY, fg=TEXT_MUTED, bg=CARD_BG, justify="left", wraplength=430).pack(anchor="w", pady=(6, 0))

        c = card(p, "🛠  What to update", "Meta title and description are always written.")
        self.update_h1_var = tk.BooleanVar(value=True)
        self.schema_var = tk.BooleanVar(value=True)
        self.og_var = tk.BooleanVar(value=True)
        self.robots_var = tk.BooleanVar(value=True)
        self.lazy_var = tk.BooleanVar(value=True)
        self.alt_var = tk.BooleanVar(value=True)
        self.ai_alt_var = tk.BooleanVar(value=True)
        self.h1fix_var = tk.BooleanVar(value=False)
        self.crumb_var = tk.BooleanVar(value=True)
        self.faq_var = tk.BooleanVar(value=True)
        toggle(c, "H1 heading", self.update_h1_var, "Only the text changes — spans, links and icons inside the H1 stay. Pages with several H1s are reported, not changed.")
        toggle(c, "JSON-LD schema", self.schema_var, "Merged into your existing schema (FAQ, Breadcrumb, Review… are kept). Contact facts come only from your verified business details.")
        toggle(c, "Breadcrumb markup", self.crumb_var, "Adds BreadcrumbList (Home › section › page) to pages that have none. "
               "Sections are only listed when they have their own index page. Needs “JSON-LD schema”.", indent=22)
        toggle(c, "FAQ markup", self.faq_var, "Adds FAQPage when the page already shows at least two questions with answers "
               "(question headings, <details> or definition lists). Nothing is invented. Needs “JSON-LD schema”.", indent=22)
        toggle(c, "One H1 per page", self.h1fix_var, "Pages with several H1 tags: the best one is kept (inside <main>, matching the "
               "title) and the others become H2. Check the look of those pages afterwards — some themes style H1 and H2 differently.")
        toggle(c, "Social tags (Open Graph & Twitter)", self.og_var, "Updates in place; other tags such as article:author and twitter:site are kept.")
        toggle(c, "Robots meta tag", self.robots_var, "Added as “index, follow” only when the page has none.")
        toggle(c, "Lazy-load images", self.lazy_var, "Skips the logo, header/nav images, the first content image and SVGs.")
        toggle(c, "Alt text for images missing it", self.alt_var, "Uses a meaningful file name when there is one, else nearby headings. SVGs are never touched.", command=self._on_alt_toggle)
        self.ai_alt_row = toggle(c, "Ask the AI for unclear file names", self.ai_alt_var, "One small extra call per page (IMG_1234.jpg → described from context).", indent=22)

        c = card(p, "🛡  Safety")
        self.backup_var = tk.BooleanVar(value=True)
        toggle(c, "Back up originals first", self.backup_var,
               "Copies go to TTM_SEO_Backups in your user folder (never inside any website folder), so nothing gets uploaded by accident. "
               "Use “Restore backup…” to undo a run.")

        c = card(p, "⏭  Skip automatically", "These files are listed as “Skipped” and never modified.")
        self.skip_partial_var = tk.BooleanVar(value=True)
        self.skip_noindex_var = tk.BooleanVar(value=True)
        self.skip_redirect_var = tk.BooleanVar(value=True)
        self.skip_error_var = tk.BooleanVar(value=True)
        toggle(c, "Partials / includes", self.skip_partial_var, "HTML fragments without <html> or <head>, e.g. header.html")
        toggle(c, "noindex pages", self.skip_noindex_var)
        toggle(c, "Redirect pages", self.skip_redirect_var, "meta-refresh redirects")
        toggle(c, "404 / error pages", self.skip_error_var)

        c = card(p, "📊  SEO score", "A 100-point internal quality score, worked out for every page before and after. "
                                    "It is our own benchmark, not a Google score.")
        self.score_var = tk.BooleanVar(value=True)
        self.deep_var = tk.BooleanVar(value=False)
        self.score_target_var = tk.StringVar(value="90")
        self.report_cost_var = tk.BooleanVar(value=False)
        self.protect_var = tk.BooleanVar(value=True)
        self.retries_var = tk.StringVar(value="3")
        toggle(c, "Calculate SEO scores", self.score_var,
               "Free. Adds Before / After columns and lets you export the client report.", command=self._update_estimate)
        toggle(c, "Never lower a page's score", self.protect_var,
               "Before a page is changed, its new score is compared with the old one. If it would go DOWN, only the "
               "parts that hurt are fetched again (the parts that helped are kept). If no version is good enough, "
               "the page is left exactly as it was. Needs “Calculate SEO scores”.", command=self._update_estimate)
        rrow = tk.Frame(c, bg=CARD_BG)
        rrow.pack(fill="x", padx=(22, 0), pady=(0, 6))
        tk.Label(rrow, text="Re-fetch at most", font=FONT_SMALL, fg=TEXT_SECONDARY, bg=CARD_BG).pack(side="left")
        ttk.Combobox(rrow, textvariable=self.retries_var, values=["1", "2", "3"], width=3, state="readonly",
                     font=FONT_MAIN).pack(side="left", padx=6)
        tk.Label(rrow, text="times per page (it stops as soon as the score is good enough)", font=FONT_SMALL,
                 fg=TEXT_MUTED, bg=CARD_BG).pack(side="left")
        toggle(c, "Deep AI quality check", self.deep_var,
               "Lets the AI also judge search intent, accuracy and wording (about 17 of the 100 points). "
               "Costs extra tokens — see the estimate on the “AI & Cost” tab.", command=self._update_estimate, indent=22)
        tk.Label(c, text="Model for the deep check", font=FONT_SMALL, fg=TEXT_SECONDARY, bg=CARD_BG).pack(anchor="w", pady=(6, 0))
        self.score_model_var = tk.StringVar(value=SAME_AS_MAIN)
        cbs = ttk.Combobox(c, textvariable=self.score_model_var, values=[SAME_AS_MAIN] + list(A.MODELS.keys()),
                           state="readonly", font=FONT_MAIN)
        cbs.pack(fill="x", pady=(2, 6))
        cbs.bind("<<ComboboxSelected>>", lambda e: self._update_estimate())
        row = tk.Frame(c, bg=CARD_BG)
        row.pack(fill="x")
        tk.Label(row, text="Target score for the report", font=FONT_SMALL, fg=TEXT_SECONDARY, bg=CARD_BG).pack(side="left")
        entry(row, self.score_target_var, width=5).pack(side="left", padx=8, ipady=3)
        toggle(c, "Show the AI cost in the report", self.report_cost_var,
               "Off by default, so the client report never mentions what the run cost. Switch it on when you want "
               "an “AI cost of this run” figure in the report.")
        tk.Label(c, text="After every run the report (HTML) and the results (CSV) are saved automatically in the same "
                         "folder you scanned (file names start with seo_report_ and seo_results_). "
                         "Click “View Report” (available after a scan) to open it; the report has its own "
                         "“Export CSV” button above the page table.",
                 font=FONT_TINY, fg=TEXT_MUTED, bg=CARD_BG, justify="left", wraplength=430).pack(anchor="w", pady=(8, 0))

    # ── tab 5: AI & cost ─────────────────────────────────────────────────────
    def _build_ai(self, p):
        c = card(p, "🔑  Anthropic API key", "Stored in the Windows credential vault, not in a file.")
        self.api_key_var = tk.StringVar(value=self._api_key)
        row = tk.Frame(c, bg=CARD_BG)
        row.pack(fill="x")
        self.api_entry = entry(row, self.api_key_var, show="•")
        self.api_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.show_key_var = tk.BooleanVar(value=False)
        tk.Checkbutton(row, text="Show", variable=self.show_key_var, command=self._toggle_key_vis, bg=CARD_BG,
                       fg=TEXT_SECONDARY, selectcolor=INPUT_BG, activebackground=CARD_BG, bd=0,
                       highlightthickness=0, font=FONT_SMALL).pack(side="left", padx=8)
        row2 = tk.Frame(c, bg=CARD_BG)
        row2.pack(fill="x", pady=(8, 0))
        Btn(row2, "💾  Save key", self._save_api_key, "accent2", pady=5).pack(side="left")
        self.key_status = tk.Label(row2, text="", font=FONT_TINY, fg=SUCCESS, bg=CARD_BG)
        self.key_status.pack(side="left", padx=8)

        c = card(p, "🤖  Models")
        tk.Label(c, text="Main model (writes the metadata)", font=FONT_SMALL, fg=TEXT_SECONDARY, bg=CARD_BG).pack(anchor="w")
        self.model_var = tk.StringVar(value=list(A.MODELS.keys())[0])
        cb = ttk.Combobox(c, textvariable=self.model_var, values=list(A.MODELS.keys()), state="readonly", font=FONT_MAIN)
        cb.pack(fill="x", pady=(2, 8))
        cb.bind("<<ComboboxSelected>>", lambda e: self._update_estimate())
        tk.Label(c, text="Model for rewrites (too long / duplicate text)", font=FONT_SMALL, fg=TEXT_SECONDARY, bg=CARD_BG).pack(anchor="w")
        self.rewrite_var = tk.StringVar(value=SAME_AS_MAIN)
        cb2 = ttk.Combobox(c, textvariable=self.rewrite_var, values=[SAME_AS_MAIN] + list(A.MODELS.keys()),
                           state="readonly", font=FONT_MAIN)
        cb2.pack(fill="x", pady=(2, 4))
        cb2.bind("<<ComboboxSelected>>", lambda e: self._update_estimate())
        tip = tk.Label(c, text="Tip: use a cheaper model for the first pass and a stronger one only for rewrites.",
                       font=FONT_TINY, fg=TEXT_MUTED, bg=CARD_BG, wraplength=430, justify="left")
        tip.pack(anchor="w")

        c = card(p, "💰  Estimated cost")
        self.estimate_lbl = tk.Label(c, text="", font=("Segoe UI Semibold", 12), fg=WARNING, bg=CARD_BG,
                                     justify="left", wraplength=430)
        self.estimate_lbl.pack(anchor="w")
        tk.Label(c, text="A rough guide only. Actual cost depends on page size, how many rewrites are needed and the "
                         "number of similar pages.", font=FONT_TINY, fg=TEXT_MUTED, bg=CARD_BG, wraplength=430,
                 justify="left").pack(anchor="w", pady=(4, 0))

    # ── right panel ──────────────────────────────────────────────────────────
    def _build_right(self, parent):
        stats = tk.Frame(parent, bg=DARK_BG)
        stats.pack(fill="x", pady=(0, 10))
        self.stat_vars = {}
        for label, key, color in [("Files", "files", ACCENT), ("Updated", "updated", SUCCESS),
                                  ("Needs review", "review", WARNING), ("Failed", "failed", DANGER),
                                  ("Skipped", "skipped", TEXT_SECONDARY), ("Protected", "protected", INFO),
                                  ("Canonical set", "canon", ACCENT), ("API cost", "cost", ACCENT2)]:
            cell = tk.Frame(stats, bg=CARD_BG, highlightbackground=CARD_BORDER, highlightthickness=1)
            cell.pack(side="left", fill="x", expand=True, padx=(0, 6))
            tk.Label(cell, text=label, font=FONT_TINY, fg=TEXT_MUTED, bg=CARD_BG).pack(pady=(8, 0))
            v = tk.StringVar(value="—")
            self.stat_vars[key] = v
            tk.Label(cell, textvariable=v, font=("Segoe UI Semibold", 16), fg=color, bg=CARD_BG).pack(pady=(0, 8))

        self.score_strip = tk.Label(parent, text="", font=("Segoe UI Semibold", 11), fg=ACCENT, bg=SOFT_ACCENT,
                                    anchor="w", justify="left", padx=14, pady=8, wraplength=820)
        self.score_strip.pack(fill="x", pady=(0, 8))
        self.score_strip.pack_forget()
        pf = tk.Frame(parent, bg=DARK_BG)
        pf.pack(fill="x", pady=(0, 8))
        self.progress_holder = pf
        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(pf, variable=self.progress_var, maximum=100, style="TTM.Horizontal.TProgressbar").pack(fill="x")
        self.progress_lbl = tk.Label(pf, text="", font=FONT_TINY, fg=TEXT_MUTED, bg=DARK_BG)
        self.progress_lbl.pack(anchor="e")

        head = tk.Frame(parent, bg=DARK_BG)
        head.pack(fill="x", pady=(2, 6))
        tk.Label(head, text="Results", font=FONT_HEADING, fg=TEXT_PRIMARY, bg=DARK_BG).pack(side="left")
        self.chips = {}
        for key, label in [("all", "All"), ("updated", "Updated"), ("review", "Review"), ("failed", "Failed"),
                           ("protected", "Protected"), ("kept", "Kept"), ("skipped", "Skipped"), ("dups", "Duplicate content")]:
            chip = tk.Label(head, text=label, font=FONT_TINY, padx=10, pady=3, cursor="hand2", bg=CARD_BG, fg=TEXT_SECONDARY)
            chip.pack(side="left", padx=(10 if key == "all" else 4, 0))
            chip.bind("<Button-1>", lambda e, k=key: self._set_filter(k))
            self.chips[key] = chip
        tk.Label(head, text="Double-click a row for details", font=FONT_TINY, fg=TEXT_MUTED, bg=DARK_BG).pack(side="right")
        self._set_filter("all", rerender=False)

        tf = tk.Frame(parent, bg=CARD_BG, highlightbackground=CARD_BORDER, highlightthickness=1)
        tf.pack(fill="both", expand=True)
        cols = ("file", "status", "before", "after", "delta", "title", "desc", "h1", "canon", "schema", "imgs", "notes", "cost")
        heads = ("Page", "Status", "Before", "After", "Δ", "Title", "Desc", "H1", "Canonical", "Schema", "Imgs", "Notes", "Cost $")
        widths = (210, 86, 52, 48, 46, 46, 46, 34, 72, 70, 40, 170, 60)
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", style="TTM.Treeview", selectmode="browse")
        for col, h, w in zip(cols, heads, widths):
            self.tree.heading(col, text=h)
            self.tree.column(col, width=w, minwidth=34, stretch=(col in ("file", "notes")))
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        for tag, col in [("ok", SUCCESS), ("error", DANGER), ("warning", WARNING), ("dry", ACCENT), ("muted", TEXT_MUTED), ("kept", INFO)]:
            self.tree.tag_configure(tag, foreground=col)
        self.tree.bind("<Double-1>", self._on_row_double_click)

        tk.Label(parent, text="Console", font=FONT_HEADING, fg=TEXT_PRIMARY, bg=DARK_BG).pack(anchor="w", pady=(10, 4))
        lf = tk.Frame(parent, bg=DARK_BG)
        lf.pack(fill="x")
        self.log = tk.Text(lf, height=7, font=FONT_MONO, bg=CONSOLE_BG, fg=CONSOLE_FG, relief="flat", highlightthickness=1, highlightbackground=CARD_BORDER, bd=0,
                           state="disabled", wrap="word", padx=8, pady=6)
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="x", expand=True)
        lsb.pack(side="right", fill="y")
        for tag, col in [("ok", SUCCESS), ("err", DANGER), ("warn", WARNING), ("info", ACCENT),
                         ("muted", TEXT_MUTED), ("dry", INFO)]:
            self.log.tag_config(tag, foreground=col)

    # ═════════════════════════════════════════════════════════════════════════
    # Small actions
    # ═════════════════════════════════════════════════════════════════════════
    def _toggle_key_vis(self):
        self.api_entry.config(show="" if self.show_key_var.get() else "•")

    def _save_api_key(self):
        key = self.api_key_var.get().strip()
        if not key:
            messagebox.showwarning("Empty key", "Please enter an API key first.")
            return
        ok = store_api_key(key)
        self.key_status.config(text="✓ Saved securely" if ok else "⚠ Saved for this session only (keyring unavailable)",
                               fg=SUCCESS if ok else WARNING)
        self.after(4000, lambda: self.key_status.config(text=""))

    def _on_alt_toggle(self):
        pass

    def _on_mode_change(self):
        sel = self.mode_var.get()
        for val, fr in self.mode_frames.items():
            on = val == sel
            clr = SOFT_ACCENT if on else CARD_BG
            fr.config(highlightbackground=ACCENT if on else CARD_BORDER)
            self._bg_recursive(fr, clr)
        self._update_estimate()

    def _bg_recursive(self, w, color):
        try:
            w.config(bg=color)
            if isinstance(w, tk.Radiobutton):
                w.config(activebackground=color)
        except tk.TclError:
            pass
        for ch in w.winfo_children():
            self._bg_recursive(ch, color)

    def _sync_context_box(self):
        cur = self.context_box.get("1.0", "end").strip()
        new = self.site_context_var.get().strip()
        if cur != new:
            self.context_box.delete("1.0", "end")
            self.context_box.insert("1.0", new)

    def _update_sep_example(self):
        brand = self.site_name_var.get().strip() or "Your Brand"
        sample = "Roof Repair in Miami, FL"
        full = C.build_title(sample, self.sep_var.get(), brand)
        room = C.title_budget(self.sep_var.get(), self.site_name_var.get().strip())
        warn = "   ⚠ brand is very long" if room < 25 else ""
        self.sep_example.config(text=f"Example:  {full}\nThe AI gets {room} characters for its part of the title.{warn}")

    def _browse_folder(self):
        f = filedialog.askdirectory(title="Select the website's root folder")
        if f:
            self.folder_var.set(f)

    def _browse_image(self):
        folder = self.folder_var.get().strip()
        f = filedialog.askopenfilename(title="Choose the default share image", initialdir=folder or None,
                                       filetypes=[("Images", "*.jpg *.jpeg *.png *.webp"), ("All files", "*.*")])
        if not f:
            return
        try:
            rel = Path(f).relative_to(folder)
            self.default_image_var.set("/" + rel.as_posix())
        except Exception:
            self.default_image_var.set(f)

    def _ui(self, fn):
        """Thread-safe: run fn on the Tk main thread."""
        self._q.put(fn)

    def _drain(self):
        try:
            for _ in range(200):
                fn = self._q.get_nowait()
                try:
                    fn()
                except Exception as e:                           # noqa: BLE001
                    print(f"UI callback error: {e}", file=sys.stderr)
        except queue.Empty:
            pass
        try:
            self.after(40, self._drain)
        except tk.TclError:
            pass

    def _set_status(self, text, color=TEXT_MUTED):
        def _u():
            self.status_lbl.config(text=text, fg=color)
            self.status_dot.config(fg=color)
        self._ui(_u)

    def _log(self, msg, tag=""):
        def _w():
            self.log.config(state="normal")
            self.log.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] ", "muted")
            self.log.insert("end", msg + "\n", tag)
            self.log.see("end")
            self.log.config(state="disabled")
        self._ui(_w)

    def _clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    # ═════════════════════════════════════════════════════════════════════════
    # Folder / files
    # ═════════════════════════════════════════════════════════════════════════
    def _schedule_folder_change(self):
        if self._folder_after:
            self.after_cancel(self._folder_after)
        self._folder_after = self.after(350, self._on_folder_change)

    def _on_folder_change(self):
        folder = self.folder_var.get().strip()
        if folder and Path(folder).is_dir():
            self.files = C.collect_html_files(folder, self.recursive_var.get())
            excl = set(self._excluded_by_folder.get(folder, []))
            self.ftree.load(Path(folder), self.files, excl)
            n = len(self.files)
            self.file_count_lbl.config(text=f"✓  Found {n} HTML file{'s' if n != 1 else ''}",
                                       fg=SUCCESS if n else WARNING)
            self.stat_vars["files"].set(str(n))
            self._refresh_resume_card()
            self._scan_done = False                 # a different folder / setting: nothing has been scanned yet
            self._refresh_report_btn()
        else:
            self.files = []
            self.ftree.load(Path("."), [], set())
            self.file_count_lbl.config(text="Folder not found" if folder else "No folder selected",
                                       fg=DANGER if folder else TEXT_MUTED)
            self.resume_lbl.config(text="")
        self._update_estimate()

    def _on_selection_change(self):
        folder = self.folder_var.get().strip()
        if folder and self.files:
            self._excluded_by_folder[folder] = sorted(self.ftree.excluded())   # survives re-scans
        n, total = self.ftree.selected_count(), len(self.files)
        self.files_summary.config(text=f"{n} of {total} files selected" if total else "Choose a folder first")
        self._update_estimate()

    def _refresh_resume_card(self):
        folder = self.folder_var.get().strip()
        try:
            st = C.RunState(folder)
            cnt = st.counts()
        except Exception:
            cnt = {}
        if cnt:
            txt = (f"This folder has saved progress:  ✓ {cnt.get('done', 0)} done   ⚠ {cnt.get('review', 0)} need review   "
                   f"✗ {cnt.get('failed', 0)} failed.\nWhen you press Run you can resume, retry only the problem pages, or start over.")
            if st.backup_dir:
                txt += f"\nBackup folder: {st.backup_dir}"
        else:
            txt = "No saved progress for this folder yet."
        self.resume_lbl.config(text=txt)

    def _selected_files(self):
        pats = self._pattern_list()
        excl = self.ftree.excluded()
        root = Path(self.folder_var.get().strip())
        out = []
        for fp in self.files:
            rel = C.rel_posix(fp, root)
            if rel in excl or C.matches_patterns(rel, pats):
                continue
            out.append(fp)
        return out

    def _pattern_list(self):
        return [l.strip() for l in self.patterns.get("1.0", "end").splitlines() if l.strip()]

    def _show_sitemap(self):
        folder = self.folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("No folder", "Choose the website folder first (tab ① Project).")
            return
        url = self._normalized_site_url()
        if not url:
            messagebox.showwarning("Website address", "Enter the website address first (tab ① Project) — "
                                                       "the sitemap check needs it.")
            return
        SitemapDialog(self, folder, url)

    def _htaccess_path(self) -> str:
        custom = self.htaccess_var.get().strip()
        if custom:
            return custom
        folder = self.folder_var.get().strip()
        return str(Path(folder) / ".htaccess") if folder else ""

    def _browse_htaccess(self):
        folder = self.folder_var.get().strip()
        f = filedialog.askopenfilename(title="Choose the .htaccess file", initialdir=folder or None,
                                       filetypes=[(".htaccess / text", "*.htaccess *.txt *"), ("All files", "*.*")])
        if f:
            self.htaccess_var.set(f)
            self._update_estimate()

    def _htaccess_hits(self):
        """(Rules, {rel: Rule}) for the pages currently selected. Cached until the file, folder or switches change."""
        path = self._htaccess_path()
        folder = self.folder_var.get().strip()
        if not path or not folder:
            return K.Rules(found=False), {}
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            mtime = 0
        key = (path, mtime, folder, len(self.files), self.clean_var.get(), len(self.ftree.excluded()),
               self.patterns.get("1.0", "end").strip())
        if getattr(self, "_ht_cache_key", None) == key:
            return self._ht_cache
        rules = K.load_htaccess(path)
        hits = {}
        if rules.found:
            root = Path(folder)
            for fp in self._selected_files():
                rel = C.rel_posix(fp, root)
                r = rules.match(rel, self.clean_var.get())
                if r:
                    hits[rel] = r
        self._ht_cache_key, self._ht_cache = key, (rules, hits)
        return self._ht_cache

    def _show_htaccess_rules(self):
        if not self.folder_var.get().strip():
            messagebox.showwarning("No folder", "Choose the website folder first (tab ① Project).")
            return
        rules, hits = self._htaccess_hits()
        win = tk.Toplevel(self)
        win.title(".htaccess rules")
        win.configure(bg=PANEL_BG)
        center(win, self, 820, 600)
        frm = tk.Frame(win, bg=PANEL_BG)
        frm.pack(fill="both", expand=True, padx=14, pady=12)
        sb = tk.Scrollbar(frm)
        sb.pack(side="right", fill="y")
        txt = tk.Text(frm, wrap="none", font=("Consolas", 10), bg=INPUT_BG, fg=TEXT_PRIMARY, relief="flat",
                      yscrollcommand=sb.set, padx=10, pady=8)
        txt.pack(fill="both", expand=True)
        sb.config(command=txt.yview)
        path = self._htaccess_path()
        out = []
        if not rules.found:
            out.append(f".htaccess not found:\n  {path}\n\nNo page can be recognised as redirected or gone.")
        else:
            c = rules.counts()
            out.append(f".htaccess: {path}")
            out.append(f"Page rules   : 301 redirect {c['301']}   ·   302 temporary {c['302']}   ·   410 gone {c['410']}")
            out.append(f"Not used     : {len(rules.ignored)} site-wide / conditional / catch-all rule(s)")
            out.append("")
            on = self.skip_ht_var.get()
            out.append(f"Pages {'that will be left out of the run' if on else 'matching a rule (the switch is OFF, so they are processed)'}: {len(hits)}")
            for rel, r in sorted(hits.items()):
                out.append(f"  {K.KIND_TEXT[r.kind]:<22} {rel}")
                out.append(f"      line {r.line}: {r.raw[:110]}")
            out.append("")
            out.append(f"Rules that are NOT used ({len(rules.ignored)})")
            for i in rules.ignored:
                out.append(f"  line {i.line}: {i.raw[:100]}")
                out.append(f"      → {i.why}")
        txt.insert("1.0", "\n".join(out))
        txt.config(state="disabled")
        Btn(win, "Close", win.destroy, "ghost").pack(pady=(0, 12))

    def _update_estimate(self):
        try:
            n = len(self._selected_files())
            if self.skip_ht_var.get() and self.folder_var.get().strip():
                n -= len(self._htaccess_hits()[1])
        except Exception:
            n = 0
        model = A.MODELS.get(self.model_var.get(), "claude-haiku-4-5")
        batch = self.mode_var.get() == "batch"
        tin, tout = (1400, 330) if batch else (1900, 330)
        if self.alt_var.get() and self.ai_alt_var.get():
            tin, tout = tin + 250, tout + 90
        per = A.cost_of(model, int(tin * 1.12), int(tout * 1.12))
        deep = ""
        if self.score_var.get() and self.deep_var.get():
            per += self._deep_cost_per_page()
            deep = " incl. deep quality check"
        total = per * n
        if n:
            txt = f"≈ ${total:,.2f}  for {n:,} pages\n(about ${per:.4f} per page with {model}{deep})"
            self.bottom_var.set(f"Estimated cost ≈ ${total:,.2f} for {n:,} pages")
        else:
            txt = "Select a folder to see an estimate."
            self.bottom_var.set("")
        self.estimate_lbl.config(text=txt)

    # ═════════════════════════════════════════════════════════════════════════
    # Settings
    # ═════════════════════════════════════════════════════════════════════════
    def _load_settings_to_ui(self):
        s = self._settings
        self.recursive_var.set(s["recursive"])
        self.mode_var.set(s["mode"])
        for k, v in A.MODELS.items():
            if s["model"] in (k, v):
                self.model_var.set(k)
        if s["rewrite_model"] in [SAME_AS_MAIN] + list(A.MODELS.keys()):
            self.rewrite_var.set(s["rewrite_model"])
        self.site_url_var.set(s["site_url"])
        self.site_name_var.set(s["site_name"])
        self.site_context_var.set(s["site_context"])
        self.sep_var.set(s["separator"])
        self.default_image_var.set(s["default_image"])
        for var, key in [(self.update_h1_var, "do_h1"), (self.schema_var, "do_schema"), (self.og_var, "do_og"),
                         (self.robots_var, "do_robots"), (self.lazy_var, "do_lazy"), (self.alt_var, "do_alt"),
                         (self.ai_alt_var, "ai_alt"), (self.backup_var, "backup"),
                         (self.skip_partial_var, "skip_partial"), (self.skip_noindex_var, "skip_noindex"),
                         (self.skip_redirect_var, "skip_redirect"), (self.skip_error_var, "skip_error")]:
            var.set(s[key])
        self.patterns.insert("1.0", s["exclude_patterns"])
        self.score_var.set(s["do_score"])
        self.deep_var.set(s["deep_check"])
        if s["score_model"] in [SAME_AS_MAIN] + list(A.MODELS.keys()):
            self.score_model_var.set(s["score_model"])
        self.score_target_var.set(str(s["score_target"]))
        self.report_cost_var.set(s["report_show_cost"])
        self.protect_var.set(s["protect_score"])
        self.retries_var.set(str(s["max_retries"]))
        self.links_var.set(s["fix_links"])
        self.h1fix_var.set(s["fix_multi_h1"])
        self.crumb_var.set(s["do_breadcrumb"])
        self.faq_var.set(s["do_faq"])
        self.canon_var.set(s["do_canonical"])
        self.skip_ht_var.set(s["skip_htaccess"])
        self.canon_rm_var.set(s["canon_remove_redirected"])
        self.clean_var.set(s["clean_url_match"])
        self.htaccess_var.set(s["htaccess_path"])
        self._on_mode_change()
        self._update_sep_example()
        self._refresh_business_card()
        self.folder_var.set(s["last_folder"])
        self._refresh_report_btn()

    def _collect_settings(self):
        folder = self.folder_var.get().strip()
        if folder and self.files:
            self._excluded_by_folder[folder] = sorted(self.ftree.excluded())
        return {
            "last_folder": folder, "recursive": self.recursive_var.get(), "mode": self.mode_var.get(),
            "model": self.model_var.get(), "rewrite_model": self.rewrite_var.get(),
            "site_url": self.site_url_var.get().strip(), "site_name": self.site_name_var.get().strip(),
            "site_context": self.site_context_var.get().strip(), "separator": self.sep_var.get(),
            "default_image": self.default_image_var.get().strip(),
            "business": self.business, "business_verified": self.business_verified,
            "do_h1": self.update_h1_var.get(), "do_schema": self.schema_var.get(), "do_og": self.og_var.get(),
            "do_robots": self.robots_var.get(), "do_lazy": self.lazy_var.get(), "do_alt": self.alt_var.get(),
            "ai_alt": self.ai_alt_var.get(), "backup": self.backup_var.get(),
            "skip_partial": self.skip_partial_var.get(), "skip_noindex": self.skip_noindex_var.get(),
            "skip_redirect": self.skip_redirect_var.get(), "skip_error": self.skip_error_var.get(),
            "exclude_patterns": self.patterns.get("1.0", "end").strip(),
            "excluded": self._excluded_by_folder,
            "do_score": self.score_var.get(), "deep_check": self.deep_var.get(),
            "score_model": self.score_model_var.get(), "score_target": self._target_value(),
            "report_show_cost": self.report_cost_var.get(),
            "protect_score": self.protect_var.get(), "max_retries": self._retries_value(),
            "do_canonical": self.canon_var.get(), "skip_htaccess": self.skip_ht_var.get(),
            "canon_remove_redirected": self.canon_rm_var.get(), "clean_url_match": self.clean_var.get(),
            "htaccess_path": self.htaccess_var.get().strip(), "fix_multi_h1": self.h1fix_var.get(),
            "do_breadcrumb": self.crumb_var.get(), "do_faq": self.faq_var.get(),
            "fix_links": self.links_var.get(),
        }

    def _on_close(self):
        save_settings(self._collect_settings())
        self.destroy()

    # ═════════════════════════════════════════════════════════════════════════
    # Business details / Fetch
    # ═════════════════════════════════════════════════════════════════════════
    def _refresh_business_card(self):
        b = self.business
        if b.get("name") and self.business_verified:
            self.biz_chip.config(text="✓  Verified", fg=SUCCESS)
        elif b:
            self.biz_chip.config(text="⚠  Not verified yet", fg=WARNING)
        else:
            self.biz_chip.config(text="⚠  No details yet — click “Fetch Details” or enter them by hand", fg=WARNING)
        addr = ", ".join(x for x in [b.get("street"), b.get("city"), b.get("region"), b.get("postal")] if x)
        bits = [b.get("name"), b.get("phone"), addr, f"{len(b.get('hours') or [])} hours line(s)" if b.get("hours") else ""]
        self.biz_summary.config(text="\n".join(x for x in bits if x) or "Schema will use a simple WebPage type without contact details.")

    def _edit_business(self, warnings=None, data=None):
        d = dict(data if data is not None else self.business)
        if not d.get("name"):
            d["name"] = self.site_name_var.get().strip()
        BusinessDialog(self, d, warnings or [], self._save_business, on_refetch=self._fetch_details)

    def _save_business(self, d):
        self.business = d
        self.business_verified = True
        # The verified details also fill the Business tab fields.
        if d.get("name"):
            self.site_name_var.set(d["name"])
        full = A.compose_context(d.get("context", ""), d.get("services"), d.get("areas"))
        if full:
            self.site_context_var.set(full)
        self._refresh_business_card()
        save_settings(self._collect_settings())
        self._log("Business details saved and marked verified.", "ok")

    def _normalized_site_url(self):
        u = self.site_url_var.get().strip()
        if u and not u.startswith(("http://", "https://")):
            u = "https://" + u
            self.site_url_var.set(u)
        return u

    def _fetch_details(self):
        url = self._normalized_site_url()
        key = self.api_key_var.get().strip()
        if not url:
            messagebox.showwarning("Site URL needed", "Enter the site URL first.")
            return
        if not key:
            messagebox.showwarning("API key needed", "Add your Anthropic API key on the “AI & Cost” tab first.")
            self._show_tab("⑤ AI & Cost")
            return
        self.fetch_btn.set_enabled(False)
        self.fetch_btn.set_text("Fetching…")
        self._set_status("Fetching business details…", ACCENT)
        self._log(f"Fetching business details from {url} …", "info")
        model = A.MODELS[self.model_var.get()]

        def work():
            try:
                ai = A.AI(anthropic.Anthropic(api_key=key))
                cfg = A.GenConfig(model=model, brand=self.site_name_var.get().strip(), site_url=url)
                res = A.extract_business_details(ai, cfg, url, log=self._log)
                self._ui(lambda: self._fetch_done(res, ai.cost))
            except Exception as e:                               # noqa: BLE001
                self._ui(lambda: self._fetch_failed(e))
        threading.Thread(target=work, daemon=True).start()

    def _fetch_reset(self):
        self.fetch_btn.set_enabled(True)
        self.fetch_btn.set_text("🔍  Fetch Details")
        self._set_status("Ready")

    def _fetch_failed(self, e):
        self._fetch_reset()
        self._log(f"Fetch failed: {e}", "err")
        messagebox.showerror("Could not fetch details",
                             f"{e}\n\nYou can still enter the details by hand with “Review & edit details”.")

    def _fetch_done(self, res, cost):
        self._fetch_reset()
        d0 = res["details"]
        self._log(f"Business details fetched — read {res['pages_read']} pages, found {len(d0.get('services') or [])} services "
                  f"and {len(d0.get('areas') or [])} areas — cost ${cost:.4f}. Please verify them.", "ok")
        d = res["details"]
        if d.get("name"):                                   # fill the Business tab right away
            self.site_name_var.set(d["name"])
        else:
            d["name"] = self.site_name_var.get().strip()
        full = A.compose_context(d.get("context", ""), d.get("services"), d.get("areas"))
        if full:
            self.site_context_var.set(full)
        self.business_verified = False
        self._edit_business(res["warnings"], d)

    # ═════════════════════════════════════════════════════════════════════════
    # Run
    # ═════════════════════════════════════════════════════════════════════════
    def _retries_value(self) -> int:
        try:
            return max(1, min(3, int(self.retries_var.get())))
        except (ValueError, tk.TclError):
            return 3

    def _target_value(self) -> int:
        try:
            return max(50, min(100, int(float(self.score_target_var.get()))))
        except (ValueError, tk.TclError):
            return 90

    def _collect_run_options(self, kind="fresh") -> P.RunOptions:
        facts = dict(self.business)
        facts.setdefault("name", self.site_name_var.get().strip())
        folder = self.folder_var.get().strip()
        rw = self.rewrite_var.get()
        return P.RunOptions(
            mode=self.mode_var.get(), run_kind=kind, folder=folder, site_url=self._normalized_site_url(),
            brand=self.site_name_var.get().strip(), sep=self.sep_var.get(),
            site_context=self.site_context_var.get().strip(), default_image=self.default_image_var.get().strip(),
            facts=facts if self.business_verified else {"name": facts.get("name", "")},
            model=A.MODELS[self.model_var.get()], rewrite_model="" if rw == SAME_AS_MAIN else A.MODELS.get(rw, ""),
            do_h1=self.update_h1_var.get(), do_schema=self.schema_var.get(), do_robots=self.robots_var.get(),
            do_lazy=self.lazy_var.get(), do_alt=self.alt_var.get(), ai_alt=self.ai_alt_var.get(),
            do_og=self.og_var.get(), backup=self.backup_var.get(),
            skip_partial=self.skip_partial_var.get(), skip_noindex=self.skip_noindex_var.get(),
            skip_redirect=self.skip_redirect_var.get(), skip_error=self.skip_error_var.get(),
            exclude_patterns=self._pattern_list(), excluded_paths=self.ftree.excluded(),
            do_score=self.score_var.get(), deep_check=self.score_var.get() and self.deep_var.get(),
            score_model="" if self.score_model_var.get() == SAME_AS_MAIN else A.MODELS.get(self.score_model_var.get(), ""),
            score_target=float(self._target_value()),
            protect_score=self.protect_var.get() and self.score_var.get(), max_retries=self._retries_value(),
            do_canonical=self.canon_var.get(), skip_htaccess=self.skip_ht_var.get(),
            canon_remove_redirected=self.canon_rm_var.get(), clean_url_match=self.clean_var.get(),
            htaccess_path=self.htaccess_var.get().strip(), fix_multi_h1=self.h1fix_var.get(), fix_links=self.links_var.get(),
            do_breadcrumb=self.crumb_var.get() and self.schema_var.get(), do_faq=self.faq_var.get() and self.schema_var.get())

    def _run(self, forced_kind=None):
        if self._running:
            return
        folder = self.folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showerror("No folder", "Please choose the website folder (tab ① Project).")
            self._show_tab("① Project")
            return
        key = self.api_key_var.get().strip()
        if not key:
            messagebox.showerror("API key missing", "Please add your Anthropic API key (tab ⑤ AI & Cost).")
            self._show_tab("⑤ AI & Cost")
            return
        self._on_folder_change()
        files = self._selected_files()
        if not files:
            messagebox.showwarning("Nothing selected", "No HTML files are selected.")
            return
        mode = self.mode_var.get()
        if not self.site_name_var.get().strip():
            if not messagebox.askyesno("No brand name", "No brand name is set, so titles will have no brand ending.\n\nContinue anyway?"):
                self._show_tab("② Business")
                return
        kind = forced_kind or "fresh"
        if mode != "dry_run" and forced_kind is None:
            st = C.RunState(folder)
            cnt = st.counts()
            if cnt:
                summary = (f"✓ {cnt.get('done', 0)} done    ⚠ {cnt.get('review', 0)} need review    "
                           f"✗ {cnt.get('failed', 0)} failed")
                hint = ("Starting over keeps the pages already updated on disk, but forgets the progress. "
                        "A new backup folder is created.")
                dlg = ChoiceDialog(self, summary, hint)
                if dlg.choice is None:
                    return
                kind = dlg.choice
        opts = self._collect_run_options(kind)
        left = len(self._htaccess_hits()[1]) if opts.skip_htaccess else 0
        if opts.do_canonical and not opts.site_url:
            self._log("No website address set — the canonical step will be skipped.", "warn")
        if opts.do_schema and not self.business_verified:
            self._log("Business details are not verified — schema will use WebPage without phone/address.", "warn")

        prices = A.PRICING[opts.model]
        facts_line = "verified" if self.business_verified else "not verified (no phone/address in schema)"
        msg = (f"Pages to process : {len(files)}\nMode             : {mode.replace('_', ' ')} ({kind})\n"
               f"Model            : {opts.model}  (${prices['input']}/${prices['output']} per M tokens)\n"
               f"Title format     : …{C.norm_separator(opts.sep)}{opts.brand or '(no brand)'}\n"
               f"Business details : {facts_line}\n"
               f"Backups          : {'yes, outside the site folder' if opts.backup and mode != 'dry_run' else 'no'}\n"
               f"Canonical tags   : {('step 1 — added / fixed on every page first' + ((', ' + str(left) + ' redirected / gone pages left out') if opts.skip_htaccess else '')) if opts.do_canonical else ('off' + ((' (' + str(left) + ' redirected / gone pages left out)') if opts.skip_htaccess else ''))}\n"
               f"Score protection : {'on — never lowers a page (up to %d re-fetches)' % opts.max_retries if opts.protect_score else 'off'}\n"
               f"SEO scoring      : {'off' if not opts.do_score else ('on + deep AI check (extra cost ≈ $%.2f)' % (self._deep_cost_per_page() * len(files)) if opts.deep_check else 'on (free checks)')}\n\n")
        msg += ("This is a DRY RUN — nothing will be written.\n" if mode == "dry_run"
                else "Files will be modified. Continue?")
        if not messagebox.askyesno(f"Confirm — {APP_NAME}", msg):
            return

        self.results = []
        self.score_items, self.score_summary = [], None
        self._sc = {"n": 0, "b": 0.0, "a": 0.0}
        self.score_strip.pack_forget()
        self.report_btn.set_enabled(False)
        self.tree.delete(*self.tree.get_children())
        for k in self.stat_vars:
            self.stat_vars[k].set("—")
        self.stat_vars["files"].set(str(len(files)))
        self.progress_var.set(0)
        self.progress_lbl.config(text="")
        self._stop_event.clear()
        self._running = True
        self._run_started = time.time()
        self._phase = ""
        self._canon_cancelled = False
        self.run_btn.set_enabled(False)
        self.stop_btn.set_enabled(True)
        self.retry_btn.set_enabled(False)
        self.review_btn.set_enabled(False)
        self._set_status("Running…", ACCENT)
        self._log(f"Starting — {len(files)} files | mode={mode} | model={opts.model} | run={kind}", "info")
        save_settings(self._collect_settings())

        def work():
            ai = A.AI(anthropic.Anthropic(api_key=key))
            self._ai = ai
            try:
                pipe = P.Pipeline(opts, ai, files, self._log,
                                  lambda r: self._ui(lambda: self._on_result(r)),
                                  lambda d, t: self._ui(lambda: self._on_progress(d, t)),
                                  self._stop_event,
                                  on_scores=lambda it, sm: self._ui(lambda: self._on_scores(it, sm)),
                                  on_phase=lambda t: self._ui(lambda: self._on_phase(t)),
                                  on_canonical_done=self._ask_canonical)
                pipe.run()
            except Exception as e:                               # noqa: BLE001
                self._log(f"Fatal error: {e}", "err")
            finally:
                self._ui(self._on_complete)
        threading.Thread(target=work, daemon=True).start()

    def _pending_reviews(self):
        folder = self.folder_var.get().strip()
        state = None
        if folder and Path(folder).is_dir() and self.mode_var.get() != "dry_run":
            state = C.RunState(folder)
        return P.pending_reviews(self.results, state)

    def _refresh_review_btn(self):
        try:
            n = len(self._pending_reviews())
        except Exception:                                        # noqa: BLE001
            n = 0
        self.review_btn.config(text=f"  Review pages ({n})…" if n else "  Review pages…")
        self.review_btn.set_enabled(bool(n) and not self._running)

    def _open_review(self, select_rel=None):
        if self._running:
            return
        key = self.api_key_var.get().strip()
        if not key:
            messagebox.showerror("API key missing", "Please add your Anthropic API key (tab ⑤ AI & Cost) — "
                                                     "the Regenerate buttons need it.")
            self._show_tab("⑤ AI & Cost")
            return
        items = self._pending_reviews()
        if not items:
            messagebox.showinfo("Review pages", "No pages are waiting for a review.")
            return
        opts = self._collect_run_options("fresh")
        ai = self._ai if getattr(self, "_ai", None) is not None else A.AI(anthropic.Anthropic(api_key=key))
        pipe = P.Pipeline(opts, ai, [], self._log, lambda r: None, lambda d, t: None, self._stop_event)
        pipe.review_session()
        ReviewWindow(self, items, pipe, select_rel)

    def _review_closed(self, approved: list, pipe):
        """Approved pages replace their 'needs review' rows; scores, report and CSV are refreshed."""
        self._refresh_review_btn()
        if not approved:
            return
        by_rel = {r["rel"]: r for r in approved}
        self.results = [by_rel.get(r["rel"], r) for r in self.results]
        have = {r["rel"] for r in self.results}
        self.results += [r for r in approved if r["rel"] not in have]
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.results):
            if self._passes_filter(r):
                self._insert_row(i, r, see=False)
        self._update_stats()
        if self.mode_var.get() != "dry_run":
            items, summary = pipe.refresh_scores()
            if summary:
                self._on_scores(items, summary)
            self._auto_export()
        self._log(f"Review finished — {len(approved)} page(s) approved and written.", "ok")
        self._refresh_review_btn()

    def _retry_failed(self):
        if not self._running:
            self._run(forced_kind="retry")

    def _stop(self):
        self._stop_event.set()
        self._log("Stop requested — finishing the current page…", "warn")

    def _ask_canonical(self, stats) -> bool:
        """Runs in the worker thread: shows the window on the main thread and waits for the answer."""
        done, answer = threading.Event(), {"go": True}

        def show():
            try:
                answer["go"] = CanonicalDoneDialog(self, stats).go
            finally:
                done.set()
        self._ui(show)
        done.wait()
        if not answer["go"]:
            self._canon_cancelled = True
        return answer["go"]

    def _on_phase(self, text):
        self._phase = text
        if text.startswith("Step 2"):
            self._run_started = time.time()                      # the time estimate starts with the metadata step
            self.progress_var.set(0)
        self.progress_lbl.config(text=text)
        self._log(text, "info")

    def _on_progress(self, done, total):
        pct = done / total * 100 if total else 0
        self.progress_var.set(pct)
        eta = ""
        phase = getattr(self, "_phase", "")
        elapsed = time.time() - self._run_started
        if done and done < total and elapsed > 5 and not phase.startswith("Step 1"):
            remain = elapsed / done * (total - done)
            eta = f"   ·   about {int(remain // 60)}m {int(remain % 60):02d}s left"
        self.progress_lbl.config(text=(phase + "   ·   " if phase else "") + f"{done} / {total}  ({pct:.0f}%){eta}")

    def _on_result(self, r):
        self.results.append(r)
        if self._passes_filter(r):
            self._insert_row(len(self.results) - 1, r)
        self._update_stats()
        sb, sa = r.get("score_before"), r.get("score_after")
        if sb and sa:
            self._sc["n"] += 1
            self._sc["b"] += sb["t"]
            self._sc["a"] += sa["t"]
            self._show_score_strip(provisional=True)

    # ── SEO score display ────────────────────────────────────────────────────
    def _show_score_strip(self, provisional=False):
        sm = self.score_summary
        if sm and not provisional and sm.get("pages"):
            rel = f", {sm['relative_pct']:+.0f}%" if sm.get("relative_pct") is not None else ""
            txt = (f"SEO score   {sm['avg_before']:.2f}  →  {sm['avg_after']:.2f}   ({sm['delta'] + 0.0:+.2f} points{rel})   ·   "
                   f"{sm['target_after']} of {sm['pages']} pages at {sm['target']:.0f}+   ·   "
                   f"{len(sm['critical_pages'])} with critical issues   ·   {len(sm['decreased'])} went down")
        elif self._sc["n"]:
            n = self._sc["n"]
            b, a = round(self._sc["b"] / n, 2), round(self._sc["a"] / n, 2)
            txt = (f"SEO score so far   {b:.2f}  →  {a:.2f}   ({round(a - b, 2) + 0.0:+.2f} points over {n} pages)   ·   "
                   f"final numbers are worked out when the run ends")
        else:
            return
        self.score_strip.config(text=txt)
        if not self.score_strip.winfo_ismapped():
            self.score_strip.pack(fill="x", pady=(0, 8), before=self.progress_holder)

    def _on_scores(self, items, summary):
        self.score_items, self.score_summary = items, summary
        self._show_score_strip()
        self._set_filter(self.filter)                      # redraw rows with the final numbers
        self._log(f"SEO score: {summary['avg_before']:.2f} → {summary['avg_after']:.2f} "
                  f"({summary['delta']:+.2f} points) over {summary['pages']} pages.", "ok")

    # ── automatic report + CSV, and "View Report" ────────────────────────────
    def _site_slug(self) -> str:
        site = (self.site_url_var.get().strip() or self.site_name_var.get().strip()
                or Path(self.folder_var.get().strip() or "site").name or "site")
        site = site.replace("https://", "").replace("http://", "").strip("/")
        return ("".join(ch if ch.isalnum() or ch in "-." else "_" for ch in site)[:50]) or "site"

    def _reports_folder(self) -> Path:
        """The report and the CSV are saved in the very folder that was scanned."""
        folder = self.folder_var.get().strip()
        return Path(folder) if folder and Path(folder).is_dir() else REPORTS_DIR / self._site_slug()

    def _writable_reports_folder(self) -> Path:
        """Scanned folder if we can write there, otherwise the fallback in the user's profile."""
        folder = self._reports_folder()
        try:
            probe = folder / ".seo_write_test"
            probe.write_text("x")
            probe.unlink()
            return folder
        except OSError:
            fb = REPORTS_DIR / self._site_slug()
            self._log(f"Cannot write to {folder} — saving the report to {fb} instead.", "warn")
            return fb

    def _latest_report(self):
        """Newest report for this website: the one just made, or the latest file on disk from earlier runs."""
        if self.last_report_path and Path(self.last_report_path).is_file():
            return Path(self.last_report_path)
        files = []
        for folder in (self._reports_folder(), REPORTS_DIR / self._site_slug()):   # scanned folder, then the fallback
            try:
                files += list(folder.glob("seo_report_*.html"))
            except OSError:
                pass
        files = sorted((f for f in files if f.is_file()), key=lambda f: f.stat().st_mtime)
        return files[-1] if files else None

    def _refresh_report_btn(self):
        """View Report is available only after a scan has COMPLETED in this session (and its report exists)."""
        self.report_btn.set_enabled(self._scan_done and not self._running and self._latest_report() is not None)

    def _auto_export(self):
        """Runs at the end of every run (also after Stop): saves the CSV and, if scores exist, the HTML report."""
        if not self.results:
            return
        folder = self._writable_reports_folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._log(f"Could not create the reports folder {folder}: {e}", "warn")
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = folder / f"seo_results_{stamp}.csv"
        csv_ok = False
        try:
            self._write_csv(csv_path)
            csv_ok = True
            self._log(f"Results CSV saved → {csv_path}", "ok")
        except Exception as e:                                 # noqa: BLE001
            self._log(f"Could not save the CSV: {e}", "warn")
        if self.score_summary and self.score_items:
            html_path = folder / f"seo_report_{stamp}.html"
            dry = all(r.get("mode") == "dry_run" for r in self.results if r["status"] != "skipped")
            meta = {"site": self.site_url_var.get().strip(), "brand": self.site_name_var.get().strip(), "dry": dry,
                    "engine": f"SEO Meta Tag Updater v{APP_VERSION}",
                    "show_cost": self.report_cost_var.get(), "csv_file": csv_path.name if csv_ok else "",
                    "canonical_step": any(r.get("canonical_action") for r in self.results),
                    "cost": self._session_cost()}
            try:
                R.write_report(html_path, self.score_summary, self.score_items, self._score_not_scored(), meta)
                self.last_report_path = str(html_path)
                self._log(f"SEO report saved → {html_path}   (click “View Report” to open it)", "ok")
            except Exception as e:                             # noqa: BLE001
                self._log(f"Could not create the report: {e}", "warn")
        else:
            self._log("No SEO report this time — scoring is switched off or no page could be scored.", "muted")

    def _view_report(self):
        path = self._latest_report()
        if path is None:
            messagebox.showinfo("No report yet", "A report is created automatically at the end of each run when "
                                                 "“Calculate SEO scores” is on (Options tab). Run the tool first — "
                                                 "a dry run is enough.")
            return
        import webbrowser
        try:
            webbrowser.open(Path(path).resolve().as_uri())
        except Exception as e:                                 # noqa: BLE001
            messagebox.showerror("Could not open the report", f"{e}\n\nThe file is here:\n{path}")

    def _session_cost(self) -> float:
        """Everything this run spent on the API (writing, rewrites, alt text and the deep check)."""
        ai = getattr(self, "_ai", None)
        return ai.cost if ai is not None else sum(r.get("cost", 0) for r in self.results)

    def _score_not_scored(self):
        out = []
        for r in self.results:
            if r["status"] in ("skipped", "failed"):
                out.append((r["rel"], r["status"], r.get("reason") or r.get("error") or ""))
        return out

    def _deep_cost_per_page(self):
        model = A.MODELS.get(self.model_var.get(), "claude-haiku-4-5")
        sm = self.score_model_var.get()
        if sm != SAME_AS_MAIN:
            model = A.MODELS.get(sm, model)
        return A.cost_of(model, 1900, 280)                 # before + after items per page, batched

    def _update_stats(self):
        c = {"updated": 0, "preview": 0, "review": 0, "failed": 0, "skipped": 0}
        cost = 0.0
        for r in self.results:
            c[r["status"]] = c.get(r["status"], 0) + 1
            cost += r.get("cost", 0)
        self.stat_vars["updated"].set(str(c["updated"] + c["preview"]))
        self.stat_vars["review"].set(str(c["review"]))
        self.stat_vars["failed"].set(str(c["failed"]))
        self.stat_vars["skipped"].set(str(c["skipped"]))
        self.stat_vars["protected"].set(str(sum(1 for r in self.results if r.get("protected"))))
        self.stat_vars["canon"].set(str(sum(1 for r in self.results if r.get("canonical_action") in ("added", "fixed"))))
        self.stat_vars["cost"].set(f"${cost:.3f}")

    def _on_complete(self):
        self._running = False
        self.run_btn.set_enabled(True)
        self.stop_btn.set_enabled(False)
        self.retry_btn.set_enabled(True)
        n_rev = sum(1 for r in self.results if r["status"] == "review")
        n_fail = sum(1 for r in self.results if r["status"] == "failed")
        n_dup = sum(1 for r in self.results if r.get("similar_to"))
        cost = sum(r.get("cost", 0) for r in self.results)
        self._auto_export()
        self._scan_done = True
        self._refresh_report_btn()
        if getattr(self, "_canon_cancelled", False):
            self._set_status("Canonical tags done — SEO update cancelled", SUCCESS)
            self._log("Canonical tags only: the SEO update was not run.", "ok")
            self._refresh_resume_card()
            return
        if self._stop_event.is_set():
            self._set_status("Stopped — progress saved", WARNING)
        elif n_fail or n_rev:
            self._set_status(f"Done — {n_rev} to review, {n_fail} failed", WARNING)
        else:
            self._set_status("Complete ✓", SUCCESS)
        self._log(f"Finished. {len(self.results)} pages · cost ${cost:.4f}"
                  + (f" · {n_rev} need review" if n_rev else "") + (f" · {n_fail} failed" if n_fail else "")
                  + (f" · {n_dup} pages have near-duplicate content" if n_dup else ""), "ok")
        self._refresh_resume_card()
        self._refresh_review_btn()

    # ═════════════════════════════════════════════════════════════════════════
    # Results table
    # ═════════════════════════════════════════════════════════════════════════
    def _passes_filter(self, r):
        f = self.filter
        if f == "all":
            return True
        if f == "updated":
            return r["status"] in ("updated", "preview")
        if f == "dups":
            return bool(r.get("similar_to"))
        if f == "protected":                                    # score protection stepped in (re-fetched, partly reverted or kept)
            return bool(r.get("protected"))
        return r["status"] == f

    def _set_filter(self, key, rerender=True):
        self.filter = key
        for k, chip in self.chips.items():
            chip.config(bg=ACCENT if k == key else CARD_BG, fg="white" if k == key else TEXT_SECONDARY)
        if rerender:
            self.tree.delete(*self.tree.get_children())
            for i, r in enumerate(self.results):
                if self._passes_filter(r):
                    self._insert_row(i, r, see=False)

    def _insert_row(self, idx, r, see=True):
        st = r["status"]
        notes = r.get("reason") or "; ".join(r.get("notes") or []) or r.get("error", "")
        if st in ("updated", "preview", "kept") and r.get("notes"):
            notes = "; ".join(r["notes"])
        if r.get("retry_note"):                                  # shown here only — it is not part of the CSV
            notes = (r["retry_note"] + ("; " + notes if notes else ""))
        sb, sa = r.get("score_before"), r.get("score_after")
        sb_t = f"{sb['t']:.2f}" if sb else "–"
        sa_t = f"{sa['t']:.2f}" if sa else "–"
        dl = f"{round(sa['t'] - sb['t'], 2) + 0.0:+.2f}" if sa and sb else "–"     # "+ 0.0" avoids "-0.0"
        self.tree.insert("", "end", iid=str(idx), tags=(STATUS_TAG.get(st, ""),), values=(
            r["rel"], STATUS_TXT.get(st, st), sb_t, sa_t, dl,
            f"{r['title_len']}" if r.get("title_len") else "—", f"{r['desc_len']}" if r.get("desc_len") else "—",
            "✓" if r.get("h1_changed") else "–", CANON_TXT.get(r.get("canonical_action"), "–"),
            r.get("schema_action") or "–",
            (r.get("lazy_added", 0) + r.get("alts_added", 0)) or "–", notes[:120], f"{r.get('cost', 0):.4f}"))
        if see:
            self.tree.see(str(idx))

    def _on_row_double_click(self, e):
        sel = self.tree.selection()
        if sel:
            r = self.results[int(sel[0])]
            if r.get("status") == "review" and r.get("review") and not self._running:
                self._open_review(select_rel=r["rel"])
            else:
                self._show_detail(r)

    def _show_detail(self, r):
        win = tk.Toplevel(self)
        win.title(f"Detail — {r['rel']}")
        win.configure(bg=PANEL_BG)
        center(win, self, 820, 720)
        sf = ScrollFrame(win, bg=PANEL_BG)
        sf.pack(fill="both", expand=True)
        self.register_scroll(sf)
        f = sf.inner

        def section(title):
            tk.Label(f, text=title, font=FONT_CARD, fg=ACCENT, bg=PANEL_BG).pack(anchor="w", padx=20, pady=(14, 2))

        def line(label, value, color=TEXT_PRIMARY, sub="", width=14):
            row = tk.Frame(f, bg=PANEL_BG)
            row.pack(fill="x", padx=20, pady=2)
            tk.Label(row, text=label, width=width, anchor="nw", font=FONT_SMALL, fg=TEXT_MUTED, bg=PANEL_BG).pack(side="left")
            tk.Label(row, text=(str(value) or "—") + sub, anchor="w", justify="left", wraplength=620,
                     font=FONT_MAIN, fg=color, bg=PANEL_BG).pack(side="left", fill="x", expand=True)

        tk.Label(f, text=r["rel"], font=("Segoe UI Semibold", 12), fg=TEXT_PRIMARY, bg=PANEL_BG).pack(anchor="w", padx=20, pady=(16, 0))
        stcol = {"kept": INFO, "updated": SUCCESS, "preview": ACCENT, "review": WARNING, "failed": DANGER, "skipped": TEXT_MUTED}
        tk.Label(f, text=STATUS_TXT.get(r["status"], r["status"]) + (f" — {r['reason']}" if r.get("reason") else ""),
                 font=FONT_MAIN, fg=stcol.get(r["status"], TEXT_PRIMARY), bg=PANEL_BG, wraplength=740, justify="left"
                 ).pack(anchor="w", padx=20, pady=(2, 0))
        if r.get("error"):
            line("Error", r["error"], DANGER)
        sb, sa = r.get("score_before"), r.get("score_after")
        if sb:
            section("SEO score (internal, out of 100)")
            if sa:
                d = round(sa["t"] - sb["t"], 2) + 0.0
                line("Total", f"{sb['t']:.2f} → {sa['t']:.2f}   ({d:+.2f} points)   ·   {sa['l']}",
                     SUCCESS if d > 0.05 else (DANGER if d < -0.05 else TEXT_PRIMARY))
            else:
                line("Total", f"{sb['t']:.2f}   ({sb['l']})   —   before only; the page was not changed", TEXT_PRIMARY)
            for cat, nm, mx in S.CATS:
                a_txt = f" → {sa['cats'][cat]:.2f}" if sa else ""
                line(nm, f"{sb['cats'][cat]:.2f}{a_txt}  / {mx}", width=30)
            crit = (sa or sb).get("crit", [])
            if crit:
                line("Critical", "; ".join(S.CRIT_TEXT.get(c, c) for c in crit), DANGER)
            left = [x for x in S.explain(sa or sb) if x["ok"] is False]
            if left:
                section("Still to improve")
                for x in left[:12]:
                    tail = f"  —  {x['reason']}" if x["reason"] else ""
                    man = "   [fix on the page]" if x["manual"] else ""
                    line("•", f"{x['label']}  ({x['earned']:g}/{x['max']}){tail}{man}", WARNING if x["manual"] else TEXT_PRIMARY)
        section("Title")
        line("Old", r["old_title"], TEXT_MUTED)
        line("New", r["new_title"], SUCCESS, f"   ({r['title_len']} characters)" if r.get("title_len") else "")
        section("Description")
        line("Old", r["old_description"], TEXT_MUTED)
        line("New", r["new_description"], SUCCESS, f"   ({r['desc_len']} characters)" if r.get("desc_len") else "")
        section("H1")
        line("Old", r["old_h1"], TEXT_MUTED)
        line("New", r["new_h1"], SUCCESS if r.get("h1_changed") else TEXT_SECONDARY)
        if r.get("retry_note"):
            section("Score protection")
            line("Result", r["retry_note"], SUCCESS if r["status"] != "kept" else WARNING,
                 sub=f"   (fetch attempts: {r.get('attempts', 1)})")
        section("Other changes")
        line("Schema", f"{r.get('schema_type') or '—'}  ({r.get('schema_action') or 'not changed'})")
        line("Social tags", "updated" if r.get("og_changed") else "unchanged")
        line("Share image", r.get("image_url") or "none found", sub=f"   [{r['image_source']}]" if r.get("image_source") else "")
        line("Images", f"{r.get('lazy_added', 0)} lazy-loaded · {r.get('alts_added', 0)} alt texts added")
        line("Robots / keywords", f"robots added: {'yes' if r.get('robots_added') else 'no'} · keyword tags removed: {r.get('keywords_removed', 0)}")
        line("Canonical", r.get("canonical") or "none", WARNING if not r.get("canonical") else TEXT_PRIMARY)
        if r.get("canonical_action"):
            line("Canonical step", {"added": "tag added", "fixed": "tag fixed", "ok": "already right",
                                    "other domain": "points to another domain — kept",
                                    "removed": "removed (redirected / gone page)"}.get(r["canonical_action"],
                                                                                          r["canonical_action"]))
        line("API cost", f"${r.get('cost', 0):.5f}")
        if r.get("notes"):
            section("Notes")
            for n in r["notes"]:
                line("•", n, WARNING)
        if r.get("schema_json"):
            section("JSON-LD written")
            box = text_box(f, 12, bg=CONSOLE_BG)
            box.pack(fill="x", padx=20, pady=(2, 16))
            box.insert("1.0", json.dumps(r["schema_json"], indent=2, ensure_ascii=False))
            box.config(state="disabled", fg=ACCENT2, font=FONT_MONO)
        bar = tk.Frame(f, bg=PANEL_BG)
        bar.pack(fill="x", padx=20, pady=(4, 18))
        Btn(bar, "📂  Open file", lambda: os.startfile(r["file"]) if sys.platform == "win32" else None, "secondary").pack(side="left")
        Btn(bar, "📋  Copy new title", lambda: (self.clipboard_clear(), self.clipboard_append(r["new_title"])), "ghost").pack(side="left", padx=8)

    # ═════════════════════════════════════════════════════════════════════════
    # Export / restore
    # ═════════════════════════════════════════════════════════════════════════
    def _write_csv(self, path):
        fields = ["rel", "status", "reason", "page_name", "old_title", "new_title", "title_len", "old_description",
                  "new_description", "desc_len", "old_h1", "new_h1", "h1_changed", "schema_type", "schema_action",
                  "image_url", "image_source", "canonical", "canonical_action", "h1_demoted", "breadcrumb_added", "faq_items", "lazy_added", "alts_added", "robots_added",
                  "keywords_removed", "og_changed", "similar_to", "notes",
                  "score_before", "score_after", "score_change", "rating_before", "rating_after",
                  "critical_issues", "fix_on_the_page"] + \
                 [f"{k}_{c}" for c, _, _ in S.CATS for k in ("before", "after")]       # each category: before, then its after
        if self.report_cost_var.get():                          # cost / error / file only when "show cost" is switched on
            fields += ["cost", "error", "file"]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in self.results:
                row = dict(r)
                row["notes"] = "; ".join(r.get("notes") or [])
                sb, sa = r.get("score_before"), r.get("score_after")
                if sb:
                    row.update(score_before=round(sb["t"], 2), rating_before=sb["l"])
                    row.update({f"before_{c}": sb["cats"][c] for c, _, _ in S.CATS})
                if sa:
                    row.update(score_after=round(sa["t"], 2), rating_after=sa["l"],
                               score_change=round(sa["t"] - (sb["t"] if sb else 0), 2),
                               critical_issues="; ".join(S.CRIT_TEXT.get(c, c) for c in sa.get("crit", [])),
                               fix_on_the_page="; ".join(S.CHECK_INFO[c][2] for c in sa.get("man", []) if c in S.CHECK_INFO))
                    row.update({f"after_{c}": sa["cats"][c] for c, _, _ in S.CATS})
                w.writerow(row)

    def _restore_backup(self):
        folder = self.folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("No folder", "Choose the website folder first (tab ① Project).")
            return
        st = C.RunState(folder)
        start = st.backup_dir if st.backup_dir and Path(st.backup_dir).is_dir() else str(Path(folder).parent)
        bdir = filedialog.askdirectory(title="Select the backup folder to restore from", initialdir=start)
        if not bdir:
            return
        n = sum(1 for f in Path(bdir).rglob("*") if f.is_file())
        if not messagebox.askyesno("Restore backup",
                                   f"Copy {n} original file(s) from\n{bdir}\n\nback over the files in\n{folder}\n\n"
                                   "This replaces the current versions of those files. Continue?"):
            return
        done = C.restore_backup(bdir, folder)
        self._log(f"Restored {done} file(s) from {bdir}", "ok")
        messagebox.showinfo("Restored", f"{done} file(s) restored.\n\nTip: choose “Start over” on the next run, "
                                         "because saved progress no longer matches the files.")


def main():
    app = SEOUpdaterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
