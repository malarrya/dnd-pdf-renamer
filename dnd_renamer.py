import concurrent.futures
from collections import Counter
import difflib
import hashlib
import io
import json
import logging
import math
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

# A --windowed (GUI-subsystem, no console) frozen build has no real stdio
# at all - sys.stdout/stderr/stdin are None, not just closed - so any bare
# print()/input() call anywhere (dependency checks before the GUI takes
# over, a worker subprocess inheriting the same windowed subsystem, the
# console-fallback path if tkinter is ever missing, etc.) would crash with
# AttributeError before any window has a chance to appear. Swapping in
# harmless no-op streams here, once, keeps every existing call site safe
# without auditing each one - a no-op for a normal console/script run,
# where these are never None to begin with.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")
if sys.stdin is None:
    sys.stdin = open(os.devnull, "r")

# --- REQUIRED: PDF READING ---
# Unlike the OCR/cover-hash fallbacks below, nothing in this script can run
# at all without this - so a missing pypdf isn't allowed to degrade
# gracefully, it's caught here and resolved (or the script exits) by
# check_dependencies() before any real work starts. PdfReader stays defined
# as None on failure so nothing NameErrors before that exit happens.
PYPDF_AVAILABLE = False
try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except ImportError:
    PdfReader = None

# --- OPTIONAL OCR FALLBACK ---
# A scanned book with no text layer at all (a pure image scan) gives every
# identification layer nothing to work with. OCR-ing a handful of front
# pages recovers many of those. This is entirely optional: pytesseract and
# Pillow are pip-installable, but the actual OCR engine (Tesseract) is a
# separate system install (e.g. `winget install UB-Mannheim.TesseractOCR`
# on Windows, or the tesseract-ocr package via apt/brew elsewhere) - if
# it's missing, OCR is silently skipped and everything else still works
# exactly as before.
OCR_AVAILABLE = False
OCR_PACKAGES_AVAILABLE = False  # pytesseract/Pillow specifically, distinct from the Tesseract binary itself - see check_dependencies()
try:
    import pytesseract
    from PIL import Image, ImageOps

    OCR_PACKAGES_AVAILABLE = True
    _tesseract_cmd = shutil.which("tesseract")
    if not _tesseract_cmd:
        for candidate in (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ):
            if os.path.isfile(candidate):
                _tesseract_cmd = candidate
                break
    if _tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = _tesseract_cmd
        OCR_AVAILABLE = True
except ImportError:
    pass


# --- OPTIONAL COVER-IMAGE FALLBACK ---
# Comparing a PDF's own front-cover page against the LaunchBox box-art
# images by perceptual hash is a much cheaper alternative to OCR (no text
# recognition at all - just a resized-image fingerprint and a Hamming
# distance), and can succeed on scans OCR can't read at all (skewed,
# blurry, low-contrast). Optional the same way OCR is: imagehash is
# pip-installable and only needs Pillow, which OCR already requires.
IMAGEHASH_AVAILABLE = False
try:
    import imagehash

    IMAGEHASH_AVAILABLE = True
except ImportError:
    pass


def _offer_pip_install(package_names):
    """Asks once, then runs `pip install` for every package in one call.
    Returns True only if the install command itself succeeded - the
    caller still has to re-import to confirm the package is actually
    usable now (a stale/broken environment can make pip succeed without
    the import actually working). In a frozen (PyInstaller) build there's
    no bundled pip and sys.executable is this exe, not a Python
    interpreter, so re-launching it with `-m pip install ...` would just
    relaunch the app itself - refuse instead of doing that."""
    if getattr(sys, "frozen", False):
        print("  This packaged build should already include every required package.")
        print("  If you're seeing this, please report it as a bug.")
        return False
    try:
        answer = input(f"  Install now via pip ({' '.join(package_names)})? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if answer in ("n", "no"):
        return False
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *package_names])
        return True
    except Exception as e:
        print(f"  Install failed: {e}")
        return False


def check_dependencies():
    """Verifies pypdf - the one package nothing here can run without - is
    actually importable, offering to pip install it on the spot rather
    than crashing on the first PdfReader() call with a bare traceback.
    Everything else (OCR, cover-image matching) already degrades
    gracefully by design (see OCR_AVAILABLE/IMAGEHASH_AVAILABLE above),
    so those are reported here only as informational notes - never
    fatal - once, up front, so a silently-disabled feature doesn't look
    like a bug three layers deep in a run."""
    global PdfReader, PYPDF_AVAILABLE

    if not PYPDF_AVAILABLE:
        print("=" * 50)
        print("  Missing required package: pypdf")
        print("  This script can't read any PDF at all without it.")
        print("=" * 50)
        if _offer_pip_install(["pypdf"]):
            try:
                from pypdf import PdfReader as _PdfReader
                PdfReader = _PdfReader
                PYPDF_AVAILABLE = True
                print("  pypdf installed successfully.\n")
            except ImportError:
                pass
        if not PYPDF_AVAILABLE:
            print("  Install it manually, then run this script again:")
            print("    pip install pypdf")
            sys.exit(1)

    notes = []
    if not OCR_PACKAGES_AVAILABLE:
        notes.append(
            "  OCR fallback (for scanned PDFs with no text layer) is disabled.\n"
            "    Install with:  pip install pytesseract Pillow\n"
            "    Also requires the Tesseract OCR engine itself - a separate,\n"
            "    non-Python install (e.g. `winget install UB-Mannheim.TesseractOCR`\n"
            "    on Windows, or the tesseract-ocr package via apt/brew elsewhere)."
        )
    elif not OCR_AVAILABLE:
        notes.append(
            "  OCR fallback is disabled: pytesseract/Pillow are installed, but the\n"
            "    Tesseract OCR engine itself wasn't found on this machine. Install it\n"
            "    separately (e.g. `winget install UB-Mannheim.TesseractOCR` on Windows,\n"
            "    or the tesseract-ocr package via apt/brew elsewhere) and run again."
        )
    if not IMAGEHASH_AVAILABLE:
        notes.append(
            "  Cover-image matching fallback is disabled.\n"
            "    Install with:  pip install imagehash"
        )

    if notes:
        print("=" * 50)
        print("  Optional features unavailable (everything else still works):")
        for note in notes:
            print(note)
        print("=" * 50 + "\n")


# A page with body text printed over a colored background (common on back
# covers - e.g. black text on an olive-green panel) can defeat Tesseract's
# default recognition outright, returning almost nothing, even though the
# text is perfectly legible to a human. Plain OCR is tried first since
# it's cheap and correctly handles the common case (ordinary black-on-
# white scans); only when it reads poorly does this fall back to
# checking a handful of black/white brightness cutoffs, since no single
# fixed cutoff works across every scan's background color and lighting.
OCR_THRESHOLD_SWEEP = (90, 110, 130, 150, 170)
OCR_GOOD_ENOUGH_CONFIDENCE = 80

# Tesseract's own recognition time scales with pixel count, and a flatbed
# scan is often captured at a far higher resolution than text recognition
# actually needs - 300 DPI on a standard printed page is already about
# 2550x3300px. Shrinking anything larger before handing it to Tesseract
# cuts recognition time substantially (and multiplies across every
# attempt in the threshold sweep below) with no measured accuracy loss;
# anything already at or under this is left untouched.
OCR_MAX_DIMENSION = 2200


def _downsize_for_ocr(pil_image):
    """Shrinks an oversized scan to a resolution Tesseract doesn't need to
    spend extra time on. A no-op for images already at or below
    OCR_MAX_DIMENSION on their longest edge."""
    width, height = pil_image.size
    longest = max(width, height)
    if longest <= OCR_MAX_DIMENSION:
        return pil_image
    scale = OCR_MAX_DIMENSION / longest
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    return pil_image.resize(new_size, Image.LANCZOS)


def _ocr_with_confidence(pil_image):
    """Returns (text, average_word_confidence). Confidence - not output
    length - is what makes the threshold sweep below safe: an earlier
    version that just kept whichever attempt produced the most
    characters was once fooled by a badly garbled attempt that
    hallucinated more text than a shorter-but-accurate one."""
    data = pytesseract.image_to_data(pil_image, output_type=pytesseract.Output.DICT)
    words, confs = [], []
    for text, conf in zip(data["text"], data["conf"]):
        text = text.strip()
        conf = int(conf)
        if text and conf >= 0:
            words.append(text)
            confs.append(conf)
    if not words:
        return "", 0.0
    return " ".join(words), sum(confs) / len(confs)


def ocr_image_best(pil_image):
    """OCRs one page image, retrying with a brightness-threshold sweep
    when the plain attempt isn't confident, and keeping whichever
    attempt Tesseract itself was most sure of. Returns "" on failure."""
    pil_image = _downsize_for_ocr(pil_image)
    try:
        best_text, best_conf = _ocr_with_confidence(pil_image)
    except Exception:
        return ""
    if best_conf >= OCR_GOOD_ENOUGH_CONFIDENCE and best_text:
        return best_text

    try:
        gray = ImageOps.grayscale(pil_image)
    except Exception:
        return best_text

    for threshold in OCR_THRESHOLD_SWEEP:
        try:
            bw = gray.point(lambda p, t=threshold: 255 if p > t else 0)
            text, conf = _ocr_with_confidence(bw)
        except Exception:
            continue
        if conf > best_conf:
            best_text, best_conf = text, conf

    return best_text

# Some Windows terminals default stdout to a legacy codepage (cp1252) that
# can't encode the checkmark/warning emoji below and would crash mid-run.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Old/scanned PDFs often have malformed numeric values buried in internal
# structures we never use (annotation coordinates, page geometry...).
# pypdf recovers from these automatically - substituting 0.0 and moving on
# - but logs a "could not convert string to float ... FloatObject invalid"
# warning every single time it does. A single old scan can hit this dozens
# of times, so across a big catalogue it reads as a wall of alarming text
# despite nothing being wrong. Nothing this script does depends on those
# geometry values (only extracted text, page count, and file size matter),
# so this warning class is quieted; pypdf's real errors (ERROR level and
# above) still get through.
logging.getLogger("pypdf").setLevel(logging.ERROR)

# --- USER CONFIGURATION ---
# These are set at startup by configure_paths() (below), which prompts on
# first run and remembers the answers in a config file next to this script
# for every run after that. They start as None so a code path that reads
# them before configure_paths() runs fails loudly instead of silently
# using someone else's folder layout.
XML_PATH = None
PDF_DIRECTORY = None
IMAGE_DIRECTORY = None
OUTPUT_DIRECTORY = None  # Safe to keep identical to PDF_DIRECTORY

# When frozen into a PyInstaller onefile exe, __file__ resolves inside the
# temporary _MEIxxxx extraction dir (wiped after every run), not next to the
# exe - so config/cache would silently reset on every launch. sys.executable
# is the exe's real, persistent location in that case.
_APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))

CONFIG_PATH = os.path.join(_APP_DIR, "dnd_renamer_config.json")

# Not user-configurable - always lives next to the script, same as
# CONFIG_PATH, so it travels with a distributed copy automatically.
CACHE_PATH = os.path.join(_APP_DIR, "dnd_renamer_cache.json")

# Same idea as CACHE_PATH, but keyed by filename instead of content hash -
# see load_scan_index for why a separate index is needed for incremental
# scans.
SCAN_INDEX_PATH = os.path.join(_APP_DIR, "dnd_renamer_scan_index.json")

# Optional hooks consumed by a GUI (see dnd_renamer_gui.py's
# run_scan_window). PROGRESS_HOOK, if set, is called as (phase: str,
# completed: int, total: int) at each scan/rename/suggestion progress
# step - total == 0 means "indeterminate" (no meaningful count yet).
# CANCEL_EVENT lets a GUI's Cancel button (or a SIGINT handler bridged
# from the console - real Ctrl+C can't reach a background thread
# directly, since SIGINT is only ever delivered to the main thread)
# request cancellation of a scan running off the main thread.
# PAUSE_EVENT lets a GUI's Pause button hold the scan at its next
# checkpoint without cancelling it. Dispatching new work stops
# immediately; the worker processes already in flight (up to
# SCAN_WORKERS of them) still run to completion in the background -
# actually freezing them via Windows' process/thread suspend APIs
# (NtSuspendProcess, and separately SuspendThread on each of a worker's
# threads) was tried and reproducibly confirmed NOT to work in at least
# one real environment this was tested in: both report success (a valid
# handle, STATUS_SUCCESS) while the target process keeps running
# completely unaffected, verified with a counter file the "suspended"
# process kept incrementing on schedule throughout. That's consistent
# with this being a virtualized/remote-session Windows install - the
# same kind of environment behind several other display/rendering
# oddities found in this app - not something fixable from here. The
# achievable version is what's implemented: new dispatches stop
# immediately, so a pause never grows past whatever's already in flight.
# _check_control() re-raises cancellation as KeyboardInterrupt so it's
# caught by the exact same cleanup paths a console Ctrl+C already goes
# through. A plain console run never sets either event, so both are
# always a no-op there.
# CONFIRM_HOOK, if set, replaces the console y/n prompt in
# review_unmatched_interactively with a GUI comparison dialog (box art
# vs. the PDF's own front page) - see dnd_renamer_gui.py. Unlike the
# other hooks, this one is a blocking round-trip: it's called from the
# scan's background thread and must return "yes"/"no"/"stop" only once
# an actual human has clicked something, so the GUI side of it hands the
# request to the main thread (Tkinter's home) and blocks on a
# threading.Event until a button click sets it.
PROGRESS_HOOK = None
CONFIRM_HOOK = None
YESNO_HOOK = None
CANCEL_EVENT = threading.Event()
PAUSE_EVENT = threading.Event()


def _report_progress(phase, completed, total):
    if PROGRESS_HOOK is not None:
        try:
            PROGRESS_HOOK(phase, completed, total)
        except Exception:
            pass


