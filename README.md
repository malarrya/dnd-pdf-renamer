# dnd_renamer

A content-based identification and renaming tool for classic TSR/D&D PDF manuals, matched against a [LaunchBox](https://www.launchbox-app.com/) platform XML catalog.

If you've got a folder of PDFs with inconsistent, cryptic, or just plain wrong filenames — scanned modules, rulebooks, accessories — this script reads each file's actual content (text, OCR, cover art) and renames it to match its real catalog entry.

## Design philosophy

**A wrong rename is worse than no rename.** Every identification layer requires a real, high-confidence signal before it will touch a file. When nothing is confident enough, the file is left alone and reported as unmatched rather than guessed at. You can optionally review low-confidence suggestions for unmatched files one at a time at the end of a run and confirm or reject each by hand.

## How it works

Each file goes through a cascade of identification layers, roughly cheapest/most-reliable first:

1. **Fingerprint cache** — if this exact file's content (SHA256) has been seen before, reuse the known answer instantly.
2. **Content-based matching** — extracts native PDF text and scores it against every catalog entry's description, page count, and file size. High score/margin thresholds required to accept a match.
3. **Process of elimination** — for files nothing above resolved, checks them only against catalog entries nothing else has already claimed — a much smaller, less noisy pool.
4. **Cover-image matching** — perceptual-hash comparison against the catalog's box-art images, tried before OCR since it's far cheaper and can succeed on scans OCR can't read at all.
5. **OCR fallback** — for scanned PDFs with no text layer, OCRs the front pages and back cover and compares against the catalog the same way as step 2.
6. **Legacy filename matching** — last resort only, using whatever the file happens to already be named.

Steps 1–2 run for every file first; only files nothing above resolves move on to steps 3–6, so most of a run's time is spent on a minority of hard cases.

Every layer is filename-independent except the last, specifically so a badly-misnamed file can still be identified from its actual content, and so a wrong rename never gets "confirmed" as correct on a later run just because it inherited a bad name.

## Installing on Windows without Python

If you don't already have Python and just want to run the tool, use the
Windows installer instead of the steps below: download the latest
`DnD_Renamer_Setup.exe` from the
[Releases page](https://github.com/malarrya/dnd-pdf-renamer/releases/latest)
(see `installer/BUILD.md` for how it's built), run it, and optionally leave
the "Install Tesseract OCR" box checked so scanned-PDF support works out of
the box. It installs a self-contained `dnd_renamer.exe` with every required
Python package already bundled in, plus Start Menu/Desktop shortcuts - no
`pip install` needed. Everything below still applies to how the tool behaves
once it's running; skip straight to [Setup](#setup).

Unsigned build: Windows SmartScreen may warn on first run ("Windows
protected your PC") - click **More info** -> **Run anyway**.

[**v1.1.0**](https://github.com/malarrya/dnd-pdf-renamer/releases/tag/v1.1.0)
adds a full GUI in place of the old console prompts - a setup window for
the four folder/file paths, and a run window with live progress, a
scrolling log, and working **Pause**/**Cancel** buttons. See
[Setup](#setup) and [Usage](#usage) below for what that looks like.

If you installed v1.1.0, update to
[**v1.1.1**](https://github.com/malarrya/dnd-pdf-renamer/releases/tag/v1.1.1)
or later - that version still popped up a separate console window
alongside the GUI, mirroring the exact same log text the GUI's own log
pane already showed.

If you installed v1.1.1, update to
[**v1.1.2**](https://github.com/malarrya/dnd-pdf-renamer/releases/tag/v1.1.2)
or later - clicking **Close** in that version could leave a scan worker
process running in the background if an earlier exception skipped its
worker pool's clean shutdown; Close now always terminates any that are
still running.

[**v1.1.3**](https://github.com/malarrya/dnd-pdf-renamer/releases/tag/v1.1.3)
is a small efficiency fix: confirming a match through the "couldn't be
confidently matched" review now also updates the incremental-scan
index, not just the fingerprint cache, so a future in-place scan can
skip re-reading that file too.

[**v1.1.4**](https://github.com/malarrya/dnd-pdf-renamer/releases/tag/v1.1.4)
makes the visual "Confirm Suggestion" review dialog clearer about what
it's showing: the text now labels the current filename vs. the
suggested identity explicitly, and the two images are captioned
"Suggested match (catalog box art)" and "This file's own front page"
with a direct prompt asking whether they show the same book.

[**v1.2.0**](https://github.com/malarrya/dnd-pdf-renamer/releases/tag/v1.2.0)
replaces the "couldn't be confidently matched" review's plain yes/no
with a searchable picker - see [Usage](#usage) below for what that
looks like. A file with no automated guess at all now gets a real
chance at manual review too, instead of being silently skipped.

If you installed v1.2.0, update to
[**v1.2.1**](https://github.com/malarrya/dnd-pdf-renamer/releases/tag/v1.2.1)
or later - that version's picker dialog opened at a cramped default
size that truncated longer catalog titles; it now opens as big as the
main run window (760x445) and is resizable if that's still not enough.

[**v1.3.0**](https://github.com/malarrya/dnd-pdf-renamer/releases/tag/v1.3.0)
adds two more ways to handle a file the picker's candidate list doesn't
cover: type the exact title yourself if it isn't in the catalog at all,
or check a box to also show titles already claimed by another file this
run (for the rare case a claim was made in error, like a genuine
duplicate PDF). The dialog is taller (760x600) to fit both without
clipping the buttons at the bottom.

[**v1.4.0**](https://github.com/malarrya/dnd-pdf-renamer/releases/tag/v1.4.0)
lets a **full** scan (re-verify every file's content from scratch) also
offer to clear the fingerprint cache and rebuild it fresh from that run,
instead of silently trusting instant answers cached from a previous run
even when you asked for a from-scratch re-check. The existing cache is
backed up first (`dnd_renamer_cache.json.bak`), so this isn't a one-way
door if the run gets cancelled partway through.

[**v1.5.0**](https://github.com/malarrya/dnd-pdf-renamer/releases/tag/v1.5.0)
adds a 100%-manual identification mode: a checkbox on the setup/confirm
screen skips the automated scan entirely and hands every file straight
to the searchable-title picker, one at a time. That picker dialog now
also stays open and reuses itself across files instead of closing and
reopening for each one, opens immediately rather than waiting on that
file's preview image first, and has two new buttons - **Back to
Automated Scan** (hands whatever's left unreviewed to the automated
pipeline instead) and **Close Program** (exits immediately). The run
window itself hides for the duration of a manual-only review, since it
has nothing useful to show, and reappears if you switch back to
automated or the review finishes.

[**v1.5.1**](https://github.com/malarrya/dnd-pdf-renamer/releases/tag/v1.5.1)
is docs-only - no functional changes from v1.5.0.

[**v1.6.0**](https://github.com/malarrya/dnd-pdf-renamer/releases/tag/v1.6.0)
adds visibility into how many files didn't need renaming at all: the
automated scan's finished summary now notes how many matched files
were already correctly named, and the 100%-manual picker shows a
one-time note when it opens if some files' current filenames already
exactly match a catalog title - a filename-only heads-up, not a claim
those files are verified correct, since manual mode does no content
analysis of its own.

If you installed v1.6.0, update to
[**v1.7.0**](https://github.com/malarrya/dnd-pdf-renamer/releases/tag/v1.7.0)
or later - that version's picker could open with its buttons clipped
at the bottom whenever the already-correctly-named note was shown,
requiring a manual resize to see them. It also adds a checkbox next to
that note to skip those already-correctly-named files outright, with
no round-trip through the picker at all - just left untouched, same as
clicking Skip.

If you installed v1.7.0, update to
[**v1.7.1**](https://github.com/malarrya/dnd-pdf-renamer/releases/tag/v1.7.1)
or later - that version's picker could get stuck showing "Loading next
file..." forever if the skip-already-correct filter happened to skip
every remaining file after the last one you actually reviewed, since
nothing was left to close the dialog in that case.

[**v1.8.0**](https://github.com/malarrya/dnd-pdf-renamer/releases/tag/v1.8.0)
fixes brief command-prompt windows flashing on screen during OCR
fallback, makes the picker's front-page thumbnail clickable to view it
at full resolution instead of only the small thumbnail, and adds a
"Keep Current Name" button next to Skip - unlike Skip (leaves the file
unresolved, so a future run asks about it again), this treats the
file's current name as a confirmed match and caches it, without
renaming anything.

If you installed v1.8.0, update to
[**v1.8.1**](https://github.com/malarrya/dnd-pdf-renamer/releases/tag/v1.8.1)
or later - some front-page previews (and, more importantly, the
automated cover-image matching layer) could pick a tiny or garbled
embedded image instead of the real scan, when a PDF's page stored a
small logo, watermark, low-res thumbnail, or mask alongside the actual
page image. Both now pick the largest embedded image on the page
instead of whichever one happened to come first internally.

[**v1.9.0**](https://github.com/malarrya/dnd-pdf-renamer/releases/tag/v1.9.0)
identifies non-PDF catalog entries too - a handful of LaunchBox entries
have no PDF at all, just a CD-ROM/installer shortcut (e.g. "AD&D Core
Rules 2.0 Expansion"), which every previous version silently ignored
since it only ever looked at `*.pdf` files. Every run now also matches
any non-PDF file in your folder against those entries by filename alone
(there's no content to check), acting only on an exact or unambiguous
match and leaving anything uncertain untouched.

If you installed v1.0.0 and hit an infinite "Press Enter to continue" loop
that kept re-spawning itself, update to v1.0.1 or later - that version was
missing `multiprocessing.freeze_support()`, so a worker process would fail
to recognize itself as a worker, re-run the whole program from the top, and
spawn its own worker pool on top of that, compounding until the machine
bogged down.

## Requirements

*(Only relevant if you're running the script directly with Python, rather
than the Windows installer above.)*

- Python 3.9+
- [`pypdf`](https://pypi.org/project/pypdf/) (required)

Optional, for scanned PDFs with no embedded text layer:
- [`pytesseract`](https://pypi.org/project/pytesseract/) and [`Pillow`](https://pypi.org/project/Pillow/), plus the [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) engine itself (a separate system install — e.g. `winget install UB-Mannheim.TesseractOCR` on Windows, or `tesseract-ocr` via `apt`/`brew` elsewhere)
- [`imagehash`](https://pypi.org/project/ImageHash/), for the cover-image matching fallback

If the optional pieces aren't installed, OCR and cover-image matching are skipped (with a note printed at startup) and everything else still works.

```bash
pip install pypdf pytesseract Pillow imagehash
```

You don't strictly have to run this yourself first: on startup, the script checks whether `pypdf` is importable and, if not, offers to `pip install` it for you on the spot. Missing optional packages (or a missing Tesseract engine) are reported the same way, just as non-fatal notes rather than a blocking prompt.

## Setup

You'll need a LaunchBox platform with:
- An XML database file (`Data/Platforms/YourPlatform.xml`) listing each title, its notes/description, and its intended filename (`ApplicationPath`)
- A folder of box-art images matching those titles

On first run, a setup window opens asking for:
1. The path to your LaunchBox platform XML file
2. The folder containing your PDFs
3. The folder containing the box-art images
4. Where renamed files should go (can be the same as your PDF folder, to rename in place)

Each field has a **Browse...** button to navigate to the folder/file instead of typing the path by hand. (If tkinter isn't available in your Python install, the same four questions are asked as console prompts instead.)

Your answers are saved to `dnd_renamer_config.json` next to the script, so you won't be asked again on future runs - you'll just see a summary of the saved paths with the option to change them. See `dnd_renamer_config.example.json` for the expected format if you'd rather set it up by hand.

## Usage

```bash
python dnd_renamer.py
```

The scan itself runs in a window showing a progress bar and a scrolling log of everything happening (the same messages you'd otherwise only see in the console), with **Pause** (lets any files already in progress finish, then holds before starting more) and **Cancel** buttons that work at the same safe points a console Ctrl+C always could. Once the scan finishes, it asks - in the same window - whether to review anything left unmatched or renamed on a low-confidence guess:
- A file already renamed on a low-confidence guess shows the catalog's box-art image side by side with a preview of the PDF's own front page, so you can visually confirm or reject the suggestion instead of judging on the filename alone.
- A file that couldn't be matched at all shows that same front-page preview next to a searchable list of every catalog title not already claimed by another file this run - the algorithm's best guess, if it has one, comes pre-selected, but you can pick any other title directly instead of just accepting or rejecting that one guess. If the correct title isn't in the list, you can type it yourself, or check a box to also show titles already claimed by another file this run (for the rare case a claim was made in error, like a genuine duplicate PDF).

Nothing is renamed without either a confident automated match or your explicit confirmation.

If you're renaming in place (output folder same as PDF folder) and a previous run already confirmed some files, you'll be asked whether to do a **full** scan (re-verify every file's content from scratch) or an **incremental** one (skip any file whose size and modified time haven't changed since it was last confirmed, and only scan what's new or changed). Incremental scans avoid the full-file read needed to re-verify each PDF, which matters most when the PDF folder is on a network share.

## Sharing the fingerprint cache

The fingerprint cache (`dnd_renamer_cache.json`, next to the script) maps a file's SHA256 content hash to its confirmed title — nothing else. It's keyed purely on content, not filename or path, so it works just as well for identifying someone else's copy of a book as your own: classic TSR/D&D PDFs mostly trace back to a handful of original scans that circulate widely, so two collectors' copies of the same book are very often byte-for-byte identical.

That means a cache built up from one person's collection can give someone else's *first* run a head start: any of their PDFs that happen to be byte-identical to a file already confirmed here get resolved instantly, skipping content analysis and OCR entirely. It carries no PDF content and nothing copyright-sensitive — just hashes, confirmed titles, and which method confirmed them.

`dnd_renamer_cache.example.json` is a snapshot of one such cache for the D&D Classic Editions catalog. To use it, copy it to `dnd_renamer_cache.json` next to your own copy of the script before your first run. It won't help with files that aren't byte-identical to something already in it — those still go through the normal identification pipeline like any new file.

A file you identify yourself — confirming a pick in the "couldn't be confidently matched" review, or in 100%-manual mode — gets added to the cache exactly the same way as an automated match. That means a cache built up entirely by hand, from a fully manual pass over your whole collection, is just as useful to share as one built from automated matches alone.

## Beyond D&D Classic Editions

The matching logic isn't specific to any one catalog — it works from whatever XML platform file and image folder you point it at. Pointing it at a different LaunchBox platform (e.g. a D&D 5th Edition catalog) should work the same way, no code changes needed.
