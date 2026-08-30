# dnd_renamer

A content-based identification and renaming tool for classic TSR/D&D PDF manuals, matched against a [LaunchBox](https://www.launchbox-app.com/) platform XML catalog.

If you've got a folder of PDFs with inconsistent, cryptic, or just plain wrong filenames — scanned modules, rulebooks, accessories — this script reads each file's actual content (text, OCR, cover art) and renames it to match its real catalog entry.

## Design philosophy

**A wrong rename is worse than no rename.** Every identification layer requires a real, high-confidence signal before it will touch a file. When nothing is confident enough, the file is left alone and reported as unmatched rather than guessed at. You can optionally review low-confidence suggestions for unmatched files one at a time at the end of a run and confirm or reject each by hand.

## How it works

Each file goes through a cascade of identification layers, roughly cheapest/most-reliable first:

1. **Fingerprint cache** — if this exact file's content (SHA256) has been seen before, reuse the known answer instantly.
2. **Content-based matching** — extracts native PDF text and scores it against every catalog entry's description, page count, and file size. High score/margin thresholds required to accept a match.
3. **OCR fallback** — for scanned PDFs with no text layer, OCRs the front pages and back cover and compares against the catalog the same way.
4. **Cover-image matching** — perceptual-hash comparison against the catalog's box-art images, for scans OCR can't read at all.
5. **Process of elimination** — once most files are matched, whatever catalog entries are left unclaimed become a much smaller, less noisy pool to check remaining files against.
6. **Legacy filename matching** — last resort only, using whatever the file happens to already be named.

Every layer is filename-independent except the last, specifically so a badly-misnamed file can still be identified from its actual content, and so a wrong rename never gets "confirmed" as correct on a later run just because it inherited a bad name.

## Requirements

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

On first run, the script will prompt you for:
1. The path to your LaunchBox platform XML file
2. The folder containing your PDFs
3. The folder containing the box-art images
4. Where renamed files should go (can be the same as your PDF folder, to rename in place)

Your answers are saved to `dnd_renamer_config.json` next to the script, so you won't be asked again on future runs unless a saved path goes stale. See `dnd_renamer_config.example.json` for the expected format if you'd rather set it up by hand.

## Usage

```bash
python dnd_renamer.py
```

The script scans your PDF folder in parallel, reports how many files it confidently matched, and offers to walk you through best-guess suggestions for anything left unmatched. Nothing is renamed without either a confident automated match or your explicit confirmation.

If you're renaming in place (output folder same as PDF folder) and a previous run already confirmed some files, you'll be asked whether to do a **full** scan (re-verify every file's content from scratch) or an **incremental** one (skip any file whose size and modified time haven't changed since it was last confirmed, and only scan what's new or changed). Incremental scans avoid the full-file read needed to re-verify each PDF, which matters most when the PDF folder is on a network share.

## Beyond D&D Classic Editions

The matching logic isn't specific to any one catalog — it works from whatever XML platform file and image folder you point it at. Pointing it at a different LaunchBox platform (e.g. a D&D 5th Edition catalog) should work the same way, no code changes needed.