def _confirm_suggestion(pdf_file, safe_title, detail, box_art_path, preview_image):
    """Asks a human to confirm or reject a rename (a never-yet-applied
    guess, or one already applied on a low-confidence layer) via
    CONFIRM_HOOK (a GUI comparison dialog showing the catalog's box art
    next to the PDF's own front page) if one is registered, else the
    original console y/n prompt. `detail` is a short, already-formatted
    description of why this suggestion was made (e.g. "(guess from
    document text, score 0.62)" or "[Legacy filename match (low
    confidence)]") - callers differ in what they have to say there, but
    the dialog itself doesn't need to know which case it is. Returns
    "yes", "no", or "stop" (stop reviewing the rest, matching Ctrl+C/EOF
    at the console prompt)."""
    if CONFIRM_HOOK is not None:
        try:
            return CONFIRM_HOOK({
                "pdf_file": pdf_file,
                "safe_title": safe_title,
                "detail": detail,
                "box_art_path": box_art_path,
                "preview_image": preview_image,
            })
        except Exception:
            pass  # fall through to the console prompt as a safety net
    try:
        answer = input(f"'{pdf_file}' -> '{safe_title}.pdf'?  {detail}  [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "stop"
    return "yes" if answer in ("y", "yes") else "no"


def _confirm_yesno(message, allow_stop=False):
    """Plain yes/no counterpart to _confirm_suggestion, for a gate
    question with nothing to visually compare (e.g. "Review best-guess
    suggestions for them one at a time?"). Via YESNO_HOOK (a GUI dialog)
    if one is registered, else the original console y/n prompt. Returns
    "yes" or "no", plus "stop" when allow_stop is set and the console
    prompt hit Ctrl+C/EOF (there's no interactive terminal under the
    GUI, so without a hook this always resolves to "stop"/"no")."""
    if YESNO_HOOK is not None:
        try:
            return YESNO_HOOK(message, allow_stop)
        except Exception:
            pass  # fall through to the console prompt as a safety net
    try:
        answer = input(f"{message} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "stop" if allow_stop else "no"
    return "yes" if answer in ("y", "yes") else "no"


def _check_cancelled():
    if CANCEL_EVENT.is_set():
        raise KeyboardInterrupt


def _check_control():
    """Call at each of the scan/rename loop's natural checkpoints."""
    _check_cancelled()
    while PAUSE_EVENT.is_set():
        time.sleep(0.2)
        _check_cancelled()


def _strip_quotes(raw):
    # Windows Explorer's "Copy as path" wraps the value in quotes.
    return raw.strip().strip('"').strip("'")


def _is_valid_dir_or_creatable(value):
    """Accepted for an output folder even if it doesn't exist yet (the
    script creates it via os.makedirs), as long as its parent does - that
    catches a typo'd drive/root without forcing the folder to pre-exist."""
    if os.path.isdir(value):
        return True
    parent = os.path.dirname(value.rstrip("\\/")) or value
    return os.path.isdir(parent)


def prompt_for_path(label, kind, default=None):
    """kind is 'file', 'dir', or 'output_dir'. Keeps re-asking until the
    entry is valid. If `default` is given, it's shown in the prompt and an
    empty answer (just pressing Enter) keeps it as-is, unvalidated (it was
    already valid when it became the default)."""
    if kind == "file":
        check, noun = os.path.isfile, "file"
    elif kind == "dir":
        check, noun = os.path.isdir, "folder"
    else:
        check, noun = _is_valid_dir_or_creatable, "folder"

    suffix = f"\n  [Enter = {default}]" if default else ""
    while True:
        try:
            raw = input(f"{label}{suffix}\n> ")
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(1)
        value = _strip_quotes(raw)
        if not value:
            if default:
                return default
            print(f"  Please enter a path to a {noun}.\n")
            continue
        if not check(value):
            print(f"  Couldn't find a {noun} at that path - double-check it and try again.\n")
            continue
        return value


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def _load_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _config_is_fully_valid(config):
    return (
        os.path.isfile(config.get("xml_path", ""))
        and os.path.isdir(config.get("pdf_directory", ""))
        and os.path.isdir(config.get("image_directory", ""))
        and bool(config.get("output_directory"))
    )


def configure_paths():
    """Loads saved folder paths, or prompts for them - on first run, when a
    saved path has gone stale (e.g. a mapped drive letter that isn't
    visible from this session - see the UNC-path note printed on failure),
    or whenever the user asks to change them (e.g. switching to a
    different platform's XML, like D&D 5th Edition instead of D&D Classic
    Editions). Prompts via a GUI window (four browsable fields) when
    tkinter is importable - see dnd_renamer_gui.py - falling back to the
    console prompts below otherwise. Sets the XML_PATH/PDF_DIRECTORY/
    IMAGE_DIRECTORY/OUTPUT_DIRECTORY globals used by the rest of the
    script."""
    global XML_PATH, PDF_DIRECTORY, IMAGE_DIRECTORY, OUTPUT_DIRECTORY

    try:
        from dnd_renamer_gui import confirm_paths_gui, configure_paths_gui
    except ImportError:
        confirm_paths_gui = configure_paths_gui = None

    config = _load_config()
    reconfigure = not _config_is_fully_valid(config)

    if not reconfigure:
        if confirm_paths_gui is not None:
            choice = confirm_paths_gui(config)
            if choice is None:
                print("\nCancelled.")
                sys.exit(1)
            reconfigure = choice == "change"
        else:
            print("Using saved settings:")
            print(f"  LaunchBox XML file : {config['xml_path']}")
            print(f"  PDF folder         : {config['pdf_directory']}")
            print(f"  Image folder       : {config['image_directory']}")
            print(f"  Output folder      : {config['output_directory']}")
            try:
                answer = input("Press Enter to continue, or type 'c' to change any of these: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nCancelled.")
                sys.exit(1)
            reconfigure = answer in ("c", "change")

    if reconfigure:
        stale = bool(config) and not _config_is_fully_valid(config)

        if configure_paths_gui is not None:
            print("\nOpening the setup window...\n")
            new_config = configure_paths_gui(config, stale=stale)
            if new_config is None:
                print("Cancelled.")
                sys.exit(1)
            config = new_config
        else:
            print()
            print("=" * 50)
            if stale:
                print("  One or more saved paths couldn't be found. If a path")
                print("  uses a mapped drive letter (e.g. Z:\\...), it may only")
                print("  exist in a different login session - try the network")
                print("  path instead, e.g. \\\\server\\share\\folder.")
            else:
                print("  Set up folder paths (saved for next time).")
            print(f"  Config file: {CONFIG_PATH}")
            print("=" * 50 + "\n")

            config["xml_path"] = prompt_for_path(
                r"Path to the LaunchBox platform XML file for this edition (e.g. C:\LaunchBox\Data\Platforms\D&D Classic Editions.xml, or ...\D&D 5th Edition.xml):",
                "file",
                default=config.get("xml_path") if os.path.isfile(config.get("xml_path", "")) else None,
            )
            config["pdf_directory"] = prompt_for_path(
                "Path to the folder containing the PDFs you want renamed:",
                "dir",
                default=config.get("pdf_directory") if os.path.isdir(config.get("pdf_directory", "")) else None,
            )
            config["image_directory"] = prompt_for_path(
                r"Path to the LaunchBox 'Box - Front' image folder for this platform:",
                "dir",
                default=config.get("image_directory") if os.path.isdir(config.get("image_directory", "")) else None,
            )
            config["output_directory"] = prompt_for_path(
                "Output folder for the renamed PDFs (press Enter to use the same folder as the PDFs above):",
                "output_dir",
                default=config["pdf_directory"],
            )

        save_config(config)
        print(f"\nSaved. (Edit or delete {CONFIG_PATH} to change these later.)\n")

    XML_PATH = config["xml_path"]
    PDF_DIRECTORY = config["pdf_directory"]
    IMAGE_DIRECTORY = config["image_directory"]
    OUTPUT_DIRECTORY = config["output_directory"]

# Common words stripped out before scoring Notes-description overlap, so
# generic filler doesn't drown out the words that actually distinguish one
# product from another.
STOPWORDS = {
    'the', 'and', 'for', 'with', 'from', 'that', 'this', 'your', 'you', 'are',
    'can', 'have', 'has', 'will', 'all', 'any', 'use', 'used', 'using',
    'into', 'out', 'over', 'game', 'games', 'rules', 'rule', 'set', 'sets',
    'book', 'books', 'guide', 'new', 'more', 'also', 'other', 'these',
    'those', 'their', 'they', 'was', 'were', 'not', 'but', 'how', 'what',
    'when', 'where', 'which', 'who', 'includes', 'including', 'contains',
    'provides', 'features', 'designed', 'players', 'player',
    # Short function words - never discriminating on their own, but short
    # enough (unlike most words above) to survive significant_words'
    # length floor by accident in Layer 3's weaker filter (see pdf_tokens
    # in identify_file_pass2) - explicitly excluded there rather than relying
    # on length alone, since real short codes like "s2"/"b3"/"iq2" need
    # to survive that same filter.
    'an', 'of', 'on', 'in', 'to', 'or', 'as', 'at', 'by', 'is', 'it',
}

# Bare franchise/trademark names that are reprinted verbatim in the legal
# notice of virtually every book in the line - a verbatim hit on one of
# these proves nothing about which specific book is being examined, unlike
# a hit on an actual product title.
GENERIC_TITLE_BLACKLIST = {
    'dungeons and dragons',
    'advanced dungeons and dragons',
}


def clean_string(text):
    """Normalizes underscores, symbols, and spaces for accurate text comparison."""
    if not text:
        return ""
    text = text.replace('_', ' ').replace('-', ' ').replace('&', ' and ')
    text = text.lower().replace(' n ', ' and ')
    cleaned = "".join([c for c in text if c.isalnum() or c.isspace()])
    return " ".join(cleaned.split())


def strip_product_code(clean_text):
    """Removes a leading 'tsr1234' catalog-code token, leaving just the
    descriptive title. The catalog code is exactly the part that's wrong
    when a file has been mis-renamed, so it must never be trusted as
    evidence of its own correctness."""
    return re.sub(r'^tsr\d+\s*', '', clean_text).strip()


def strip_any_product_code(clean_text):
    """Like strip_product_code, but also strips placeholder codes like
    'TSRXXXX' (letters, not just digits) - used only to detect generic
    placeholder entries with a more specific sibling (see
    find_specific_siblings); never used for the main title-matching path,
    since a placeholder code is not a real identifier."""
    return re.sub(r'^tsr\w*\s*', '', clean_text).strip()


def find_specific_siblings(item, xml_items):
    """The XML database uses the literal code 'TSRXXXX' as a placeholder
    for dozens of unrelated products whose real catalog number is
    unknown - it identifies nothing on its own. Occasionally a generic
    'TSRXXXX' entry sits alongside one or more specific, genuinely-coded
    siblings describing the exact same thing in more detail (e.g.
    'TSRXXXX: Fast-Play Game: Worlds of Adventure' next to BOTH
    'TSR11331: ...: Wrath of the Minotaur' and 'TSR11373: ...: Eye of
    the Wyvern')."""
    if not item['title'].startswith('TSRXXXX:'):
        return []
    base = strip_any_product_code(item['clean_title'])
    if not base:
        return []
    return [
        other for other in xml_items
        if other is not item
        and not other['title'].startswith('TSRXXXX:')
        and strip_any_product_code(other['clean_title']).startswith(base + ' ')
    ]


def mark_generic_placeholders_with_siblings(xml_items):
    """Flags (via item['has_specific_sibling']) every generic 'TSRXXXX'
    entry that has a more specific, genuinely-coded sibling, so those
    generic entries can be excluded from scoring entirely. Measured in
    production: a generic entry's Notes blurb is often much SHORTER than
    its specific siblings' (e.g. 23 words vs. 75+), which - exactly like
    the blank-form-product problem - lets it score deceptively high
    (1.00 vs. ~0.45) off pure chance overlap. That gap is far too wide
    for a margin-based "prefer the sibling if it's close" rule to catch,
    so the generic entry is excluded outright instead whenever a more
    specific alternative exists; the specific sibling (or, failing that,
    a completely different candidate) gets to compete on its own merits
    instead of losing to short-blurb inflation. Call once after loading
    the database - this is O(n^2) worst case and must not be repeated
    per file."""
    for item in xml_items:
        item['has_specific_sibling'] = bool(find_specific_siblings(item, xml_items))


def significant_words(clean_text, min_len=4):
    return {w for w in clean_text.split() if len(w) >= min_len and w not in STOPWORDS}


OCR_MAX_PAGES = 5  # keep this small - OCR is orders of magnitude slower than native extraction

# Some scans embed a page as hundreds of thin horizontal image strips
# instead of one image (a scanner/PDF-generation artifact) - confirmed on
# a real 315-page file where one page alone held 199 images, nearly all
# 1244x8 pixels. Each Tesseract call costs ~1-1.3s of pure subprocess
# overhead regardless of content, so OCR-ing every strip on a page like
# that costs 200+ seconds for zero possible text (an 8px-tall image can't
# contain readable text at any resolution) - enough on its own to trip
# the per-file scan timeout. Skipping anything too small to plausibly
# hold text is pure upside: it can't discard real content, only garbage
# that would have OCR'd to nothing anyway.
OCR_MIN_IMAGE_DIMENSION = 20


def ocr_front_pages(reader, max_pages=OCR_MAX_PAGES):
    """Runs OCR against the embedded page images of the first few pages.
    A scanned book's title page and credits page - almost always within
    the first handful of pages - are exactly where OCR has the best chance
    of recovering the book's own stated title, so this deliberately does
    NOT try to OCR the whole document; that would be far too slow across
    a large catalogue for marginal additional benefit. This is the
    lower-yield OCR path (see identify_file_pass2 for why it's tried only after
    the back cover has already had a chance) - it's still kept as a
    fallback since a legible front title page can succeed here even when
    the back cover can't. Deliberately uses plain OCR, not the
    confidence-sweep in ocr_image_best: that sweep can retry a single
    image up to 6 times, which is worth paying for on the back cover (at
    most 1-2 images) but not across 5 whole front pages of them - on a
    218-page test file, running the sweep here pushed one file's
    identification time past 100 seconds. Returns cleaned text, or "" if
    OCR isn't available or nothing could be read."""
    if not OCR_AVAILABLE:
        return ""

    chunks = []
    page_count = len(reader.pages)
    for i in range(min(max_pages, page_count)):
        try:
            for img in reader.pages[i].images:
                pil_image = img.image if img.image is not None else Image.open(io.BytesIO(img.data))
                if min(pil_image.size) < OCR_MIN_IMAGE_DIMENSION:
                    continue
                pil_image = _downsize_for_ocr(pil_image)
                text = pytesseract.image_to_string(pil_image)
                if text:
                    chunks.append(text)
        except Exception:
            continue
    return clean_string(" ".join(chunks))


def analyze_pdf_internals(reader):
    """Extracts page count and normalized NATIVE (non-OCR) text sampled
    from the front (title/credits pages) and back (colophon) of the
    document - those are the pages most likely to state the book's
    actual title, regardless of what the file happens to be named right
    now. OCR fallback is applied later, as its own explicit steps in
    identify_file_pass2 - trying the cheaper, more productive back-cover
    check first before spending time on the broader (and less often
    decisive) front-page OCR pass. See ocr_back_cover_text() and
    ocr_front_pages(). Takes an already-open PdfReader (each identify_file_
    pass1/identify_file_pass2 call opens exactly one per file and shares it
    across its own layers, rather than each layer re-opening and
    re-parsing the same file over the network)."""
    page_count = 0
    internal_text = ""
    try:
        page_count = len(reader.pages)

        front = range(min(20, page_count))
        back = range(max(0, page_count - 10), page_count)
        pages_to_scan = sorted(set(front) | set(back))

        chunks = []
        for i in pages_to_scan:
            t = reader.pages[i].extract_text()
            if t:
                chunks.append(t)
        internal_text = clean_string(" ".join(chunks))
    except Exception:
        pass
    return page_count, internal_text


FINGERPRINT_HASH_CHUNK_SIZE = 4 * 1024 * 1024  # stream in 4MB chunks - these PDFs can be large


def hash_file_sha256(path):
    """Full-file SHA256, streamed so multi-hundred-MB PDFs don't need to
    be loaded into memory at once. Returns None if the file can't be
    read (e.g. a transient network-share glitch) rather than raising -
    a fingerprint-cache miss is a harmless, recoverable outcome."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(FINGERPRINT_HASH_CHUNK_SIZE), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return None


def load_fingerprint_cache(cache_path):
    """SHA256 -> {'clean_title', 'title'} for PDFs already confirmed by a
    high-confidence identification method on some earlier run (by this
    user or, since the file travels alongside the script, by someone
    else's identical copy of the same scan). Missing or unreadable cache
    is just an empty cache, never a hard error."""
    if not os.path.exists(cache_path):
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_fingerprint_cache(cache_path, cache):
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
    except Exception as e:
        print(f"⚠️  Could not save fingerprint cache: {e}")


def load_scan_index(path):
    """filename -> {'size', 'mtime', 'sha256'}, recorded the last time that
    exact filename was scanned and confidently identified. An incremental
    scan (see partition_for_incremental_scan) trusts an entry only while the
    file's live size and mtime still match what's recorded here - computing
    the SHA256 needed for the fingerprint cache (see hash_file_sha256)
    requires reading the whole file, which is exactly the cost an
    incremental scan exists to avoid for files nothing has touched since
    they were last confirmed. Size+mtime both matching is as strong a
    proxy for "unchanged" as is available without paying that cost - a
    coincidental match on both for genuinely different content isn't
    realistically possible. Missing or unreadable index is just an empty
    index, never a hard error."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_scan_index(path, index):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2, sort_keys=True)
    except Exception as e:
        print(f"⚠️  Could not save scan index: {e}")


def partition_for_incremental_scan(pdf_files, pdf_directory, scan_index, fingerprint_cache):
    """Splits pdf_files into (skip_plans, to_scan_files) for an incremental
    scan. skip_plans is in the same (pdf_file, full_pdf_path,
    target_final_title, match_method) shape _run_parallel_scan produces, so
    callers can merge the two uniformly. A file is skipped only when its
    filename was indexed before (see load_scan_index) AND its live size and
    mtime still match what was recorded AND the fingerprint cache still has
    a confirmed title for that recorded SHA256 - any of those failing just
    means a real scan, same as if it had never been indexed at all."""
    skip_plans = []
    to_scan_files = []
    for pdf_file in pdf_files:
        entry = scan_index.get(pdf_file)
        cached = entry and fingerprint_cache.get(entry.get("sha256"))
        cached_title = cached.get("title") if cached else None
        full_pdf_path = os.path.join(pdf_directory, pdf_file)
        if not cached_title:
            to_scan_files.append(pdf_file)
            continue
        try:
            stat = os.stat(full_pdf_path)
        except OSError:
            to_scan_files.append(pdf_file)
            continue
        if stat.st_size != entry.get("size") or stat.st_mtime != entry.get("mtime"):
            to_scan_files.append(pdf_file)
            continue
        skip_plans.append((
            pdf_file, full_pdf_path, cached_title,
            "Fingerprint Cache -> Skipped (incremental scan, unchanged since last run)",
        ))
    return skip_plans, to_scan_files


def prompt_scan_mode(skip_candidate_count, total_count):
    """Asks whether to run a full scan (every file re-verified by content)
    or an incremental one (files unchanged since a previous confirmed match
    are trusted without re-reading them). Only called when there's at least
    one file the incremental path could actually skip."""
    print(f"\n{skip_candidate_count} of {total_count} PDFs are unchanged (same size and modified time) "
          f"since they were last confidently identified.")
    if YESNO_HOOK is not None:
        decision = _confirm_yesno(
            f"Skip those {skip_candidate_count} unchanged, already-confirmed file(s) and only scan the rest (incremental)?"
        )
        return "incremental" if decision == "yes" else "full"
    while True:
        try:
            answer = input(
                "Scan (F)ull - re-verify every file, or (I)ncremental - skip those and only scan the rest? [F/i]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nDefaulting to a full scan.")
            return "full"
        if answer in ("", "f", "full"):
            return "full"
        if answer in ("i", "incremental"):
            return "incremental"
        print("  Please enter 'f' or 'i'.\n")


def load_image_library(image_dir):
    """Indexes the verified LaunchBox image pack naming structures."""
    library = []
    if not os.path.exists(image_dir):
        print(f"⚠️ Warning: Image folder not found at '{image_dir}'.")
        return library

    valid_exts = ('.png', '.jpg', '.jpeg', '.bmp', '.gif')
    for f in os.listdir(image_dir):
        if f.lower().endswith(valid_exts):
            filename_no_ext, _ = os.path.splitext(f)
            clean_name = clean_string(filename_no_ext)
            library.append({
                'original_name': filename_no_ext.strip(),
                'clean_name': clean_name,
                'core_name': strip_product_code(clean_name),
                'file_path': os.path.join(image_dir, f),
            })
    return library


COVER_HASH_SIZE = 16  # phash bit-grid dimension - higher = more discriminating, still cheap


def build_cover_hash_index(image_library):
    """Precomputes a perceptual hash for every LaunchBox box-art image
    once per run, so identifying any single PDF only costs one hash
    comparison against this table rather than re-decoding every cover
    image for every file scanned."""
    index = []
    if not IMAGEHASH_AVAILABLE:
        return index
    for img in image_library:
        try:
            with Image.open(img['file_path']) as pil_img:
                phash = imagehash.phash(pil_img, hash_size=COVER_HASH_SIZE)
            index.append((phash, img))
        except Exception:
            continue
    return index


def pdf_cover_hash(reader):
    """Perceptual hash of the PDF's own first-page image (the front
    cover), or None if unavailable. Deliberately only page 1 - unlike
    the OCR layers, there's no reason a cover photo would appear
    anywhere else."""
    if not IMAGEHASH_AVAILABLE or len(reader.pages) == 0:
        return None
    try:
        for img in reader.pages[0].images:
            pil_image = img.image if img.image is not None else Image.open(io.BytesIO(img.data))
            return imagehash.phash(pil_image, hash_size=COVER_HASH_SIZE)
    except Exception:
        pass
    return None


# Placeholder thresholds - see the calibration pass before these are
# trusted to make a confident call on their own.
COVER_HASH_MAX_DISTANCE = 10
COVER_HASH_MIN_MARGIN = 8


def cover_image_match(reader, cover_hash_index):
    """Compares the PDF's own front-cover image against every LaunchBox
    box-art image by perceptual hash (Hamming distance between hashes)
    and returns (image_entry, method_string) on a clear winner, else
    (None, None). Much cheaper than OCR - no text recognition at all -
    and can succeed on scans OCR can't read (skewed, blurry, low-
    contrast covers). Returns the specific image directly rather than an
    XML item, since two catalog entries can otherwise share one core
    title (see resolve_display_name) - comparing actual cover art
    sidesteps that ambiguity entirely."""
    if not cover_hash_index:
        return None, None
    pdf_hash = pdf_cover_hash(reader)
    if pdf_hash is None:
        return None, None

    scored = sorted(
        ((pdf_hash - stored_hash, img) for stored_hash, img in cover_hash_index),
        key=lambda pair: pair[0],
    )
    top_distance, top_img = scored[0]
    runner_up_distance = scored[1][0] if len(scored) > 1 else 999

    if top_distance <= COVER_HASH_MAX_DISTANCE and (runner_up_distance - top_distance) >= COVER_HASH_MIN_MARGIN:
        return top_img, f"Cover Image Match -> perceptual hash (distance {top_distance}, margin {runner_up_distance - top_distance})"
    return None, None


def load_launchbox_db(xml_path):
    """Maps the authoritative LaunchBox platform XML schema metadata."""
    db = []
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception as e:
        print(f"❌ Error loading XML file: {e}")
        return []

    for game in root.findall('Game'):
        title = game.find('Title').text if game.find('Title') is not None else ""
        notes_raw = game.find('Notes').text if game.find('Notes') is not None else ""
        year = game.find('ReleaseYear').text if game.find('ReleaseYear') is not None else "9999"
        app_path = game.find('ApplicationPath').text if game.find('ApplicationPath') is not None else ""
        if not title:
            continue

        clean_title = clean_string(title)
        notes_clean = clean_string(notes_raw) if notes_raw else ""
        # The maintainer's own ApplicationPath already names the exact
        # file intended for THIS SPECIFIC entry - see resolve_display_name
        # for why that matters when two entries share an identical Title.
        app_filename = os.path.splitext(os.path.basename(app_path))[0].strip() if app_path else ""
        db.append({
            'title': title.strip(),
            'clean_title': clean_title,
            'core_title': strip_product_code(clean_title),
            'notes': notes_clean,
            'notes_words': significant_words(notes_clean),
            'year': year.strip() if year else "9999",
            'application_filename_clean': clean_string(app_filename) if app_filename else "",
            # Some catalog entries are pure software (an emulator/
            # installer shortcut, ApplicationPath ending ".lnk") - e.g.
            # "Core Rules: CD-ROM". No PDF can ever legitimately BE one of
            # these, so they must never be a content-matching candidate
            # anywhere (confirmed a real problem: their generic "core
            # rules" vocabulary spuriously attracted ~150 unrelated real
            # books once they were the only entries left unclaimed).
            # Deliberately narrower than "ApplicationPath doesn't end in
            # .pdf" - this catalog also has several genuine books/
            # adventures still pointing at a not-yet-extracted .rar/.zip
            # archive (ReleaseType "Boxed Set", not "Software"; see the
            # renumbering work that discovered these mislabeled archives
            # in the first place) - those ARE real, legitimately
            # PDF-matchable products and excluding them wrongly cost a
            # genuine match ("TSR1135: Introduction to Advanced Dungeons &
            # Dragons Game") its own correct identification.
            'is_pdf_product': not (bool(app_path) and app_path.lower().endswith('.lnk')),
        })
    return db


def resolve_display_name(xml_item, image_library):
    """Prefer the LaunchBox image library's exact filename style (it
    preserves punctuation like apostrophes) over the raw XML title."""
    # Two distinct catalog entries can share an identical Title and even
    # an identical product code - e.g. "TSR2010: Players Handbook" and a
    # separately-catalogued "TSR2010: Players Handbook" entry for a
    # different printing (an "Orange Spine" cover variant). Their box-art
    # images carry the extra distinguishing words ("... (Orange Spine)"),
    # which also changes their code-stripped core_name, so the candidates
    # search below (matched on core_name) never even considers that image
    # for either entry - both silently collapsed to whichever image
    # happened to match first, discarding which specific printing the
    # content match actually identified. Confirmed in production: the
    # Player's Handbook, Monster Manual, and Dungeon Masters Guide Orange
    # Spine pairs all did this, producing a same-name collision the
    # renamer then "resolved" with a meaningless "(2)" suffix. This
    # xml_item's own ApplicationPath already names exactly which physical
    # file the maintainer intended for THIS entry, so check the whole
    # image library for that exact filename first - it's unambiguous even
    # when title, product code, AND core_name all collide.
    if xml_item.get('application_filename_clean'):
        by_app_path = [img for img in image_library if img['clean_name'] == xml_item['application_filename_clean']]
        if by_app_path:
            return by_app_path[0]['original_name']

    candidates = [img for img in image_library if img['core_name'] == xml_item['core_title']]
    if len(candidates) > 1:
        # Two distinct catalog entries can share an identical
        # code-stripped title - e.g. 'TSR2010: Players Handbook' and
        # 'TSR2159: Player's Handbook' both reduce to core_title
        # "players handbook". Matching on core_name alone would silently
        # collapse both to whichever image happens to sort first,
        # discarding which specific printing the content match actually
        # identified. Break the tie using each candidate's own leading
        # product code against the winning item's.
        own_code = xml_item['clean_title'].split()[0] if xml_item['clean_title'] else ""
        exact = [
            img for img in candidates
            if img['clean_name'] and img['clean_name'].split()[0] == own_code
        ]
        if exact:
            return exact[0]['original_name']
        return candidates[0]['original_name']
    if candidates:
        return candidates[0]['original_name']
    for img in image_library:
        if xml_item['core_title'] and xml_item['core_title'] in img['clean_name']:
            return img['original_name']
    return xml_item['title'].replace(':', ' -')


def build_idf_table(xml_items):
    """Inverse-document-frequency weights for every word appearing in any
    Notes field. Generic thematic words shared across the whole product
    line ('monsters', 'magic', 'world', 'adventure'...) show up in hundreds
    of entries and must count for very little; a word specific to a
    handful of entries ('kara', 'gamefolio', 'skirmishes') is what actually
    identifies a book, and should dominate the overlap score."""
    n_docs = len(xml_items) or 1
    doc_freq = {}
    for item in xml_items:
        for w in item['notes_words']:
            doc_freq[w] = doc_freq.get(w, 0) + 1
    return {w: math.log(n_docs / df) + 1.0 for w, df in doc_freq.items()}


MIN_NOTES_WORDS_FOR_RATIO = 20


def notes_overlap_score(internal_words, item, idf):
    """A one-line marketing blurb (a coloring book, a character sheet pad)
    can rack up a near-perfect overlap RATIO purely by chance once the
    haystack is a 100+ page rulebook full of generic fantasy vocabulary -
    hitting nearly all of a 15-word blurb means much less than hitting
    two-thirds of an 80-word description. Requiring a reasonably sized
    Notes text before trusting the ratio keeps those short blurbs from
    ever outscoring the genuine (but wordier) match."""
    if len(item['notes_words']) < MIN_NOTES_WORDS_FOR_RATIO or not internal_words:
        return 0.0
    hits = item['notes_words'] & internal_words
    if not hits:
        return 0.0
    hit_weight = sum(idf.get(w, 1.0) for w in hits)
    total_weight = sum(idf.get(w, 1.0) for w in item['notes_words'])
    return hit_weight / total_weight if total_weight else 0.0


def page_count_bonus(page_count, item):
    """Rewards an explicit '128-page' / '128 page' style callout in the
    Notes that exactly matches the PDF's real page count - far more
    precise than just checking whether the digits appear anywhere."""
    if not page_count:
        return 0.0
    for m in re.finditer(r'(\d{2,3})[\s-]?page', item['notes']):
        if int(m.group(1)) == page_count:
            return 0.3
    return 0.0


def density_bonus(bytes_per_page, item):
    """Weak corroborating signal: color-plate-heavy scans run much larger
    per page than plain text booklets, so a Notes mention of color should
    line up with a denser file, and vice versa."""
    mentions_color = 'color' in item['notes'] or 'colour' in item['notes']
    if mentions_color and bytes_per_page > 120_000:
        return 0.1
    if not mentions_color and 0 < bytes_per_page < 40_000:
        return 0.05
    return 0.0


def score_candidate(candidate, internal_words, page_count, bytes_per_page, idf):
    return (
        notes_overlap_score(internal_words, candidate, idf)
        + page_count_bonus(page_count, candidate)
        + density_bonus(bytes_per_page, candidate)
    )


BLANK_FORM_KEYWORDS = (
    'record sheet', 'record sheets', 'adventure log', 'character folder',
    'hex book', 'hex pad', 'graph paper',
)


def is_blank_form_product(item):
    """Character sheets, hex paper, adventure logs, and similar blank
    stationery accessories have essentially no descriptive content - their
    Notes are just a list of the form-field names printed on the page
    (saving throw, movement, equipment, spells...), which is D&D's most
    universal vocabulary. In full-catalogue testing this made them
    reliable false-positive attractors for almost any character-related
    product (a class-specific "Player Pack" character sheet, a rules
    supplement discussing class abilities, ...), so they're excluded from
    content-based scoring entirely. They can still be identified by
    filename via the legacy layer."""
    return any(kw in item['clean_title'] for kw in BLANK_FORM_KEYWORDS)


# Universal reference/hub books whose Notes are dominated by D&D's most
# generic vocabulary (monster stat blocks, catch-all setting boilerplate)
# rather than anything specific to themselves - confirmed real case: a
# genuine 84-page match against "Blood Spawn: Creatures of Light and
# Shadow" (score 0.99) missed the required margin only because Monster
# Manual inflated a false runner-up score (0.79) purely from shared
# stat-block vocabulary, not any real overlap. Layer 2B (back-cover OCR)
# already handles these fine on its own - its matching-blocks scoring
# requires actual reproduced phrases, which generic vocabulary alone
# can't fake - so this exclusion is deliberately scoped to Layer 2's
# bag-of-words scoring only; applying it to Layer 2B too would risk
# blocking a still-unmatched numbered duplicate of one of these very
# books from matching itself, since the self-match exemption only fires
# once a file is already named after its own entry.
REFERENCE_HUB_KEYWORDS = (
    'monster manual',
    'forgotten realms campaign set',
)


def is_reference_hub_book(item):
    return any(kw in item['clean_title'] for kw in REFERENCE_HUB_KEYWORDS)


def has_verbatim_title(item, internal_text):
    """A short/specific phrase like "battlesystem fantasy combat
    supplement" or "mertwig's maze gamefolio" appearing verbatim in the
    PDF's own text is real evidence. But it is NOT proof of identity by
    itself: a supplement routinely says "requires the Forgotten Realms
    Campaign Set" or "for use with the Ravenloft Campaign Setting" in its
    own boilerplate, so a hub/core book's title turns up verbatim inside
    dozens of unrelated companion products. A *two-word* title
    ("oriental adventures", "expert set", "dungeons and dragons") is
    short enough to be an incidental mention on its own, so it takes at
    least three real (non-stopword) words before this counts for
    anything."""
    return (
        bool(internal_text)
        and len(significant_words(item['core_title'])) >= 3
        and item['core_title'] not in GENERIC_TITLE_BLACKLIST
        and item['core_title'] in internal_text
    )


# A verbatim title hit is folded in as a bonus on top of the Notes-overlap
# score rather than an automatic win, precisely because it isn't reliable
# proof on its own (see has_verbatim_title). Thresholds below were fit
# empirically against a real 994-book catalogue: a lone score/margin cutoff
# cannot perfectly separate genuine matches from "companion book mentions
# its hub book" false positives (their score distributions overlap), so
# this trades recall for precision - many correct matches will be left
# for a human/legacy layer rather than risk a confident wrong rename.
CONTENT_MATCH_MIN_SCORE = 0.90
CONTENT_MATCH_MIN_MARGIN = 0.20


def content_based_match(internal_text, page_count, file_size, xml_items, idf, clean_pdf_name=None):
    """Primary identification path. Trusts what's actually inside the PDF -
    its own title-page text, Notes-description overlap, page count, and
    page density - instead of tokens pulled from the (possibly already
    wrong) current filename. This is what keeps a bad rename from being
    re-confirmed as 'correct' on a later run."""
    internal_words = significant_words(internal_text) if internal_text else set()
    bytes_per_page = (file_size / page_count) if page_count else 0.0

    scored = []
    for item in xml_items:
        # is_blank_form_product/has_specific_sibling stop a generic/thin
        # entry from stealing OTHER files' identity by content persuasion
        # alone. But applied unconditionally they also block that entry's
        # own genuinely-matching file from ever confirming itself here,
        # which then falls through to a wrong sibling instead (seen with
        # both TSRXXXX placeholder codes and Hex Book vs Hexagonal Mapping
        # Booklet). The current file's own filename already exactly naming
        # this specific candidate is a strong, narrow enough signal to
        # exempt it - it still has to win the scoring race on content
        # below, this only lets it back into the running.
        if not item.get('is_pdf_product', True):
            continue
        if (is_blank_form_product(item) or item.get('has_specific_sibling') or is_reference_hub_book(item)) and item['clean_title'] != clean_pdf_name:
            continue
        base = score_candidate(item, internal_words, page_count, bytes_per_page, idf)
        bonus = 0.2 if has_verbatim_title(item, internal_text) else 0.0
        scored.append((base + bonus, base, bonus, item))
    scored.sort(key=lambda row: row[0], reverse=True)

    if not scored:
        return None, None

    top_score, top_base, top_bonus, top_item = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0

    if top_score >= CONTENT_MATCH_MIN_SCORE and (top_score - runner_up) >= CONTENT_MATCH_MIN_MARGIN:
        source = "title phrase + notes overlap" if top_bonus else "notes/page-count/size overlap"
        return top_item, f"Content Match -> {source} (score {top_score:.2f}, margin {top_score - runner_up:.2f})"

    return None, None


# Calibrated against real data: five files stuck as bare-numbered
# placeholders (every general-purpose layer, including this one at full
# catalogue scale, had already given up on them) scored 0.585-0.922 here
# once scored against only the handful of catalogue entries nothing else
# on disk had claimed. A confirmed non-match - a file whose true identity
# ("IQ2 - Forbidden Ground", see Layer 3) simply isn't in the catalogue at
# all - scored only 0.10 against that same shrunken pool. The floor sits
# roughly in the middle of that gap. No fixed margin requirement: one of
# the five genuine matches led its own runner-up by just 0.004 once the
# pool was down to single digits, so margin doesn't separate real from
# close-second here the way it does at full catalogue scale (see
# CONTENT_MATCH_MIN_MARGIN) - the pool being this small is itself already
# most of the safety margin.
UNCLAIMED_POOL_MIN_SCORE = 0.30


def unclaimed_pool_match(internal_text, page_count, file_size, unclaimed_xml_items, idf):
    """Last-resort process-of-elimination layer, tried only once every
    other layer has already given up. By that point most of the
    catalogue has usually already been claimed by some other correctly-
    identified file, so scoring against only the handful of entries
    nothing HAS claimed is a much less noisy problem than content_based_
    match's whole-catalogue comparison. This is exactly what let five
    files resolve that content_based_match had missed: their own text
    was dominated by table-of-contents listings rather than back-cover-
    style prose, which never scores competitively against a Notes field
    when competing against all ~1000 entries - but each one clearly stood
    out once compared only against the couple of entries nothing else had
    claimed. Deliberately native-text only (no OCR) and tried before any
    image-touching layer - two of those five files hang pypdf/Pillow's
    decoder on a malformed embedded image (see SCAN_TASK_TIMEOUT), so
    resolving them here, before anything OCR-based runs, is what lets
    them resolve at all instead of just timing out.

    Caveat: the unclaimed pool is computed once per worker process from
    whatever's on disk at scan start, not updated live as other files in
    the SAME batch get matched - so two different still-unidentified
    files in one run could theoretically both point at the same unclaimed
    entry. That's a same-name collision execute_renames already handles
    safely (a "(2)" suffix, never a silent overwrite), so the failure
    mode is a rare one-time human review, not a wrong rename."""
    if not unclaimed_xml_items:
        return None, None

    internal_words = significant_words(internal_text) if internal_text else set()
    if not internal_words:
        return None, None
    bytes_per_page = (file_size / page_count) if page_count else 0.0

    scored = []
    for item in unclaimed_xml_items:
        if not item.get('is_pdf_product', True):
            continue
        if is_blank_form_product(item) or item.get('has_specific_sibling') or is_reference_hub_book(item):
            continue
        base = score_candidate(item, internal_words, page_count, bytes_per_page, idf)
        bonus = 0.2 if has_verbatim_title(item, internal_text) else 0.0
        scored.append((base + bonus, item))
    scored.sort(key=lambda row: row[0], reverse=True)

    if not scored:
        return None, None
    top_score, top_item = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0

    if top_score >= UNCLAIMED_POOL_MIN_SCORE and top_score > runner_up:
        return top_item, f"Content Match -> process of elimination vs {len(unclaimed_xml_items)} unclaimed entries (score {top_score:.2f})"
    return None, None


def _find_box_art_path(display_name, image_library):
    """Reverse lookup from a resolve_display_name() result back to the
    box-art file it came from, for the visual side-by-side comparison in
    the low-confidence review step. Deliberately not a change to
    resolve_display_name's own return value - that function has callers
    throughout the scan that only ever wanted the name string, and its
    one fallback branch (no image_library entry at all) can't produce a
    path anyway. Returns None if nothing matches (fallback branch, or
    the image was removed from disk after resolve_display_name ran)."""
    for img in image_library:
        if img['original_name'] == display_name:
            return img['file_path']
    return None


def _extract_first_page_image(full_pdf_path):
    """Best-effort preview of the PDF's own front page for a human
    reviewing a low-confidence suggestion - the same embedded-image
    extraction pdf_cover_hash uses for hashing, just returning the image
    itself. Only works for scanned PDFs (a raster image embedded on page
    1); returns None for born-digital/text PDFs with no such image, or
    on any read error - the caller shows a placeholder in that case."""
    try:
        reader = PdfReader(full_pdf_path)
        if len(reader.pages) == 0:
            return None
        for img in reader.pages[0].images:
            return img.image if img.image is not None else Image.open(io.BytesIO(img.data))
    except Exception:
        pass
    return None


def best_guess_for_unmatched(full_pdf_path, xml_items, image_library, idf_table):
    """Only ever called for a file every confident layer above has
    already given up on. Computes the single strongest candidate anyway
    - no exclusions, no threshold - so a human reviewing the still-
    unmatched pile has somewhere to start instead of nothing. This is
    safe specifically because it's advisory, never auto-applied: a
    human confirming or rejecting the suggestion IS the safety check
    here, not the score. Returns (suggested_title, score, source_label,
    box_art_path) - box_art_path is None if the catalog has no image for
    the suggested title - or None if there's nothing even worth
    suggesting."""
    try:
        reader = PdfReader(full_pdf_path)
    except Exception:
        return None

    page_count, internal_text = analyze_pdf_internals(reader)
    try:
        file_size = os.path.getsize(full_pdf_path)
    except Exception:
        file_size = 0
    internal_words = significant_words(internal_text) if internal_text else set()
    bytes_per_page = (file_size / page_count) if page_count else 0.0

    best = None  # (score, item, source_label)

    if internal_words:
        scored = []
        for item in xml_items:
            if item.get('has_specific_sibling') or not item.get('is_pdf_product', True):
                continue
            base = score_candidate(item, internal_words, page_count, bytes_per_page, idf_table)
            bonus = 0.2 if has_verbatim_title(item, internal_text) else 0.0
            scored.append((base + bonus, item))
        scored.sort(key=lambda row: row[0], reverse=True)
        if scored and scored[0][0] >= 0.3:
            best = (scored[0][0], scored[0][1], "document text")

    if not best and OCR_AVAILABLE:
        back_text = ocr_back_cover_text(reader)
        if len(back_text) >= BACK_COVER_MIN_TEXT_CHARS:
            scored = sorted(
                (
                    (notes_similarity_score(back_text, item), item)
                    for item in xml_items
                    if not item.get('has_specific_sibling') and item.get('is_pdf_product', True)
                ),
                key=lambda pair: pair[0], reverse=True,
            )
            if scored and scored[0][0] >= 0.15:
                best = (scored[0][0], scored[0][1], "back cover")

    if not best:
        return None
    score, item, source = best
    display_name = resolve_display_name(item, image_library)
    return display_name, score, source, _find_box_art_path(display_name, image_library)


BLANK_FORM_FALLBACK_MIN_SCORE = 0.5
BLANK_FORM_FALLBACK_MIN_MARGIN = 0.40
# Recalibrated after a real production miss: a "Player Pack" bundle
# product (not itself a blank form) that happens to bundle mostly
# record-sheet pages scored HIGHER (1.08) against the generic "Character
# Record Sheets" entry than either genuine blank-form match did (0.73,
# 0.83) - so raising the score bar wouldn't have caught it, and would
# have thrown out both good matches. Its margin (0.36) was the one thing
# that came in lower than both genuine matches (0.44 each), so the
# margin floor moved up to 0.40 - just above the miss, just below the
# two hits. Only 3 real examples total for this layer, versus much
# broader validation elsewhere in this pipeline - treat it as less
# proven than the rest.


def blank_form_fallback_match(internal_text, page_count, file_size, xml_items, idf):
    """Blank-form/record-sheet products (see is_blank_form_product) are
    excluded from the main scoring pool above because their generic
    form-field vocabulary makes them false-positive attractors for
    unrelated books. But applied unconditionally, that same exclusion
    leaves a genuine blank-form file with no path to ever being
    identified at all: it has no informative filename for Layer 3 to
    use, and - confirmed on two real unmatched files - its own back
    cover is often trailing ad copy for unrelated products rather than
    its own content (scoring surfaced unrelated Giant-series modules,
    not itself). So this tries once more, restricted to competing only
    within the small blank-form pool, where the vocabulary is at least
    specific to that category - and only as a last resort after every
    non-blank-form candidate has already failed to explain the file.
    Thresholds calibrated from real unmatched files, including one
    production miss: a "Player Pack" bundle product (not itself a
    blank form, but mostly made up of bundled record-sheet pages)
    scored 1.08 with a 0.36 margin against the generic "Character
    Record Sheets" entry - higher than either genuine match's score
    (0.73, 0.83) - so the score floor alone can't catch it, and would
    reject genuine matches before it rejected that one. Its margin
    (0.36) was the one thing lower than both genuine matches' (0.44
    each), hence the 0.40 floor. Two other genuinely ambiguous/weak
    cases (near-identical sibling printings, or too weak overall)
    scored 0.02-0.06 margin, well clear of this bar either way. This
    is a much smaller, less-tested sample than the other layers'
    calibration, precisely because it's strictly additive: it only
    ever runs for files every other layer has already given up on, so
    it can recover a match but never override or worsen one - but
    given it already produced one real miss, treat any future one as
    likely too, not surprising."""
    internal_words = significant_words(internal_text) if internal_text else set()
    bytes_per_page = (file_size / page_count) if page_count else 0.0

    scored = []
    for item in xml_items:
        if not is_blank_form_product(item) or item.get('has_specific_sibling') or not item.get('is_pdf_product', True):
            continue
        base = score_candidate(item, internal_words, page_count, bytes_per_page, idf)
        bonus = 0.2 if has_verbatim_title(item, internal_text) else 0.0
        scored.append((base + bonus, item))
    scored.sort(key=lambda row: row[0], reverse=True)

    if not scored:
        return None, None
    top_score, top_item = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0

    if top_score >= BLANK_FORM_FALLBACK_MIN_SCORE and (top_score - runner_up) >= BLANK_FORM_FALLBACK_MIN_MARGIN:
        return top_item, f"Content Match -> blank-form fallback (score {top_score:.2f}, margin {top_score - runner_up:.2f})"
    return None, None


BACK_COVER_MAX_PAGES = 2  # how many trailing pages to OCR looking for the back-cover blurb
BACK_COVER_MIN_TEXT_CHARS = 100  # ignore OCR output too short to mean anything
# Recalibrated for the matching-blocks-based notes_similarity_score (see
# there for why exact-phrase reproduction is the right signal here). Real
# matches measured 0.778-0.975; a book whose blurb opens with the same
# boilerplate paragraph as its whole product series (e.g. "Adventure
# Gamebook" #18 vs. its other numbered siblings) can still pull a
# competitive-looking runner-up score purely from that shared opening, so
# margin alone is dropped from 0.25 to 0.20 while the minimum score is
# raised from 0.40 to 0.65 - comfortably below every real case measured,
# but well above what a shared-template-only sibling reaches once it's
# also carrying the specific plot description the genuine match keeps
# accumulating length from that its siblings don't share.
BACK_COVER_MATCH_MIN_SCORE = 0.65
BACK_COVER_MATCH_MIN_MARGIN = 0.20


def ocr_back_cover_text(reader, max_pages=BACK_COVER_MAX_PAGES):
    """OCRs the last few pages looking for the back-cover blurb. Many TSR
    products' LaunchBox Notes field reads like a near-verbatim transcript
    of the actual back-cover copy, so when the back cover is legible this
    gives a much stronger, more specific signal than generic front-matter
    text ever does - see notes_similarity_score(). Returns cleaned text,
    or "" if OCR isn't available or nothing usable was found."""
    if not OCR_AVAILABLE:
        return ""

    page_count = len(reader.pages)
    chunks = []
    for i in range(max(0, page_count - max_pages), page_count):
        try:
            for img in reader.pages[i].images:
                pil_image = img.image if img.image is not None else Image.open(io.BytesIO(img.data))
                if min(pil_image.size) < OCR_MIN_IMAGE_DIMENSION:
                    continue
                text = ocr_image_best(pil_image)
                if text:
                    chunks.append(text)
        except Exception:
            continue
    return clean_string(" ".join(chunks))


NOTES_MATCH_MIN_BLOCK_WORDS = 4  # ignore coincidental short runs (a few shared words means nothing)


def notes_similarity_score(text, item):
    """Looks for exact reproduced phrases (not just bag-of-words overlap,
    and not just one overall similarity ratio) between some text and a
    candidate's Notes field, then sums every non-trivial exact run found
    (>=NOTES_MATCH_MIN_BLOCK_WORDS words), normalized by how long the
    Notes field is. The Notes field for this database was hand-typed by
    the maintainer directly from the actual back-cover text of each PDF
    (or from a web listing quoting it) - so a real match reproduces
    whole sentences verbatim, not just a loose paraphrase, and even a
    single exact matching sentence reliably identifies the right item on
    its own. That also makes this naturally resistant to two problems a
    single whole-text similarity ratio had: (1) it isn't diluted when
    OCR happens to also pick up an unrelated page (an ad for a different
    book, say) mixed in with the real one, since irrelevant text simply
    contributes no matching runs rather than lowering the whole ratio;
    and (2) when a book's blurb opens with the same boilerplate
    paragraph as its whole product series, matching only that shared
    opening isn't enough on its own to look like a confident match - the
    genuine book keeps accumulating additional matched length from its
    own specific plot description that the other siblings don't share."""
    notes_words = item["notes"].split()
    if not notes_words:
        return 0.0
    matcher = difflib.SequenceMatcher(None, text.split(), notes_words, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks() if block.size >= NOTES_MATCH_MIN_BLOCK_WORDS)
    return matched / len(notes_words)


def back_cover_match(reader, xml_items, clean_pdf_name=None):
    """Scores every candidate against the OCR'd back cover and returns
    (item, method_string) if there's a clear winner, else (None, None).
    Intended as a last-resort layer - OCR is slow, so this should only be
    reached once cheaper methods have already failed."""
    back_text = ocr_back_cover_text(reader)
    if len(back_text) < BACK_COVER_MIN_TEXT_CHARS:
        return None, None

    # See the matching comment in content_based_match: the blank-form/
    # generic-sibling exclusions are exempted for a candidate whose title
    # exactly matches the current file's own (cleaned) filename, so a
    # correctly-named file can still confirm itself here instead of losing
    # to a near-identical-boilerplate sibling.
    scored = sorted(
        (
            (notes_similarity_score(back_text, item), item)
            for item in xml_items
            if item.get('is_pdf_product', True)
            and (item['clean_title'] == clean_pdf_name or (not is_blank_form_product(item) and not item.get('has_specific_sibling')))
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not scored:
        return None, None

    top_score, top_item = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0

    if top_score >= BACK_COVER_MATCH_MIN_SCORE and (top_score - runner_up) >= BACK_COVER_MATCH_MIN_MARGIN:
        return top_item, f"Content Match -> back cover OCR vs Notes field (score {top_score:.2f}, margin {top_score - runner_up:.2f})"

    return None, None


def identify_file_pass1(pdf_file, full_pdf_path, xml_items, image_library, idf_table,
                         fingerprint_cache=None):
    """First pass, run for every file before identify_file_pass2 touches
    any of them: the fingerprint cache, the Deities & Demigods override,
    and full-catalogue content-based matching (plus its blank-form
    fallback) - every layer that's both filename-independent and doesn't
    need OCR or a precomputed "unclaimed catalogue entries" pool. Returns
    (target_final_title_or_None, match_method, file_sha256_or_None). The
    hash is always returned (whether or not it hit the cache) so the
    caller can seed the cache - or hand it to identify_file_pass2 - without
    re-reading the file.

    Splitting the scan into two passes like this exists specifically so
    unclaimed_pool_match's pool (used by identify_file_pass2) can be
    computed from this pass's REAL, whole-batch results instead of
    guessing from on-disk filenames at scan start - a file sitting under a
    temporary or wrong name mid-batch used to make its own true catalogue
    entry look falsely "unclaimed" to every other file's process-of-
    elimination layer, which is exactly what let a single generic-scoring
    entry silently absorb dozens of unrelated files in one real run."""
    filename_no_ext, _ = os.path.splitext(pdf_file)

    clean_pdf_name = clean_string(filename_no_ext)
    # Kept short codes like "s2"/"b3"/"iq2" (genuinely discriminating and
    # common in these titles) by filtering on STOPWORDS rather than a
    # length floor - a plain length cutoff would have excluded those too.
    # Found the hard way: a title starting "TSRXXXX - An Original RPG
    # Module - IQ2 - Forbidden Ground" fed Layer 3 the tokens
    # ["tsrxxxx", "an"] as its first two - "an" is a substring of "and",
    # so it substring-matched dozens of unrelated "TSRXXXX ... and ..."
    # entries, and the file was misidentified via a near-random page-count
    # tiebreak among them instead of ever reaching its own actual entry.
    pdf_tokens = [t for t in clean_pdf_name.split() if len(t) > 1 and t not in STOPWORDS]

    target_final_title = None
    match_method = "None"

    # -----------------------------------------------------------------
    # LAYER 0: SHA256 FINGERPRINT CACHE
    # -----------------------------------------------------------------
    # A full-file content hash is the strongest possible signal - if it's
    # byte-identical to a file some earlier run (by this user, or anyone
    # else running this same script on their own copy of the same scan)
    # already confirmed with a high-confidence method, it IS that exact
    # file, full stop. Checked first and, on a hit, skips PDF
    # parsing/OCR entirely for a large speed win on repeat catalogues.
    #
    # The cache stores the already-*resolved* final display name rather
    # than a catalog identifier to re-look-up, because two XML entries
    # can share identical Title/Notes text (LaunchBox has no distinct
    # entry for some printings - see the Deities & Demigods handling in
    # Layer 1 below, which disambiguates those only via page count, not
    # title text) - so a title-based re-lookup could land on the wrong
    # twin. The resolved name recorded here is exactly the one that
    # earlier confirmed run already worked out, ambiguity and all.
    file_sha256 = hash_file_sha256(full_pdf_path)
    if file_sha256 and fingerprint_cache and file_sha256 in fingerprint_cache:
        cached_title = fingerprint_cache[file_sha256].get('title')
        if cached_title:
            return cached_title, "Fingerprint Cache -> Instant Match (SHA256)", file_sha256

    # Open the PDF exactly once and share it across every layer below -
    # each layer used to open its own PdfReader(full_pdf_path), re-parsing
    # the same file's cross-reference table (and re-opening the network
    # file handle, on a share) up to four times over for one file.
    try:
        file_size = os.path.getsize(full_pdf_path)
    except Exception:
        file_size = 0
    try:
        reader = PdfReader(full_pdf_path)
    except Exception:
        reader = None

    # Gather dynamic internal metadata signatures directly from the PDF,
    # independent of whatever the file happens to be named right now.
    page_count, internal_text = analyze_pdf_internals(reader) if reader else (0, "")
    # A bare "cthulhu" mention isn't enough - the revised printing still
    # thanks a contributor "for his help with the Cthulhu mythos" in the
    # acknowledgements even though the actual mythos section was cut.
    # Require either several mentions (an actual write-up, not a single
    # credit line) or a Melnibonean Mythos mention, since that section
    # was removed in the very same revision and has no such incidental
    # false positive.
    has_cthulhu_text = (
        internal_text.count("cthulhu") >= 4
        or "melnibon" in internal_text
        or "cthulhu" in pdf_tokens
    )

    # -----------------------------------------------------------------
    # LAYER 1: DEITIES & DEMIGODS HISTORICAL OVERRIDE LOCK
    # -----------------------------------------------------------------
    # Filename-triggered ONLY. A content-based trigger ("deities" and
    # "demigods" both appear somewhere in the text) was tried and
    # measured on the full catalogue: it hijacked dozens of unrelated
    # books - even a 5-page character-record-sheet PDF - because almost
    # any AD&D book's back-of-book ad page lists "Deities & Demigods"
    # among other core titles for sale. That single incidental mention
    # was enough to trigger this override. Filenames don't have that
    # failure mode, so this layer only fires when the file is already
    # named after this specific book.
    if "deities" in pdf_tokens and "demigods" in pdf_tokens:
        possible_xml_matches = [item for item in xml_items if "deities" in item['clean_title'] and "demigods" in item['clean_title']]

        if possible_xml_matches:
            # Primary calculation rule: 146 page container size maps to Cthulhu
            is_1st_printing = (page_count >= 140) or has_cthulhu_text

            cthulhu_options = [m for m in possible_xml_matches if "4th" not in m['title'].lower()]
            clean_options = [m for m in possible_xml_matches if "4th" in m['title'].lower()]

            if is_1st_printing and cthulhu_options:
                cthulhu_options.sort(key=lambda x: x['year'])
                best_xml = cthulhu_options[0]
                match_method = f"Omni-Verification -> Target 1st Printing Cthulhu ({page_count} pgs)"
            elif not is_1st_printing and clean_options:
                clean_options.sort(key=lambda x: x['year'])
                best_xml = clean_options[0]
                match_method = f"Omni-Verification -> Target 4th Printing Revised ({page_count} pgs)"
            else:
                possible_xml_matches.sort(key=lambda x: x['year'])
                best_xml = possible_xml_matches[0] if is_1st_printing else possible_xml_matches[-1]
                match_method = f"Omni-Verification -> Default Date Fallback ({page_count} pgs)"

            # LaunchBox catalogs the 1st printing and the 4th-printing
            # revision as two separate <Game> entries with the exact
            # same Title/Notes text - there is nothing in the XML to
            # tell them apart. The image library IS distinct ("Deities
            # & Demigods" vs "Deities & Demigods (4th Printing)"), so
            # pick between those two image variants directly using the
            # is_1st_printing signal instead of trusting best_xml's
            # (indistinguishable) title text.
            base_clean = strip_product_code(clean_string(best_xml['title']))
            exact_img = next((img for img in image_library if img['core_name'] == base_clean), None)
            variant_imgs = [
                img for img in image_library
                if img['core_name'] != base_clean and img['core_name'].startswith(base_clean)
            ]

            if is_1st_printing:
                chosen_img = exact_img or (variant_imgs[0] if variant_imgs else None)
            else:
                chosen_img = variant_imgs[0] if variant_imgs else exact_img

            target_final_title = chosen_img['original_name'] if chosen_img else resolve_display_name(best_xml, image_library)

    # -----------------------------------------------------------------
    # LAYER 2: CONTENT-BASED IDENTIFICATION (primary, filename-independent)
    # -----------------------------------------------------------------
    if not target_final_title:
        best_xml, method = content_based_match(internal_text, page_count, file_size, xml_items, idf_table, clean_pdf_name)
        if best_xml:
            target_final_title = resolve_display_name(best_xml, image_library)
            match_method = method

    # -----------------------------------------------------------------
    # LAYER 2-BF: BLANK-FORM FALLBACK (see blank_form_fallback_match) -
    # only reached once the main content match above has already given
    # up on every real book candidate; restricted to the small blank-
    # form-only pool so it can never compete with, or be stolen by, an
    # actual book's content match above.
    # -----------------------------------------------------------------
    if not target_final_title:
        best_xml, method = blank_form_fallback_match(internal_text, page_count, file_size, xml_items, idf_table)
        if best_xml:
            target_final_title = resolve_display_name(best_xml, image_library)
            match_method = method

    return target_final_title, match_method, file_sha256


def identify_file_pass2(pdf_file, full_pdf_path, file_sha256, xml_items, image_library, idf_table,
                         unclaimed_xml_items, cover_hash_index=None):
    """Second pass - only for files identify_file_pass1 couldn't resolve.
    Re-opens the PDF (an open PdfReader can't be carried across the two
    separate process-pool dispatches pass1 and pass2 run as, so this is a
    genuine second read - accepted as the cost of computing
    unclaimed_xml_items correctly instead of guessing it) and runs every
    remaining layer: process-of-elimination against unclaimed_xml_items
    (computed by the caller from pass1's whole-batch results - see
    identify_file_pass1), cover-image hashing, OCR, and the low-confidence
    legacy filename/substring fallbacks. file_sha256 is whatever
    identify_file_pass1 already computed for this file, passed through
    rather than re-hashed. Returns (target_final_title_or_None,
    match_method, file_sha256)."""
    filename_no_ext, _ = os.path.splitext(pdf_file)
    clean_pdf_name = clean_string(filename_no_ext)
    pdf_tokens = [t for t in clean_pdf_name.split() if len(t) > 1 and t not in STOPWORDS]

    target_final_title = None
    match_method = "None"

    try:
        file_size = os.path.getsize(full_pdf_path)
    except Exception:
        file_size = 0
    try:
        reader = PdfReader(full_pdf_path)
    except Exception:
        reader = None

    page_count, internal_text = analyze_pdf_internals(reader) if reader else (0, "")

    # -----------------------------------------------------------------
    # LAYER 2-EL: PROCESS OF ELIMINATION (see unclaimed_pool_match) -
    # native-text only, tried before any image-touching layer below on
    # purpose (see that function's docstring for why). Restricted to
    # whatever catalogue entries pass1 didn't already claim across the
    # whole batch, which is what makes a much lower score threshold safe
    # here than content_based_match uses against the full catalogue.
    # -----------------------------------------------------------------
    if not target_final_title:
        best_xml, method = unclaimed_pool_match(internal_text, page_count, file_size, unclaimed_xml_items, idf_table)
        if best_xml:
            target_final_title = resolve_display_name(best_xml, image_library)
            match_method = method

    # -----------------------------------------------------------------
    # LAYER 2A: COVER-IMAGE PERCEPTUAL HASH (tried before OCR - no text
    # recognition at all, just a resized-image fingerprint compared
    # against a table precomputed once at startup, so it's far cheaper
    # per file than either OCR layer below. Also succeeds on scans OCR
    # can't read at all (skewed, blurry, low-contrast covers).
    # -----------------------------------------------------------------
    if not target_final_title and cover_hash_index and reader:
        try:
            best_img, method = cover_image_match(reader, cover_hash_index)
            if best_img:
                target_final_title = best_img['original_name']
                match_method = method
        except Exception:
            pass

    # -----------------------------------------------------------------
    # LAYER 2B: BACK-COVER OCR vs NOTES FIELD (last resort before giving
    # up on content entirely). Deliberately tried only after Layer 2
    # fails: OCR is slow, so this expense is reserved for files nothing
    # cheaper could identify. When the back cover IS legible, this is
    # dramatically more discriminating than bag-of-words scoring, because
    # it's checking for near-verbatim reproduced text (many Notes entries
    # read like a transcript of the actual back-cover copy) rather than
    # shared genre vocabulary - which is exactly what made hub books like
    # "Monster Manual" or "Forgotten Realms Campaign Set" false-positive
    # attractors for the scoring in Layer 2.
    # -----------------------------------------------------------------
    if not target_final_title and OCR_AVAILABLE and reader:
        try:
            best_xml, method = back_cover_match(reader, xml_items, clean_pdf_name)
            if best_xml:
                target_final_title = resolve_display_name(best_xml, image_library)
                match_method = method
        except Exception:
            pass

    # -----------------------------------------------------------------
    # LAYER 2C: FRONT-PAGE OCR RETRY (tried only after the back cover has
    # already had its chance). Measured across a full catalogue run: the
    # back-cover check alone accounted for the large majority of OCR
    # recoveries, with the front-page pass adding comparatively little on
    # its own - so it's kept as a fallback (a legible front title page can
    # still succeed here even when the back cover can't), but demoted
    # behind the cheaper, higher-yield check above rather than run first.
    # -----------------------------------------------------------------
    if not target_final_title and OCR_AVAILABLE and reader:
        try:
            ocr_text = ocr_front_pages(reader)
            if ocr_text:
                enriched_text = clean_string(internal_text + " " + ocr_text)
                best_xml, method = content_based_match(enriched_text, page_count, file_size, xml_items, idf_table, clean_pdf_name)
                if best_xml:
                    target_final_title = resolve_display_name(best_xml, image_library)
                    match_method = method
        except Exception:
            pass

    # -----------------------------------------------------------------
    # LAYER 3: LEGACY FILENAME-TOKEN MATCHING (last resort only - the
    # current filename may itself be the product of a bad rename, so
    # this is never trusted over content-based evidence above).
    # -----------------------------------------------------------------
    if not target_final_title:
        possible_xml_matches = []
        for item in xml_items:
            if len(pdf_tokens) >= 2 and all(token in item['clean_title'] for token in pdf_tokens[:2]):
                possible_xml_matches.append(item)

        if len(possible_xml_matches) == 1:
            best_xml = possible_xml_matches[0]
            match_method = "Legacy Filename Match -> Unique Token (low confidence)"
            target_final_title = resolve_display_name(best_xml, image_library)

        elif len(possible_xml_matches) > 1:
            best_xml = None

            # If the file is *already* named exactly like one of the
            # plausible candidates, trust that self-consistency over an
            # arbitrary tie-break. Concretely fixed a real production
            # bug: excluding a generic 'TSRXXXX' placeholder from content
            # scoring (see is_specific_sibling) - correct, since it kept
            # that placeholder from stealing *other* books' identities -
            # had the side effect of also blocking it from re-confirming
            # its own, already-correctly-named file, which fell through
            # to this layer and, with three same-year 'TSRXXXX ... Fast
            # ...' candidates tied, got silently reassigned to a
            # different one by list order.
            for match in possible_xml_matches:
                if match['clean_title'] == clean_pdf_name:
                    best_xml = match
                    match_method = "Legacy Filename Match -> Exact Self-Match (low confidence)"
                    break

            if not best_xml:
                page_str = str(page_count)
                for match in possible_xml_matches:
                    if page_str in match['notes']:
                        best_xml = match
                        match_method = f"Legacy Filename Match -> Cross-Referenced XML Notes ({page_count} pages, low confidence)"
                        break

            if not best_xml:
                possible_xml_matches.sort(key=lambda x: x['year'])
                best_xml = possible_xml_matches[-1]
                match_method = "Legacy Filename Match -> Defaulted Variant Version (low confidence)"

            target_final_title = resolve_display_name(best_xml, image_library)

    # -----------------------------------------------------------------
    # LAYER 4: STANDARD IMAGE STRING LOOKUP
    # -----------------------------------------------------------------
    # A too-short cleaned filename (e.g. a bare "1" from a generic/
    # numbered file) is a substring of almost anything and would match
    # whatever happens to be first in the image library, essentially at
    # random - so substring matching is only attempted once the name
    # carries enough information to mean something.
    if not target_final_title and len(clean_pdf_name) >= 4:
        for img in image_library:
            if clean_pdf_name in img['clean_name'] or img['clean_name'] in clean_pdf_name:
                target_final_title = img['original_name']
                match_method = "Image Library Substring Match (low confidence)"
                break

    # -----------------------------------------------------------------
    # LAYER 5: AUTHORITATIVE PLATFORM DATABASE FALLBACK
    # -----------------------------------------------------------------
    if not target_final_title and len(clean_pdf_name) >= 4:
        for item in xml_items:
            if clean_pdf_name in item['clean_title'] or item['clean_title'] in clean_pdf_name:
                target_final_title = item['title'].replace(':', ' -')
                match_method = "XML Database Substring Match (low confidence)"
                break

    return target_final_title, match_method, file_sha256


def safe_pdf_filename(target_final_title):
    """Turns a resolved target_final_title into the exact filename
    execute_renames will actually use on disk: Windows-illegal characters
    stripped, then '.pdf' appended. target_final_title itself never carries
    the extension (every identify_file_pass1/identify_file_pass2 layer
    returns a bare title) - shared here so any check comparing a resolved
    target against a real on-disk filename (like the same-batch collision
    checks in run_matching_agent) compares apples to apples instead of
    silently never matching."""
    safe_title = "".join(c for c in target_final_title if c not in '<>:"/\\|?*').strip()
    return f"{safe_title}.pdf"


def execute_renames(plans, output_directory):
    """Two-phase collision-safe execution: every source that needs to move
    is first shifted to a temporary name (Phase 1), then every temp file is
    assigned its real final name, with genuine collisions getting a
    numbered suffix (Phase 2) - see the comments below for why it's split
    this way. If cancelled (Ctrl+C) partway through, any file currently
    sitting at a temporary name is moved back to where it started, so the
    catalogue always ends up either fully done or exactly as it was -
    never with orphaned "__dnd_renamer_tmp_N__.pdf" files left behind.
    Returns (results, cancelled)."""
    os.makedirs(output_directory, exist_ok=True)

    reserved_names = set()
    results = []  # (pdf_file, match_method, final_filename_or_None, already_correct)
    move_list = []  # (pdf_file, temp_path, wanted_name, match_method)
    pending_temp_moves = {}  # temp_path -> original_full_path, for rollback on cancel
    pending_final = {}  # temp_path -> (pdf_file, match_method, new_filename), for the Phase 2
                         # rename currently in flight - lets a KeyboardInterrupt landing right
                         # after os.rename() succeeds but before this loop's own bookkeeping
                         # still be counted as finished below, instead of vanishing from every
                         # tally (the file itself is already safely at its final name either way)
    temp_counter = 0

    try:
        # -------------------------------------------------------------
        # PHASE 1: FREE UP EVERY SOURCE THAT NEEDS TO MOVE
        # -------------------------------------------------------------
        # Move every file whose computed name differs from its current name
        # to a temporary placeholder first. That clears its old name out of
        # the way before any target name is checked, so a rename can never
        # be blocked by a collision with a file that's itself about to
        # vacate that name later in this same run.
        for pdf_file, full_pdf_path, target_final_title, match_method in plans:
            _check_control()
            if not target_final_title:
                results.append((pdf_file, match_method, None, False))
                continue

            wanted_name = safe_pdf_filename(target_final_title)

            if wanted_name == pdf_file:
                reserved_names.add(wanted_name)
                results.append((pdf_file, match_method, wanted_name, True))
                continue

            temp_counter += 1
            temp_path = os.path.join(output_directory, f"__dnd_renamer_tmp_{temp_counter}__.pdf")
            # Record the intended mapping *before* attempting the move, not
            # after - a Ctrl+C can land exactly as os.rename() returns, and
            # if that happens between the call and this bookkeeping line,
            # the file that just got moved would otherwise never be found
            # by the rollback below and would be orphaned as a
            # "__dnd_renamer_tmp_N__.pdf" file forever.
            pending_temp_moves[temp_path] = full_pdf_path
            try:
                os.rename(full_pdf_path, temp_path)
                move_list.append((pdf_file, temp_path, wanted_name, match_method))
            except Exception as e:
                del pending_temp_moves[temp_path]  # the move never happened - nothing to roll back
                results.append((pdf_file, f"{match_method} [FAILED]", None, False))
                print(f"❌ Failed to stage rename for '{pdf_file}': {e}")

        # -------------------------------------------------------------
        # PHASE 2: ASSIGN FINAL NAMES, RESOLVING GENUINE COLLISIONS
        # -------------------------------------------------------------
        # Every source that needed to move has already vacated the target
        # namespace, so any collision found here is a real one - two
        # different books whose computed titles happen to coincide - and
        # still gets the numbered " (2)", " (3)", ... treatment rather than
        # overwriting.
        for pdf_file, temp_path, wanted_name, match_method in move_list:
            _check_control()
            new_filename = wanted_name
            new_path = os.path.join(output_directory, new_filename)
            base_name = wanted_name[:-len(".pdf")] if wanted_name.lower().endswith(".pdf") else wanted_name

            counter = 1
            while new_filename in reserved_names or os.path.exists(new_path):
                counter += 1
                new_filename = f"{base_name} ({counter}).pdf"
                new_path = os.path.join(output_directory, new_filename)

            pending_final[temp_path] = (pdf_file, match_method, new_filename)
            try:
                os.rename(temp_path, new_path)
                reserved_names.add(new_filename)
                results.append((pdf_file, match_method, new_filename, False))
                del pending_temp_moves[temp_path]
                del pending_final[temp_path]
            except Exception as e:
                del pending_final[temp_path]
                print(f"❌ Failed to rename '{pdf_file}' -> '{new_filename}': {e}")
                results.append((pdf_file, f"{match_method} [FAILED]", None, False))

    except KeyboardInterrupt:
        print("\n\n⚠️  Cancelled. Restoring any files that were mid-rename...")
        restored = 0
        for temp_path, original_path in pending_temp_moves.items():
            if not os.path.exists(temp_path):
                # The bookkeeping entry was written just before the move was
                # attempted (see above) - if Ctrl+C landed before the move
                # itself actually happened, the temp file was never created,
                # so the original is still sitting right where it started.
                # But if this was a Phase 2 (final-name) move, it may instead
                # mean the rename to its final name already succeeded and only
                # this loop's own results/reserved_names bookkeeping got cut
                # off - count it as finished rather than losing track of it.
                final = pending_final.get(temp_path)
                if final:
                    pdf_file, match_method, new_filename = final
                    reserved_names.add(new_filename)
                    results.append((pdf_file, match_method, new_filename, False))
                continue
            try:
                os.rename(temp_path, original_path)
                restored += 1
            except Exception as e:
                print(f"❌ Could not restore '{temp_path}' back to '{original_path}': {e}")

        print(f"\n{len(results)} file(s) were already finished before you cancelled.")
        if restored:
            print(f"{restored} file(s) that were mid-rename were restored to their original name.")
        remaining = len(plans) - len(results) - restored
        if remaining > 0:
            print(f"{remaining} file(s) hadn't been touched yet.")
        print("Nothing was left in a half-renamed state. Re-run the script to pick up where you left off.")
        return results, True

    return results, False


def is_network_path(path):
    """Best-effort check for whether a path lives on a network drive or
    UNC share - used only to warn the user this run will be slower, never
    to change behavior. Measured directly: a plain full-file read (the
    SHA256 hashing pass, or any file resolved by cheap native-text
    matching) was ~66x slower over a network share than local storage in
    testing, while OCR-bound files (Tesseract's own recognition time
    dwarfing the read) showed almost no difference - so this matters for
    most files but not the slowest ones. Failure/uncertainty defaults to
    False (no warning) rather than a possibly-wrong claim."""
    try:
        if path.startswith('\\\\') or path.startswith('//'):
            return True
        if sys.platform == "win32":
            import ctypes
            drive = os.path.splitdrive(os.path.abspath(path))[0]
            if drive:
                DRIVE_REMOTE = 4
                return ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\") == DRIVE_REMOTE
    except Exception:
        pass
    return False


SCAN_WORKERS = min(os.cpu_count() or 4, 8)

_worker_context = {}


def _ignore_sigint():
    # On Windows (and POSIX), SIGINT is delivered to every process in the
    # console's process group, not just the parent - so without this,
    # every idle worker independently receives the interrupt too and each
    # prints its own "Process SpawnProcess-N: Traceback ...
    # KeyboardInterrupt" noise, on top of the real one. Only the main
    # process is meant to react to Ctrl+C (see the explicit
    # executor.shutdown(cancel_futures=True) call in run_matching_agent /
    # review_unmatched_interactively); workers should just keep working
    # until told to stop.
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _init_scan_worker_pass1(xml_path, image_dir, cache_path):
    """Runs once per worker process, not once per file. ProcessPoolExecutor
    spawns each worker as a fresh Python process that re-imports this
    module from scratch - none of the main process's globals (including
    whatever configure_paths() set) carry over automatically - so this
    loads everything identify_file_pass1 needs exactly once per worker and
    stashes it in a module-level dict the worker's own calls can reach."""
    _ignore_sigint()
    xml_items = load_launchbox_db(xml_path)
    mark_generic_placeholders_with_siblings(xml_items)
    _worker_context['xml_items'] = xml_items
    _worker_context['image_library'] = load_image_library(image_dir)
    _worker_context['idf_table'] = build_idf_table(xml_items)
    _worker_context['fingerprint_cache'] = load_fingerprint_cache(cache_path)


def _scan_worker_pass1(pdf_file, full_pdf_path):
    """Top-level, picklable per-file entry point for a pass-1 scan worker -
    pulls the catalogue data _init_scan_worker_pass1 already loaded once,
    rather than re-loading (or re-pickling across the process boundary)
    it for every single file."""
    ctx = _worker_context
    return identify_file_pass1(
        pdf_file, full_pdf_path,
        ctx['xml_items'], ctx['image_library'], ctx['idf_table'], ctx['fingerprint_cache'],
    )


def _init_scan_worker_pass2(xml_path, image_dir, cache_path, unclaimed_xml_items, cover_hash_index):
    """Like _init_scan_worker_pass1, but for pass-2 workers: takes
    unclaimed_xml_items as computed by the caller from pass 1's actual,
    whole-batch results (see identify_file_pass1 and run_matching_agent)
    instead of guessing it from on-disk filenames itself. cover_hash_index
    is likewise built once by the caller (see build_cover_hash_index) and
    handed to every worker rather than each one re-hashing the whole
    LaunchBox image set itself. fingerprint_cache isn't loaded here -
    every file reaching pass 2 already missed it in pass 1, and nothing
    in pass 2 checks it again."""
    _ignore_sigint()
    xml_items = load_launchbox_db(xml_path)
    mark_generic_placeholders_with_siblings(xml_items)
    _worker_context['xml_items'] = xml_items
    _worker_context['image_library'] = load_image_library(image_dir)
    _worker_context['idf_table'] = build_idf_table(xml_items)
    _worker_context['unclaimed_xml_items'] = unclaimed_xml_items
    _worker_context['cover_hash_index'] = cover_hash_index


def _scan_worker_pass2(pdf_file, full_pdf_path, file_sha256):
    """Top-level, picklable per-file entry point for a pass-2 scan worker.
    file_sha256 is whatever pass 1 already computed for this file (see
    identify_file_pass1), passed through rather than re-hashed."""
    ctx = _worker_context
    return identify_file_pass2(
        pdf_file, full_pdf_path, file_sha256,
        ctx['xml_items'], ctx['image_library'], ctx['idf_table'],
        ctx['unclaimed_xml_items'], ctx['cover_hash_index'],
    )


def _guess_worker(pdf_file, full_pdf_path):
    """Top-level, picklable per-file entry point for computing a best-
    guess suggestion (see best_guess_for_unmatched) in a worker process -
    same rationale as _scan_worker_pass1: pulls catalogue data already
    loaded once by _init_scan_worker_pass1 instead of reloading it per
    file. Returns (pdf_file, full_pdf_path, guess_or_None) so the caller
    can match results back up after they complete out of submission
    order."""
    ctx = _worker_context
    guess = best_guess_for_unmatched(full_pdf_path, ctx['xml_items'], ctx['image_library'], ctx['idf_table'])
    return pdf_file, full_pdf_path, guess


def _drop_low_confidence_tag(match_method):
    """Strips the '(low confidence)' marker (and its ', low confidence'
    form when it shares a parenthetical with other detail, e.g. a page
    count) from a match_method string - used only once a human has
    confirmed the match, so the caching/scan-index logic downstream (which
    keys off this exact substring's absence) treats it like any other
    trusted match."""
    cleaned = re.sub(r', low confidence(?=\))', '', match_method)
    cleaned = re.sub(r'\s*\(low confidence\)', '', cleaned)
    return cleaned.strip()


def review_low_confidence_matches(results, output_directory, image_library, plan_targets):
    """Post-scan step: for every file this run renamed based on a low-
    confidence layer (identify_file_pass2's legacy filename/substring
    matching - the last-resort layers, tagged "(low confidence)" in their
    match_method), offers to confirm each one by hand - the same visual
    box-art-vs-front-page comparison _confirm_suggestion shows for a
    never-yet-applied guess, since this is the identical "is this rename
    right?" question, just for a file that's already been moved. Those
    matches are deliberately excluded from the fingerprint cache and scan
    index while unconfirmed (see identify_file_pass1's caching-exclusion
    note) - caching an unverified guess would make it permanent for every
    future encounter of the identical file. A human confirming one is a
    stronger signal than any automated layer, so a confirmed entry has
    its match_method rewritten to drop the "(low confidence)" tag, which
    lets it flow into the exact same caching/scan-index logic a high-
    confidence automated match already gets, further down in
    run_matching_agent. Returns a new results list with confirmed
    entries updated in place; unconfirmed/declined ones are returned
    unchanged."""
    candidates = [
        i for i, (pdf_file, match_method, new_filename, already_correct) in enumerate(results)
        if new_filename and "(low confidence)" in match_method
    ]
    if not candidates:
        return results

    print(f"\n{len(candidates)} file(s) this run were renamed based on a low-confidence guess.")
    if _confirm_yesno("Review and confirm which are correct, so they're trusted next time?") != "yes":
        print("Skipping review.")
        return results

    results = list(results)
    confirmed = 0
    for i in candidates:
        _check_control()
        pdf_file, match_method, new_filename, already_correct = results[i]
        on_disk_title = new_filename[:-4] if new_filename.lower().endswith(".pdf") else new_filename
        display_name = plan_targets.get(pdf_file, on_disk_title)
        box_art_path = _find_box_art_path(display_name, image_library)
        preview_image = _extract_first_page_image(os.path.join(output_directory, new_filename))
        decision = _confirm_suggestion(pdf_file, on_disk_title, f"[{match_method}]", box_art_path, preview_image)
        if decision == "stop":
            print("Stopping review - anything already confirmed stays trusted.")
            break
        if decision == "yes":
            results[i] = (pdf_file, f"Human-Confirmed ({_drop_low_confidence_tag(match_method)})", new_filename, already_correct)
            confirmed += 1

    if confirmed:
        print(f"\nConfirmed {confirmed} match(es) - they'll be cached and trusted next time.")
    return results


def review_unmatched_interactively(unmatched, output_directory, fingerprint_cache, scan_index, renaming_in_place):
    """Post-scan step: for every file the automated pass couldn't
    confidently identify, computes the single best guess anyway (see
    best_guess_for_unmatched) and asks the user to confirm it one at a
    time, rather than silently leaving all of them untouched. A 'y'
    renames the file and seeds the fingerprint cache - a human
    confirming a suggestion is a stronger signal than any automated
    threshold, so it's trusted the same way a high-confidence automated
    match is. Anything else (including Ctrl+C) skips that file; nothing
    already confirmed is undone by skipping or cancelling the rest.

    When renaming in place, a confirmed file also gets a scan_index
    entry, the same as any other confirmed match - see
    run_matching_agent's own scan_index update for why (mirrored here
    rather than shared, since this runs after that one and on a
    different, review-specific set of files)."""
    if not unmatched:
        return 0

    print(f"\n{len(unmatched)} file(s) couldn't be confidently matched.")
    if _confirm_yesno("Review best-guess suggestions for them one at a time?") != "yes":
        print("Skipping review.")
        return 0

    print("\nComputing suggestions (this re-uses the same OCR/content analysis as the main scan)...")
    executor = concurrent.futures.ProcessPoolExecutor(
        max_workers=SCAN_WORKERS,
        initializer=_init_scan_worker_pass1,
        initargs=(XML_PATH, IMAGE_DIRECTORY, CACHE_PATH),
    )
    guesses = {}  # pdf_file -> (full_pdf_path, guess_or_None)
    try:
        futures = {
            executor.submit(_guess_worker, pdf_file, full_pdf_path): (pdf_file, full_pdf_path)
            for pdf_file, full_pdf_path in unmatched
        }
        completed = 0
        _report_progress("Computing suggestions", 0, len(unmatched))
        for future in concurrent.futures.as_completed(futures):
            _check_control()
            pdf_file, full_pdf_path = futures[future]
            try:
                _, _, guess = future.result()
            except Exception as e:
                guess = None
                print(f"\n⚠️  Couldn't compute a suggestion for '{pdf_file}': {e}")
            guesses[pdf_file] = (full_pdf_path, guess)
            completed += 1
            print(f"  Computed ({completed}/{len(unmatched)}): {pdf_file}")
            _report_progress("Computing suggestions", completed, len(unmatched))
        executor.shutdown(wait=True)
    except KeyboardInterrupt:
        executor.shutdown(wait=False, cancel_futures=True)
        print("\n\nCancelled computing suggestions - nothing was renamed in this step.")
        return 0

    confirmed = 0
    scan_index_changed = False
    with_guess = [(pdf_file, fp, g) for pdf_file, (fp, g) in guesses.items() if g]
    print(f"Have a suggestion for {len(with_guess)} of {len(unmatched)} files.\n")

    for pdf_file, full_pdf_path, (suggested_title, score, source, box_art_path) in with_guess:
        _check_control()
        safe_title = "".join(c for c in suggested_title if c not in '<>:"/\\|?*').strip()
        preview_image = _extract_first_page_image(full_pdf_path)
        detail = f"(guess from {source}, score {score:.2f})"
        decision = _confirm_suggestion(pdf_file, safe_title, detail, box_art_path, preview_image)
        if decision == "stop":
            print("\nStopping review - anything already confirmed stays renamed.")
            break
        if decision != "yes":
            continue

        new_filename = f"{safe_title}.pdf"
        new_path = os.path.join(output_directory, new_filename)
        counter = 1
        while os.path.exists(new_path):
            counter += 1
            new_filename = f"{safe_title} ({counter}).pdf"
            new_path = os.path.join(output_directory, new_filename)
        try:
            os.rename(full_pdf_path, new_path)
        except Exception as e:
            print(f"  Failed to rename: {e}")
            continue

        confirmed += 1
        file_sha256 = hash_file_sha256(new_path)
        if file_sha256:
            fingerprint_cache[file_sha256] = {"title": suggested_title, "matched_via": f"Human-Confirmed Suggestion ({source})"}

        if renaming_in_place and file_sha256:
            try:
                stat = os.stat(new_path)
            except OSError:
                stat = None
            if stat is not None:
                entry = {"size": stat.st_size, "mtime": stat.st_mtime, "sha256": file_sha256}
                if scan_index.get(new_filename) != entry:
                    scan_index[new_filename] = entry
                    scan_index_changed = True
                if new_filename != pdf_file and scan_index.pop(pdf_file, None) is not None:
                    scan_index_changed = True

    if confirmed:
        save_fingerprint_cache(CACHE_PATH, fingerprint_cache)
        print(f"\nConfirmed {confirmed} rename(s) and updated the fingerprint cache.")
    if scan_index_changed:
        save_scan_index(SCAN_INDEX_PATH, scan_index)
    return confirmed


SCAN_TASK_TIMEOUT = 240  # seconds - generous: the slowest legitimate file
# measured in real testing was well under a minute even with the full OCR
# cascade. Confirmed on real data: a malformed embedded image can make
# pypdf/Pillow's decoder hang indefinitely (a specific PDF, 40MB, otherwise
# unremarkable, hung 90+ seconds with zero progress on just listing one
# page's images - not an OCR cost, a stuck decode). CPython's
# ProcessPoolExecutor has no supported way to kill one specific stuck
# worker without the whole pool being marked broken (any unexpected
# worker death - crash or external kill alike - trips that), so recovering
# from a hang means tearing down and rebuilding the whole pool for
# whatever's left, with the offending file marked skipped rather than
# retried into the same hang.


def _run_parallel_scan(file_pairs, dispatch, init_fn, init_args, progress_verb="Scanning"):
    """Runs `dispatch(executor, pdf_file, full_pdf_path)` across a process
    pool for every (pdf_file, full_pdf_path) pair, rebuilding the pool
    whenever a single task exceeds SCAN_TASK_TIMEOUT so one pathological
    PDF can never hang the entire scan. `init_fn`/`init_args` set up each
    fresh worker process (see _init_scan_worker_pass1/_init_scan_worker_
    pass2) - parametrized so this same loop drives both scan passes (see
    run_matching_agent) rather than duplicating it. Keeps at most
    SCAN_WORKERS tasks in flight at once and tracks each one's OWN
    dispatch time individually, rather than one shared clock for the whole
    batch - found the hard way: with more files queued than workers, most
    tasks sit waiting behind others before any worker touches them, and
    timing from batch-submission then flags those as "stuck" purely for
    still being in line - a real production bug that wrongly skipped
    well-tested, sub-second files (including one of the regression-set
    files itself) in a 247-file batch on an 8-worker pool. Returns
    (plans, file_hashes, cache_hits). Raises KeyboardInterrupt after
    tearing down whatever pool is currently active - nothing is left
    running in the background either way."""
    total_files = len(file_pairs)
    queue = list(file_pairs)
    plans = []
    file_hashes = {}
    cache_hits = 0
    completed = 0
    _report_progress(progress_verb, 0, total_files)

    def new_executor():
        return concurrent.futures.ProcessPoolExecutor(
            max_workers=SCAN_WORKERS,
            initializer=init_fn,
            initargs=init_args,
        )

    def kill_pool(executor):
        executor.shutdown(wait=False, cancel_futures=True)
        for proc in list((getattr(executor, "_processes", None) or {}).values()):
            try:
                proc.kill()
            except Exception:
                pass

    executor = new_executor()
    futures = {}  # future -> (pdf_file, full_pdf_path, submit_time)

    def refill():
        while queue and len(futures) < SCAN_WORKERS:
            pdf_file, full_pdf_path = queue.pop(0)
            future = dispatch(executor, pdf_file, full_pdf_path)
            futures[future] = (pdf_file, full_pdf_path, time.time())

    try:
        refill()
        while futures:
            _check_control()
            done, _ = concurrent.futures.wait(
                set(futures), timeout=5, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                pdf_file, full_pdf_path, _ = futures.pop(future)
                try:
                    target_final_title, match_method, file_sha256 = future.result()
                except Exception as e:
                    target_final_title, match_method, file_sha256 = None, f"Error: {e}", None

                plans.append((pdf_file, full_pdf_path, target_final_title, match_method))
                file_hashes[pdf_file] = file_sha256
                if match_method and match_method.startswith("Fingerprint Cache"):
                    cache_hits += 1

                completed += 1
                print(f"  {progress_verb} ({completed}/{total_files}): {pdf_file}")
                _report_progress(progress_verb, completed, total_files)

            now = time.time()
            stale = {f for f, (_, _, submit_time) in futures.items() if now - submit_time > SCAN_TASK_TIMEOUT}
            if stale:
                # CPython gives no way to kill just the stuck worker(s)
                # without the whole pool being marked broken (any
                # unexpected worker death - crash or external kill
                # alike - trips that), so the whole pool has to come
                # down. Anything else still in flight is genuinely
                # healthy, just sharing the pool with the stuck one -
                # it goes back on the front of the queue for a full,
                # fresh timeout window in the next pool, rather than
                # being blamed for its neighbor's hang.
                for future in list(futures):
                    if future in stale:
                        continue
                    pdf_file, full_pdf_path, _ = futures.pop(future)
                    queue.insert(0, (pdf_file, full_pdf_path))
                kill_pool(executor)
                for future in stale:
                    pdf_file, full_pdf_path, _ = futures.pop(future)
                    plans.append((pdf_file, full_pdf_path, None,
                                  f"Skipped -> exceeded {SCAN_TASK_TIMEOUT}s (likely a malformed embedded image hanging the PDF decoder)"))
                    file_hashes[pdf_file] = None
                    completed += 1
                    print(f"⚠️  '{pdf_file}' didn't finish within {SCAN_TASK_TIMEOUT}s - skipped, rest of the scan continues.")
                    _report_progress(progress_verb, completed, total_files)
                executor = new_executor()

            # Re-checked here, not just at the top of the loop - without
            # this, a pause requested while this iteration was already
            # past its check still fell through to one more refill(),
            # dispatching a fresh batch of up to SCAN_WORKERS new files
            # before pausing actually took visible effect.
            _check_control()
            refill()
    except KeyboardInterrupt:
        kill_pool(executor)
        raise

    kill_pool(executor)
    return plans, file_hashes, cache_hits


def run_matching_agent():
    print("==================================================")
    print("  LaunchBox D&D Omni-Method Renamer Agent v22.0   ")
    print("==================================================\n")
    _report_progress("Loading catalog", 0, 0)

    image_library = load_image_library(IMAGE_DIRECTORY)
    xml_items = load_launchbox_db(XML_PATH)
    mark_generic_placeholders_with_siblings(xml_items)
    idf_table = build_idf_table(xml_items)
    fingerprint_cache = load_fingerprint_cache(CACHE_PATH)

    print(f"Loaded {len(image_library)} Image names and {len(xml_items)} XML entries.")
    if fingerprint_cache:
        print(f"Loaded fingerprint cache: {len(fingerprint_cache)} previously-confirmed PDFs.")
    if OCR_AVAILABLE:
        print("OCR fallback: ENABLED (used automatically for scanned PDFs with no text layer).")
    else:
        print("OCR fallback: disabled - Tesseract not found. Image-only scans will be left unmatched.")
        print("  Install it (e.g. 'winget install UB-Mannheim.TesseractOCR' on Windows) and")
        print("  'pip install pytesseract Pillow' to enable OCR recovery for those files.")

    if not os.path.exists(PDF_DIRECTORY):
        print(f"❌ Error: PDF folder not found at '{PDF_DIRECTORY}'")
        return

    if is_network_path(PDF_DIRECTORY):
        print("⚠️  This folder looks like it's on a network drive/share. Most files")
        print("   will scan noticeably slower than they would from a local drive -")
        print("   copying the PDFs to local storage first will speed this up. (Files")
        print("   that need OCR won't see much difference either way, since Tesseract's")
        print("   own recognition time dominates those regardless of storage location.)")

    pdf_files = [f for f in os.listdir(PDF_DIRECTORY) if f.lower().endswith('.pdf')]
    total_files = len(pdf_files)

    # The scan index (see load_scan_index) only means anything for files
    # that stay put after being confirmed - if the output folder is
    # somewhere else, a confirmed file is moved out of PDF_DIRECTORY
    # entirely and simply won't be listed here again next time anyway.
    renaming_in_place = (
        os.path.normcase(os.path.normpath(PDF_DIRECTORY))
        == os.path.normcase(os.path.normpath(OUTPUT_DIRECTORY))
    )
    scan_index = load_scan_index(SCAN_INDEX_PATH) if renaming_in_place else {}

    skip_plans, to_scan_files = [], pdf_files
    if scan_index:
        skip_plans, to_scan_files = partition_for_incremental_scan(
            pdf_files, PDF_DIRECTORY, scan_index, fingerprint_cache
        )
        if skip_plans and prompt_scan_mode(len(skip_plans), total_files) == "full":
            skip_plans, to_scan_files = [], pdf_files

    print(f"Scanning {len(to_scan_files)} PDFs using ALL investigative methods ({SCAN_WORKERS} workers in parallel)...")
    if skip_plans:
        print(f"({len(skip_plans)} unchanged, previously-confirmed file(s) skipped - incremental scan.)")
    print("(Press Ctrl+C at any time to cancel. Nothing is renamed until the scan below finishes,")
    print(" and any rename already in progress when cancelled is safely undone.)\n")

    # Identification is decided for every file first, and nothing is
    # renamed until that's done (see execute_renames' two-phase design).
    # Doing the rename inline, file by file, let a file's target-name
    # collision check race against another file that was *also* about to
    # move away later in the very same run - e.g. two prints of "Deities &
    # Demigods" would settle into the right names only after being run
    # twice.
    #
    # That scan is the slow part on a large catalogue (opening and reading
    # pages out of every PDF, often over a network share, and running
    # Tesseract OCR on scanned files), and each file is independent of
    # every other - so it's spread across a process pool rather than
    # done one file at a time. Deliberately NOT a `with` block: that
    # would call ProcessPoolExecutor's default __exit__, which blocks
    # until every already-submitted task finishes even on a
    # KeyboardInterrupt - silently breaking the "cancel anytime"
    # guarantee above for whatever's still queued. Shutting down
    # explicitly in each branch below keeps cancellation immediate.
    try:
        # PASS 1: fingerprint cache + full-catalogue content match for
        # every file that needs scanning - no OCR, no process-of-
        # elimination yet (see identify_file_pass1).
        file_pairs = [(pdf_file, os.path.join(PDF_DIRECTORY, pdf_file)) for pdf_file in to_scan_files]
        pass1_plans, file_hashes, cache_hits = _run_parallel_scan(
            file_pairs,
            dispatch=lambda ex, f, p: ex.submit(_scan_worker_pass1, f, p),
            init_fn=_init_scan_worker_pass1,
            init_args=(XML_PATH, IMAGE_DIRECTORY, CACHE_PATH),
        )

        # The "unclaimed catalogue entries" pool for pass 2's process-of-
        # elimination layer (see unclaimed_pool_match), computed from pass
        # 1's REAL, whole-batch results plus this run's incremental-scan
        # skips - not guessed from on-disk filenames at scan start. That
        # guess was the root cause of a real incident: a file scanned
        # under a temporary or wrong name made its own true catalogue
        # entry look falsely "unclaimed" to every other file's process-
        # of-elimination layer, letting one generic-scoring entry silently
        # absorb dozens of unrelated files in a single run.
        # resolve_display_name (used below) never returns a '.pdf'
        # extension, and neither does target_final_title (see
        # safe_pdf_filename) - so a skipped file's own filename has its
        # extension stripped too, to compare like with like.
        claimed_names = {target for _, _, target, _ in pass1_plans if target}
        claimed_names.update(os.path.splitext(pdf_file)[0] for pdf_file, *_r in skip_plans)
        unclaimed_xml_items = [
            item for item in xml_items
            if resolve_display_name(item, image_library) not in claimed_names
        ]

        still_unresolved = [
            (pdf_file, full_pdf_path) for pdf_file, full_pdf_path, target, _ in pass1_plans if not target
        ]
        pass2_by_name = {}
        if still_unresolved:
            print(f"Pass 1 complete. Running deeper identification (process of elimination, "
                  f"cover art, OCR) on {len(still_unresolved)} still-unmatched file(s)...\n")
            # Built once here (not per worker) and only when pass 2 is
            # actually needed - every LaunchBox box-art image lives on the
            # local drive, so this costs a few seconds regardless of how
            # many PDFs are being scanned, not per-file network time.
            cover_hash_index = build_cover_hash_index(image_library)
            pass2_plans, pass2_hashes, _ = _run_parallel_scan(
                still_unresolved,
                dispatch=lambda ex, f, p: ex.submit(_scan_worker_pass2, f, p, file_hashes[f]),
                init_fn=_init_scan_worker_pass2,
                init_args=(XML_PATH, IMAGE_DIRECTORY, CACHE_PATH, unclaimed_xml_items, cover_hash_index),
                progress_verb="Deeper scan",
            )
            file_hashes.update(pass2_hashes)

            # None of pass 2's layers (process-of-elimination, OCR, the
            # low-confidence legacy filename/substring fallbacks) can see
            # another still-unresolved file in the SAME batch independently
            # landing on the identical catalogue entry - unclaimed_xml_items
            # is a snapshot taken once before pass 2 starts, not updated
            # live as results come in (see unclaimed_pool_match's own
            # docstring). A real incident showed this isn't just
            # theoretical: with enough files sharing a shrunken pool, more
            # than one can score confidently against the same entry, and
            # the higher-scoring guess isn't reliably the correct one - so
            # rather than trust a score comparison to pick a "winner",
            # every entry claimed by more than one file here is rejected
            # outright and left for manual review. A wrong rename is worse
            # than no rename; an unresolved tie is a human's call, not a
            # coin flip.
            target_counts = Counter(target for _, _, target, _ in pass2_plans if target)
            pass2_plans = [
                (pdf_file, full_pdf_path, None,
                 f"Skipped -> {target_counts[target]} files in this batch all matched "
                 f"'{target}' - ambiguous, left for manual review")
                if target and target_counts[target] > 1
                else (pdf_file, full_pdf_path, target, match_method)
                for pdf_file, full_pdf_path, target, match_method in pass2_plans
            ]

            # A pass-2 layer doesn't need another pass-2 file competing for
            # the exact same guess to still be wrong - it can just as easily
            # land on the name of a DIFFERENT file that isn't going
            # anywhere this run (already correctly named, still unmatched
            # after pass 1, or itself just rejected as ambiguous above).
            # execute_renames' own collision handling would safely give
            # that a "(2)" suffix rather than overwrite anything - but per
            # the same reasoning as the check above, a pass-2 guess landing
            # on an already-spoken-for name is itself the signal of a wrong
            # guess, not a coincidence worth a numbered suffix.
            # target_final_title never carries the '.pdf' extension (see
            # safe_pdf_filename) - comparing it directly against pdf_file
            # would silently never match even a genuine self-match, so
            # every comparison below goes through safe_pdf_filename first.
            stable_names = {
                pdf_file for pdf_file, _, target, _ in pass1_plans
                if not target or safe_pdf_filename(target) == pdf_file
            }
            stable_names.update(pdf_file for pdf_file, *_r in skip_plans)
            stable_names.update(
                pdf_file for pdf_file, _, target, _ in pass2_plans
                if not target or safe_pdf_filename(target) == pdf_file
            )
            pass2_plans = [
                (pdf_file, full_pdf_path, None,
                 f"Skipped -> matches the current name of a different file staying in "
                 f"place ('{target}') - ambiguous, left for manual review")
                if target and safe_pdf_filename(target) != pdf_file and safe_pdf_filename(target) in stable_names
                else (pdf_file, full_pdf_path, target, match_method)
                for pdf_file, full_pdf_path, target, match_method in pass2_plans
            ]

            pass2_by_name = {p[0]: p for p in pass2_plans}
    except KeyboardInterrupt:
        # _run_parallel_scan has already torn down its pool by the time
        # this propagates, so there's nothing left running in the
        # background - this returns to the user immediately.
        print("\n\n⚠️  Cancelled during the scan - nothing was renamed.")
        return

    scanned_plans = [pass2_by_name.get(plan[0], plan) if not plan[2] else plan for plan in pass1_plans]

    # A skipped file's SHA256 came from the scan index, not this run's
    # scan, but the results/scan-index bookkeeping below reads every
    # file's hash from this same dict either way.
    file_hashes.update({pdf_file: scan_index[pdf_file]["sha256"] for pdf_file, *_r in skip_plans})
    plan_by_name = {p[0]: p for p in skip_plans + scanned_plans}
    plans = [plan_by_name[pdf_file] for pdf_file in pdf_files]

    print(f"Scan complete - identifying names for {total_files} PDFs.\n")
    if cache_hits:
        print(f"({cache_hits} of those were instant fingerprint-cache hits.)\n")

    results, cancelled = execute_renames(plans, OUTPUT_DIRECTORY)
    if cancelled:
        return

    # Report in the same order the PDFs were originally scanned.
    order = {pdf_file: i for i, (pdf_file, *_rest) in enumerate(plans)}
    results.sort(key=lambda r: order[r[0]])
    # target_final_title as identify_file resolved it (pre-dedup-suffix,
    # pre-illegal-char-stripping) - what gets cached, not the on-disk
    # filename, since a future identical file should resolve the same way
    # even if collision handling gave THIS copy a "(2)" suffix.
    plan_targets = {pdf_file: target_final_title for pdf_file, _, target_final_title, _ in plans}

    matched_count = 0
    for pdf_file, match_method, new_filename, already_correct in results:
        if new_filename is None:
            if match_method and match_method not in ("None", "None [FAILED]"):
                print(f"⚠️  '{pdf_file}' - left unchanged. [{match_method}]")
            else:
                print(f"⚠️  No match found for '{pdf_file}' - left unchanged.")
        elif already_correct:
            print(f"✅ [{match_method}] '{pdf_file}' already correctly named.")
            matched_count += 1
        else:
            print(f"✅ [{match_method}]\n   '{pdf_file}'  ->  '{new_filename}'")
            matched_count += 1

    results = review_low_confidence_matches(results, OUTPUT_DIRECTORY, image_library, plan_targets)

    # Seed the fingerprint cache from every match just confirmed by a
    # high-confidence method, so an identical copy of this same file -
    # this user's on a future run, or anyone else's if they get a copy of
    # this cache file - can be identified instantly next time. Anything
    # labeled "(low confidence)" is a last-resort fallback specifically
    # because it isn't reliable enough to trust blindly; caching it would
    # make that guess permanent and unverified for every future encounter
    # of the identical file, so those are deliberately left out.
    new_cache_entries = 0
    for pdf_file, match_method, new_filename, already_correct in results:
        if new_filename is None or match_method.startswith("Fingerprint Cache") or "(low confidence)" in match_method:
            continue
        target_final_title = plan_targets.get(pdf_file)
        file_sha256 = file_hashes.get(pdf_file)
        if target_final_title and file_sha256 and file_sha256 not in fingerprint_cache:
            fingerprint_cache[file_sha256] = {"title": target_final_title, "matched_via": match_method}
            new_cache_entries += 1

    if new_cache_entries:
        save_fingerprint_cache(CACHE_PATH, fingerprint_cache)
        print(f"Fingerprint cache updated: +{new_cache_entries} new entries ({len(fingerprint_cache)} total).")

    # Keep the scan index (see load_scan_index) in sync with what actually
    # ended up on disk, so the next run's incremental scan can trust it:
    # confirmed matches get a fresh size/mtime/sha256 entry under their
    # final name, a file that came back unmatched has its entry dropped
    # (whatever confirmed it before no longer holds), and a renamed file's
    # old name is dropped so a future unrelated file dropped under that
    # same old name is never mistaken for it.
    if renaming_in_place:
        scan_index_changed = False
        for pdf_file, match_method, new_filename, already_correct in results:
            if new_filename is None:
                if scan_index.pop(pdf_file, None) is not None:
                    scan_index_changed = True
                continue
            if "(low confidence)" in match_method:
                continue
            file_sha256 = file_hashes.get(pdf_file)
            if not file_sha256:
                continue
            try:
                stat = os.stat(os.path.join(OUTPUT_DIRECTORY, new_filename))
            except OSError:
                continue
            entry = {"size": stat.st_size, "mtime": stat.st_mtime, "sha256": file_sha256}
            if scan_index.get(new_filename) != entry:
                scan_index[new_filename] = entry
                scan_index_changed = True
            if new_filename != pdf_file and scan_index.pop(pdf_file, None) is not None:
                scan_index_changed = True
        if scan_index_changed:
            save_scan_index(SCAN_INDEX_PATH, scan_index)

    print("\n==================================================")
    print(f"  Finished. Matched {matched_count} of {len(pdf_files)} PDFs.")
    print("==================================================")
    _report_progress("Finished", total_files, total_files)

    plan_paths = {pdf_file: full_pdf_path for pdf_file, full_pdf_path, *_rest in plans}
    unmatched = [
        (pdf_file, plan_paths[pdf_file])
        for pdf_file, match_method, new_filename, already_correct in results
        # A file already known to hang the scan (see _run_parallel_scan)
        # would just hang best_guess_for_unmatched the same way - it goes
        # through the same OCR/image-reading code - so it's excluded here
        # rather than offered for review.
        if new_filename is None and pdf_file in plan_paths and not (match_method or "").startswith("Skipped -> exceeded")
    ]
    review_unmatched_interactively(unmatched, OUTPUT_DIRECTORY, fingerprint_cache, scan_index, renaming_in_place)


if __name__ == "__main__":
    # Required for ProcessPoolExecutor to work at all in a frozen (PyInstaller)
    # exe on Windows: a spawned worker re-launches this same exe with an
    # internal marker telling it "you're a worker, run one task and exit."
    # freeze_support() is what makes the exe recognize that marker - without
    # it, every "worker" just doesn't see it, treats the relaunch as a normal
    # start, and reruns this entire script from the top: re-prompting for
    # config, then spawning its own pool of workers that do the same thing
    # again, compounding with every worker spawned. A no-op when not frozen
    # or not on Windows, so this is always safe to call.
    multiprocessing.freeze_support()

    # A double-clicked frozen exe's console window closes the instant the
    # process exits - on a normal finish that hides the final summary, and
    # on a crash it hides the traceback entirely. Pausing for a keypress
    # only in that case (never for a plain `python dnd_renamer.py` run,
    # where the terminal itself stays open) fixes both without changing
    # anything for the existing script-based workflow.
    try:
        check_dependencies()
        configure_paths()

        # The scan itself runs in a window showing live progress and a
        # scrolling log of everything that would otherwise only be
        # visible in the console - see dnd_renamer_gui.run_scan_window -
        # falling back to running it directly in the console (as before)
        # if tkinter isn't importable.
        try:
            from dnd_renamer_gui import run_scan_window
        except ImportError:
            run_scan_window = None

        if run_scan_window is not None:
            # Passing this module's own object rather than letting
            # run_scan_window do `import dnd_renamer` itself - when run
            # directly (`python dnd_renamer.py`), this file executes as
            # "__main__", not as a module named "dnd_renamer", so that
            # import would silently load a second, disconnected copy of
            # it instead of finding this one. See run_scan_window's
            # docstring - this was a real bug (Pause/Cancel did nothing).
            run_scan_window(run_matching_agent, sys.modules[__name__])
        else:
            run_matching_agent()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nCancelled.")
    except Exception:
        import traceback
        traceback.print_exc()
        print("\nSomething went wrong (see error above). Please report this as a bug.")
    finally:
        if getattr(sys, "frozen", False):
            try:
                input("\nPress Enter to exit...")
            except (EOFError, KeyboardInterrupt):
                pass
