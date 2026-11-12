# SEO Meta Tag Updater

A desktop app (Python / Tkinter) that cleans up the SEO basics of a **static HTML website** in bulk: canonical tags, unique meta titles and descriptions, H1s, structured data and social tags. It uses the Claude API to write the text, checks every change with a built-in 100-point score, and produces a client-ready before/after report.

Built by **Top Ten Website Marketing**. Current version: **5.2.0**.

## What it does

A run has two steps.

**Step 1 — canonical tags and links (no AI, no cost)**
- Adds the canonical tag to every page, or fixes it when it points to a different page of the same site, the wrong `www` / `http` form, or is empty or relative. A canonical that points to another domain is kept and flagged.
- Reads the site's `.htaccess`: pages that are **301 / 302 redirected or 410 gone** get no canonical (any existing one is removed) and are left out of step 2. Site-wide rules (www / https, catch-alls) are never used.
- Rewrites internal links that point to redirected pages so they go straight to the final page. Links to gone pages are only listed in `seo_links_<time>.csv`.
- When step 1 finishes, a window shows the numbers and asks **Continue SEO update** or **Cancel** (Cancel = canonical tags only, no AI spend).

**Step 2 — meta data (Claude)**
- Writes a unique meta title, description and H1 for every page. Titles and descriptions are checked for length, complete sentences and similarity to every other page, so 3,000+ page sites stay unique.
- Merges JSON-LD schema into what is already there, writes Open Graph / Twitter tags, and fixes lazy-loading and image alt text.
- Optional: breadcrumb markup, FAQ markup (only from questions already on the page), and one H1 per page.
- **Never lowers a score.** Each page is scored before and after (total and every category). If the new version would score lower, only the fields that hurt are rewritten (up to 3 times); if nothing is good enough the page is left unchanged.
- **Review before writing:** pages that fail the checks can be edited by hand, regenerated field by field (with an optional instruction), and approved one at a time.

**Reports**
- An HTML report and a CSV are saved in the scanned folder after every run: before/after scores with two decimals, category scores, remaining actions per page, character-count charts and more. Cost is hidden unless you switch it on.
- A sitemap check lists problem entries and live pages that are missing, and can write a clean `sitemap.xml`.

## Safety

- **Dry run** shows exactly what would change and writes nothing.
- **Backups:** originals are copied to `~/TTM_SEO_Backups` (never inside the website folder). "Restore backup…" puts them back.
- Progress is saved, so a stopped run can be resumed.
- Files are read and written without changing their encoding or line endings.
- Your Anthropic API key is stored in the operating system's credential store (via `keyring`), never in the project.

## The score

A 100-point internal quality score (not a Google score) across seven categories: meta title (15), meta description (15), H1 and on-page relevance (20), canonical and robots (10), Open Graph and social tags (10), structured data (15), content relevance and consistency (15). Most checks are code-verified; an optional Deep AI check adds judged points.

## Requirements

- Python 3.10 or newer (Tkinter included; on Windows the python.org installer has it)
- `pip install anthropic keyring`
- An [Anthropic API key](https://console.anthropic.com/)
- To open the report charts you need an internet connection (Chart.js and Google Fonts load from a CDN)

## Run it

```bash
pip install anthropic keyring
python seo_meta_updater.py
```

1. **Project** tab: choose the website folder and enter the site address and brand name.
2. **Business** tab: fetch or enter verified business details (used for schema).
3. **Options** tab: choose what to update. Start with a **dry run**.
4. **AI & Cost** tab: add your API key and pick a model (Haiku / Sonnet / Opus 4.5) — an estimate is shown before you run.

## Build a Windows .exe

```bash
pip install pyinstaller
python -m PyInstaller --noconfirm --clean SEO-Meta-Updater.spec
```

The executable is written to `dist/`. The spec bundles `report_template.html`.

## Tests

```bash
python -m unittest discover -s tests      # unit and pipeline tests (no network, no API cost)
python tools/e2e_ui.py                    # drives the real Tk window against a fake AI
```

A few report tests render the page in headless Chrome or Edge and are skipped when neither is installed.

## Project layout

| File | Purpose |
|---|---|
| `seo_meta_updater.py` | The Tkinter app (screens, dialogs, settings) |
| `seo_pipeline.py` | The run engine: step 1, step 2, score protection, review |
| `seo_core.py` | Safe HTML reading / writing, tag scanner, schema merge, images, backups, run state |
| `seo_ai.py` | Prompts, the Claude wrapper, validation, regeneration |
| `seo_score.py` | The 100-point scoring model |
| `seo_canonical.py` | Canonical decisions and `.htaccess` redirect / gone rules |
| `seo_links.py` | Internal links to redirected pages |
| `seo_sitemap.py` | Sitemap check and rebuild |
| `seo_structure.py` | H1 fix, breadcrumb and FAQ markup |
| `seo_report.py`, `report_template.html` | HTML report builder and template |
| `tools/` | `rebuild_report.py`, `refresh_report.py`, `report_from_csv.py`, `e2e_ui.py`, icon builder |
| `tests/` | Unit, pipeline and browser tests |

## Limits

- Static `.html` / `.htm` sites only (no PHP or WordPress templates).
- Only the `.htaccess` in the website folder is read, not ones in sub-folders. A redirect written with `RewriteCond` is treated as site-wide and ignored.
- Scores are an internal benchmark, not a ranking guarantee.

## License

No license has been chosen yet. Until one is added, all rights are reserved by Top Ten Website Marketing.
