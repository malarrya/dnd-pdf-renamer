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

The scan itself runs in a window showing a progress bar and a scrolling log of everything happening (the same messages you'd otherwise only see in the console), with **Pause** (lets any files already in progress finish, then holds before starting more) and **Cancel** buttons that work at the same safe points a console Ctrl+C always could. Once the scan finishes, it asks - in the same window - whether to review anything left unmatched or renamed on a low-confidence guess; reviewing one shows the catalog's box-art image side by side with a preview of the PDF's own front page, so you can visually confirm or reject the suggestion instead of judging on the filename alone. Nothing is renamed without either a confident automated match or your explicit confirmation.

If you're renaming in place (output folder same as PDF folder) and a previous run already confirmed some files, you'll be asked whether to do a **full** scan (re-verify every file's content from scratch) or an **incremental** one (skip any file whose size and modified time haven't changed since it was last confirmed, and only scan what's new or changed). Incremental scans avoid the full-file read needed to re-verify each PDF, which matters most when the PDF folder is on a network share.

## Sharing the fingerprint cache

The fingerprint cache (`dnd_renamer_cache.json`, next to the script) maps a file's SHA256 content hash to its confirmed title — nothing else. It's keyed purely on content, not filename or path, so it works just as well for identifying someone else's copy of a book as your own: classic TSR/D&D PDFs mostly trace back to a handful of original scans that circulate widely, so two collectors' copies of the same book are very often byte-for-byte identical.

That means a cache built up from one person's collection can give someone else's *first* run a head start: any of their PDFs that happen to be byte-identical to a file already confirmed here get resolved instantly, skipping content analysis and OCR entirely. It carries no PDF content and nothing copyright-sensitive — just hashes, confirmed titles, and which method confirmed them.

`dnd_renamer_cache.example.json` is a snapshot of one such cache for the D&D Classic Editions catalog. To use it, copy it to `dnd_renamer_cache.json` next to your own copy of the script before your first run. It won't help with files that aren't byte-identical to something already in it — those still go through the normal identification pipeline like any new file.

## Beyond D&D Classic Editions

The matching logic isn't specific to any one catalog — it works from whatever XML platform file and image folder you point it at. Pointing it at a different LaunchBox platform (e.g. a D&D 5th Edition catalog) should work the same way, no code changes needed.
