"""Tkinter windows for D&D Renamer: the four-field path setup screens, and
the scan-progress window (progress bar + scrolling log).

Imported lazily (inside a function, not at module scope) by
dnd_renamer.py, so a Python build without tkinter falls back to that
module's console-only behavior instead of failing outright.
"""
import multiprocessing
import os
import queue
import re
import signal
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# (config key, on-screen label, dialog kind: "file" or "dir")
FIELDS = [
    ("xml_path", "LaunchBox platform XML file:", "file"),
    ("pdf_directory", "Folder containing the PDFs to rename:", "dir"),
    ("image_directory", "LaunchBox 'Box - Front' image folder:", "dir"),
    ("output_directory", "Output folder for the renamed PDFs:", "dir"),
]

FIELD_LABELS = {key: label for key, label, _kind in FIELDS}


def _strip_quotes(raw):
    return raw.strip().strip('"').strip("'")


def _validate(key, value):
    """Mirrors dnd_renamer.prompt_for_path's validation for the same
    field, minus the retry loop (the caller re-shows the window instead)."""
    if not value:
        return "This field is required."
    if key == "xml_path":
        return None if os.path.isfile(value) else "Couldn't find a file at that path."
    if key == "output_directory":
        if os.path.isdir(value):
            return None
        parent = os.path.dirname(value.rstrip("\\/")) or value
        return None if os.path.isdir(parent) else "Couldn't find that folder (or its parent)."
    return None if os.path.isdir(value) else "Couldn't find a folder at that path."


def _make_browse(var, kind, root):
    def browse():
        current = _strip_quotes(var.get())
        if kind == "file":
            picked = filedialog.askopenfilename(
                title="Select the LaunchBox platform XML file",
                filetypes=[("XML files", "*.xml"), ("All files", "*.*")],
                initialdir=os.path.dirname(current) if current else None,
                parent=root,
            )
        else:
            picked = filedialog.askdirectory(
                title="Select folder",
                initialdir=current if os.path.isdir(current) else (os.path.dirname(current) or None),
                parent=root,
            )
        if picked:
            var.set(os.path.normpath(picked))

    return browse


def _new_window(title):
    """Creates a Tk root and forces the 'clam' ttk theme - a theme drawn
    entirely by Tk itself, rather than 'vista' (Tk's default on Windows),
    which paints widgets via the OS's native uxtheme API. That native
    path is the prime suspect for a real, reproducible bug: buttons that
    measure as correctly sized and positioned (verified externally via
    GetWindowRect) yet never visually render on a multi-monitor/remote-
    session Windows machine - a known class of uxtheme rendering failure
    in exactly that kind of environment. 'clam' sidesteps uxtheme
    entirely, at the cost of looking less native."""
    root = tk.Tk()
    root.title(title)
    try:
        ttk.Style(root).theme_use("clam")
    except tk.TclError:
        pass
    return root


def _center(root):
    """Centers root on the screen, both horizontally and vertically."""
    root.update_idletasks()
    # A single update_idletasks() right after packing can under-report a
    # freshly-added widget's contribution to the container's total
    # requested size - a second pass (after Tk has settled on the first)
    # reliably reports the true final size.
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f"{w}x{h}+{x}+{y}")


def _center_on_parent(dialog, parent):
    """Centers a Toplevel dialog over its parent window rather than the
    whole screen - the usual expectation for a modal dialog. Falls back
    to centering on the screen if the parent's position/size can't be
    read (e.g. it's withdrawn), and clamps so the dialog never lands
    partly off-screen if the parent is near an edge."""
    dialog.update_idletasks()
    dw, dh = dialog.winfo_width(), dialog.winfo_height()
    try:
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        x = px + (pw - dw) // 2
        y = py + (ph - dh) // 2
    except tk.TclError:
        x = (dialog.winfo_screenwidth() - dw) // 2
        y = (dialog.winfo_screenheight() - dh) // 2
    x = max(0, min(x, dialog.winfo_screenwidth() - dw))
    y = max(0, min(y, dialog.winfo_screenheight() - dh))
    dialog.geometry(f"+{x}+{y}")


def configure_paths_gui(config, stale=False):
    """Shows one browsable field per FIELDS entry, pre-filled from
    `config`. Returns a new {config_key: value} dict on "Save and
    Continue" (each value already validated), or None if the user
    cancelled or closed the window."""
    result = {"config": None}

    root = _new_window("D&D Renamer - Setup")

    main = ttk.Frame(root, padding=16)
    main.pack(fill="both", expand=True)
    main.columnconfigure(0, weight=1)

    row = 0
    if stale:
        warning = (
            "One or more saved paths couldn't be found. If a path uses a mapped\n"
            "network drive letter (e.g. Z:\\...), try the network path instead,\n"
            "e.g. \\\\server\\share\\folder."
        )
        ttk.Label(main, text=warning, foreground="#a83232", justify="left").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )
        row += 1

    vars_by_key = {}
    for key, label, kind in FIELDS:
        ttk.Label(main, text=label).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        var = tk.StringVar(value=config.get(key) or "")
        vars_by_key[key] = var
        ttk.Entry(main, textvariable=var, width=64).grid(
            row=row, column=0, sticky="ew", padx=(0, 8), pady=(2, 12)
        )
        ttk.Button(main, text="Browse...", command=_make_browse(var, kind, root)).grid(
            row=row, column=1, sticky="e", pady=(2, 12)
        )
        row += 1

    def on_continue():
        new_config = {}
        for key, label, _kind in FIELDS:
            value = _strip_quotes(vars_by_key[key].get())
            error = _validate(key, value)
            if error:
                messagebox.showerror("D&D Renamer", f"{label}\n{error}", parent=root)
                return
            new_config[key] = value
        result["config"] = new_config
        root.destroy()

    button_row = ttk.Frame(main)
    button_row.grid(row=row, column=0, columnspan=2, sticky="e", pady=(4, 0))
    ttk.Button(button_row, text="Cancel", command=root.destroy).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(button_row, text="Save and Continue", command=on_continue).grid(row=0, column=1)

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    _center(root)
    root.mainloop()
    return result["config"]


def confirm_paths_gui(config):
    """Shows the already-valid saved paths read-only, with a choice to
    keep them or edit them. Returns "continue", "change", or None if the
    user cancelled/closed the window."""
    result = {"choice": None}

    root = _new_window("D&D Renamer - Setup")

    main = ttk.Frame(root, padding=6)
    main.pack(fill="both", expand=True)

    ttk.Label(main, text="Using saved settings:", font=("", 9, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 2)
    )
    for i, (key, label, _kind) in enumerate(FIELDS, start=1):
        ttk.Label(main, text=label, font=("", 8)).grid(row=i, column=0, sticky="w", padx=(0, 10), pady=0)
        ttk.Label(main, text=config.get(key, ""), foreground="#444", font=("", 8)).grid(
            row=i, column=1, sticky="w", pady=0
        )

    def choose(choice):
        result["choice"] = choice
        root.destroy()

    button_row = ttk.Frame(main)
    button_row.grid(row=len(FIELDS) + 1, column=0, columnspan=2, sticky="e", pady=(4, 0))
    ttk.Button(button_row, text="Change Settings...", command=lambda: choose("change")).grid(
        row=0, column=0, padx=(0, 8)
    )
    ttk.Button(button_row, text="Continue", command=lambda: choose("continue")).grid(row=0, column=1)

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    _center(root)
    root.mainloop()
    return result["choice"]


class _QueueWriter:
    """A file-like object that mirrors every write into a queue (for the
    log widget below), into the real underlying stream if any (so a
    console attached to the process keeps seeing the exact same output
    it always has), and into a plain-text log file on disk from the very
    first line - independent of the log widget's own 4000-line cap, so
    "View Full Log" always has the complete run, and it keeps growing
    for the rest of the run whether or not anyone ever opens it."""

    def __init__(self, q, real_stream, log_file=None):
        self._q = q
        self._real_stream = real_stream
        self._log_file = log_file

    def write(self, s):
        if self._real_stream is not None:
            try:
                self._real_stream.write(s)
            except Exception:
                pass
        if self._log_file is not None:
            try:
                self._log_file.write(s)
                self._log_file.flush()
            except Exception:
                pass
        if s:
            self._q.put(("log", s))
        return len(s)

    def flush(self):
        if self._real_stream is not None:
            try:
                self._real_stream.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def _feed_log(text_widget, s, state):
    """Applies one chunk of raw stdout/stderr text to the log Text
    widget, treating a bare '\\r' the way a real terminal would: the next
    literal text overwrites the current line instead of appending a new
    one. Without this, the frequent `print(..., end="\\r")` progress
    updates elsewhere in the app would each add their own line instead of
    updating in place, flooding the log with one line per file.

    All inserts/deletes target "end-1c" rather than "end". A Tk Text
    widget always keeps one trailing newline that can never really be
    removed; once a delete leaves that newline as the buffer's *only*
    newline, Tk starts treating it as that permanent placeholder again,
    so a plain insert("end", ...) lands *before* it and silently glues
    onto the previous line instead of starting a new one. Working
    against "end-1c" (the position of that trailing newline) rather
    than "end" itself keeps it untouched and avoids the merge.
    """
    for piece in re.findall(r'[^\r\n]+|\r|\n', s):
        if piece == "\n":
            text_widget.insert("end-1c", "\n")
            state["pending_clear"] = False
        elif piece == "\r":
            state["pending_clear"] = True
        else:
            if state["pending_clear"]:
                text_widget.delete("end-1c linestart", "end-1c")
                state["pending_clear"] = False
            text_widget.insert("end-1c", piece)


def _make_confirm_hook(q):
    """Returns the function installed as dnd_renamer.CONFIRM_HOOK. Called
    from the scan's background thread (see review_unmatched_interactively/
    _confirm_suggestion in dnd_renamer.py) - it can't build or touch any
    Tkinter widget itself, since only the main thread owns those. Instead
    it hands the request to the main thread via the same queue the log
    and progress updates already use, then blocks on a threading.Event
    until poll() (running on the main thread) has shown the comparison
    dialog and a human has actually clicked something."""

    def confirm_hook(request):
        result_holder = {"decision": "stop"}
        event = threading.Event()
        q.put(("confirm_request", request, result_holder, event))
        event.wait()
        return result_holder["decision"]

    return confirm_hook


def _make_yesno_hook(q):
    """Returns the function installed as dnd_renamer.YESNO_HOOK - the
    plain yes/no counterpart to _make_confirm_hook, for the console's
    bare gate questions (e.g. "Review best-guess suggestions for them
    one at a time?") that have no image comparison to show. Same
    cross-thread queue+Event handoff to the main thread."""

    def yesno_hook(message, allow_stop):
        result_holder = {"decision": "no"}
        event = threading.Event()
        q.put(("yesno_request", message, allow_stop, result_holder, event))
        event.wait()
        return result_holder["decision"]

    return yesno_hook


def _show_yesno_dialog(root, message, allow_stop):
    """Modal Yes/No (optionally +Stop) dialog for one of the console's
    plain gate questions. Returns "yes", "no", or "stop"."""
    result = {"decision": "no"}

    dialog = tk.Toplevel(root)
    dialog.title("D&D Renamer")
    dialog.transient(root)

    main = ttk.Frame(dialog, padding=10)
    main.pack(fill="both")
    ttk.Label(main, text=message, justify="left", wraplength=420).pack(anchor="w", pady=(0, 10))

    def choose(decision):
        result["decision"] = decision
        dialog.destroy()

    button_row = ttk.Frame(main)
    button_row.pack(fill="x")
    if allow_stop:
        ttk.Button(button_row, text="Stop Reviewing", command=lambda: choose("stop")).pack(side="left")
    ttk.Button(button_row, text="No", command=lambda: choose("no")).pack(side="right", padx=(8, 0))
    ttk.Button(button_row, text="Yes", command=lambda: choose("yes")).pack(side="right")

    dialog.protocol("WM_DELETE_WINDOW", lambda: choose("stop" if allow_stop else "no"))
    _center_on_parent(dialog, root)
    dialog.grab_set()
    dialog.wait_window()
    return result["decision"]


def _show_confirm_dialog(root, request):
    """Modal comparison dialog for one low-confidence rename suggestion:
    the catalog's box-art image side by side with whatever could be
    extracted from the PDF's own front page, so a human can visually
    verify the match instead of judging on the filename/score alone.
    Runs on the main thread (a Toplevel child of the run window, not a
    fresh Tk() root - it must coexist with that window, not replace it).
    Returns "yes", "no", or "stop"."""
    try:
        from PIL import Image, ImageTk
        pil_available = True
    except ImportError:
        pil_available = False

    result = {"decision": "no"}
    photo_refs = []  # keep PhotoImage objects alive for the dialog's lifetime - Tk drops a garbage-collected one silently, leaving a blank label

    dialog = tk.Toplevel(root)
    dialog.title("Confirm Suggestion")
    dialog.transient(root)

    main = ttk.Frame(dialog, padding=10)
    main.pack(fill="both")

    info = (
        f"Currently named:  {request['pdf_file']}\n"
        f"Suggested identity:  {request['safe_title']}.pdf\n"
        f"{request['detail']}\n\n"
        f"Do the two images below show the same book?"
    )
    ttk.Label(main, text=info, justify="left").pack(anchor="w", pady=(0, 8))

    images_row = ttk.Frame(main)
    images_row.pack()

    THUMB_SIZE = (160, 210)

    def add_thumb(pil_image, path, caption):
        col = ttk.Frame(images_row)
        col.pack(side="left", padx=10)
        ttk.Label(col, text=caption, font=("", 8, "bold")).pack()
        img = pil_image
        if img is None and path and pil_available:
            try:
                img = Image.open(path)
            except Exception:
                img = None
        if img is not None and pil_available:
            try:
                img = img.copy()
                img.thumbnail(THUMB_SIZE)
                photo = ImageTk.PhotoImage(img)
                photo_refs.append(photo)
                ttk.Label(col, image=photo).pack()
                return
            except Exception:
                pass
        placeholder = ttk.Label(
            col, text="(no preview\navailable)", justify="center", anchor="center",
            relief="solid", borderwidth=1, width=18,
        )
        placeholder.pack(ipady=THUMB_SIZE[1] // 2 - 15)

    add_thumb(None, request["box_art_path"], "Suggested match\n(catalog box art)")
    add_thumb(request["preview_image"], None, "This file's own\nfront page")

    def choose(decision):
        result["decision"] = decision
        dialog.destroy()

    button_row = ttk.Frame(main)
    button_row.pack(fill="x", pady=(10, 0))
    ttk.Button(button_row, text="Stop Reviewing", command=lambda: choose("stop")).pack(side="left")
    ttk.Button(button_row, text="Reject", command=lambda: choose("no")).pack(side="right", padx=(8, 0))
    ttk.Button(button_row, text="Confirm Rename", command=lambda: choose("yes")).pack(side="right")

    dialog.protocol("WM_DELETE_WINDOW", lambda: choose("no"))
    _center_on_parent(dialog, root)
    dialog.grab_set()
    dialog.wait_window()
    return result["decision"]


def _force_close(dnd_renamer, root):
    """Guarantees the whole application actually exits when Close is
    clicked or the window's X is pressed - not just this window. A
    ProcessPoolExecutor that doesn't get a clean .shutdown() call on
    some exception path (this has happened - see git history on
    orphaned worker processes) leaves its manager thread running as a
    non-daemon thread, which silently keeps the whole process alive
    even after every window is gone and nothing is visibly happening;
    a worker process it already spawned is a separate OS process
    entirely, unaffected by anything happening in this one. Explicitly
    terminating both here removes any dependence on every executor
    call site's cleanup being airtight, rather than trying to audit
    each one. os._exit() (not sys.exit()) is deliberate - it forces an
    immediate stop with no cleanup handlers or thread joins, which is
    exactly what's needed here."""
    try:
        dnd_renamer.CANCEL_EVENT.set()
        for child in multiprocessing.active_children():
            try:
                child.terminate()
            except Exception:
                pass
        root.destroy()
    except Exception:
        pass
    finally:
        os._exit(0)


def run_scan_window(run_fn, dnd_renamer):
    """Opens a window with a progress bar and a scrolling log mirroring
    stdout/stderr while run_fn() runs on a background thread (tkinter
    needs the main thread for its own event loop, so the actual work
    can't happen there). Blocks until the window is closed.

    `dnd_renamer` must be the caller's OWN module object (pass
    sys.modules[__name__]) rather than importing "dnd_renamer" by name
    here - when dnd_renamer.py is run directly (`python dnd_renamer.py`),
    it executes as "__main__", not as a module named "dnd_renamer". An
    `import dnd_renamer` from inside this file wouldn't find that
    __main__ module under this different name and would instead load
    dnd_renamer.py a SECOND time as a brand new, independent module -
    with its own separate PAUSE_EVENT/CANCEL_EVENT, disconnected from the
    ones the actual running scan checks. That was a real, reproduced bug:
    every button here appeared to do nothing at all when launched the
    normal way, while working fine in any test harness that imported
    dnd_renamer.py as a plain module instead of running it as the
    entry point.

    A Pause button holds the scan at its next checkpoint (dnd_renamer.
    PAUSE_EVENT) without cancelling it - worker processes already in
    flight keep running, only new work stops being dispatched - and
    toggles to Resume. A Cancel button - and, via a SIGINT handler
    installed here, the console's own Ctrl+C - requests cooperative
    cancellation through dnd_renamer.CANCEL_EVENT, which the scan checks
    at each of the same checkpoints (see _check_control in
    dnd_renamer.py). That bridge is what keeps Ctrl+C working at all
    once run_fn() is on a background thread: a real SIGINT is only ever
    delivered to the main thread,
    which here is sitting in Tkinter's mainloop, not in the scan."""
    dnd_renamer.CANCEL_EVENT.clear()
    dnd_renamer.PAUSE_EVENT.clear()
    q = queue.Queue()

    # A real, persistent file capturing the complete run from the very
    # first line - unlike the log Text widget below (capped at 4000
    # lines to bound memory on a long run), this never trims anything,
    # and it keeps growing for the rest of the run whether or not "View
    # Full Log" is ever clicked. Named per-run (timestamped) so it
    # doesn't collide with or overwrite a previous run's log.
    log_file_path = os.path.join(
        dnd_renamer._APP_DIR, f"dnd_renamer_log_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    )
    try:
        log_file = open(log_file_path, "w", encoding="utf-8")
    except OSError:
        log_file, log_file_path = None, None

    real_stdout, real_stderr = sys.stdout, sys.stderr
    sys.stdout = _QueueWriter(q, real_stdout, log_file)
    sys.stderr = _QueueWriter(q, real_stderr, log_file)
    dnd_renamer.PROGRESS_HOOK = lambda phase, completed, total: q.put(("progress", phase, completed, total))
    dnd_renamer.CONFIRM_HOOK = _make_confirm_hook(q)
    dnd_renamer.YESNO_HOOK = _make_yesno_hook(q)

    prev_sigint_handler = None
    try:
        prev_sigint_handler = signal.signal(signal.SIGINT, lambda *_a: dnd_renamer.CANCEL_EVENT.set())
    except (ValueError, OSError):
        pass  # not on the main thread, or not supported here - the Cancel button still works

    root = _new_window("D&D Renamer - Running")
    # A fixed height set explicitly (rather than measuring pack()'s own
    # natural size, which proved unreliable here - see git history) plus
    # matching maxsize keeps the initial layout from shifting on the
    # first paint. The log area only shows a handful of lines as a
    # result - "View Full Log" below opens everything captured so far
    # in Notepad.
    root.geometry("760x445")
    root.minsize(760, 445)
    root.maxsize(2000, 445)

    main = ttk.Frame(root, padding=8)
    main.pack(fill="both")

    top_row = ttk.Frame(main)
    top_row.pack(fill="x")
    status_var = tk.StringVar(value="Starting...")
    ttk.Label(top_row, textvariable=status_var).pack(side="left")
    progress = ttk.Progressbar(main, mode="determinate", maximum=1)
    progress.pack(fill="x", pady=(2, 6))

    log_frame = ttk.Frame(main)
    log_frame.pack(fill="x")
    log_text = tk.Text(log_frame, wrap="none", state="disabled", font=("Consolas", 8), height=20, width=110)
    scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
    log_text.configure(yscrollcommand=scrollbar.set)
    log_text.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    def view_full_log():
        if log_file_path is None:
            messagebox.showerror("D&D Renamer", "Couldn't create a log file for this run.", parent=root)
            return
        log_file.flush()
        try:
            os.startfile(log_file_path)
        except Exception as e:
            messagebox.showerror(
                "D&D Renamer", f"Couldn't open the log file:\n{e}\n\nIt was saved to:\n{log_file_path}", parent=root
            )

    def toggle_pause():
        pausing = not ui_state["paused"]
        ui_state["paused"] = pausing
        if pausing:
            dnd_renamer.PAUSE_EVENT.set()
        else:
            dnd_renamer.PAUSE_EVENT.clear()
        pause_button.configure(text="Resume" if pausing else "Pause")
        status_var.set(ui_state["base_status"] + (" [Paused]" if pausing else ""))

    # The buttons' real Tk parent must be button_row itself, not main -
    # creating them under main and only *visually* placing them inside
    # button_row via pack(in_=...) left them as button_row's siblings, and
    # since button_row (an opaque frame) was created after them, it was
    # stacked on top and silently painted over them: right size, right
    # position, completely invisible - reproduced and confirmed via a
    # winfo_children() dump showing button_row occupying the exact same
    # rectangle as the three buttons underneath it.
    button_row = ttk.Frame(main)
    button_row.pack(fill="x", pady=(4, 0))
    pause_button = ttk.Button(button_row, text="Pause", command=toggle_pause)
    pause_button.pack(side="left")
    cancel_button = ttk.Button(button_row, text="Cancel", command=lambda: dnd_renamer.CANCEL_EVENT.set())
    cancel_button.pack(side="left", padx=(8, 0))
    ttk.Button(button_row, text="View Full Log", command=view_full_log).pack(side="left", padx=(8, 0))
    close_button = ttk.Button(button_row, text="Close", state="disabled", command=lambda: _force_close(dnd_renamer, root))
    close_button.pack(side="right")

    pause_note = f"Note: Pause stops new files immediately, but up to {dnd_renamer.SCAN_WORKERS} already in progress will finish first."
    ttk.Label(main, text=pause_note, foreground="#666", font=("", 8)).pack(anchor="w", pady=(4, 0))

    log_state = {"pending_clear": False}
    ui_state = {"paused": False, "base_status": "Starting..."}
    run_state = {"done": False}

    def append_log(s):
        log_text.configure(state="normal")
        _feed_log(log_text, s, log_state)
        # Cap retained lines so a very large run doesn't grow this
        # window's memory without bound.
        line_count = int(log_text.index("end-1c").split(".")[0])
        if line_count > 4000:
            log_text.delete("1.0", f"{line_count - 3000}.0")
        log_text.see("end")
        # see("end") only guarantees vertical visibility - with
        # wrap="none", inserting text also drags the widget's
        # HORIZONTAL view to follow the insertion cursor, which for a
        # long line (the un-wrapped ~100-char progress/status lines)
        # ends up scrolled to the right, hiding the left edge (the
        # "Scanning (x/y):" prefix that actually matters) after enough
        # output has gone by. Pinning the horizontal view back to 0
        # every time keeps every line's beginning in view instead.
        log_text.xview_moveto(0)
        log_text.configure(state="disabled")

    def apply_progress(phase, completed, total):
        # total == 0 means "unknown length" (e.g. still loading the
        # catalog, before any per-file count exists) - shown as a static
        # empty bar with just the phase text, rather than an animated
        # indeterminate bar. ttk.Progressbar's start()/stop() autoincrement
        # was tried here first, but its internal timer doesn't reliably
        # stop: even with mode already back to "determinate" and stop()
        # called, it kept firing and stomping every explicit value set for
        # the rest of the run - a static bar sidesteps that entirely.
        if total > 0:
            progress.configure(mode="determinate", maximum=total)
            progress["value"] = completed
            ui_state["base_status"] = f"{phase} ({completed}/{total})"
        else:
            progress.configure(mode="determinate", maximum=1)
            progress["value"] = 0
            ui_state["base_status"] = phase
        # A progress update only ever arrives between checkpoints, i.e.
        # while not actually paused (see _check_control in dnd_renamer.py) -
        # but the toggle_pause() button click can't rely on new progress
        # ever arriving to refresh the label, so it appends/strips the
        # "[Paused]" suffix itself. Re-applying it here too just keeps this
        # function the single source of truth for what status_var shows.
        status_var.set(ui_state["base_status"] + (" [Paused]" if ui_state["paused"] else ""))

    def poll():
        try:
            while True:
                try:
                    kind, *payload = q.get_nowait()
                except queue.Empty:
                    break
                try:
                    if kind == "log":
                        append_log(payload[0])
                    elif kind == "progress":
                        apply_progress(*payload)
                    elif kind == "done":
                        run_state["done"] = True
                        pause_button.configure(state="disabled")
                        cancel_button.configure(state="disabled")
                        close_button.configure(state="normal")
                    elif kind == "confirm_request":
                        request, result_holder, event = payload
                        try:
                            result_holder["decision"] = _show_confirm_dialog(root, request)
                        finally:
                            # Always set the event, even if the dialog
                            # itself blew up - otherwise the worker
                            # thread waits on it forever.
                            event.set()
                    elif kind == "yesno_request":
                        message, allow_stop, result_holder, event = payload
                        try:
                            result_holder["decision"] = _show_yesno_dialog(root, message, allow_stop)
                        finally:
                            event.set()
                except Exception:
                    # A single bad item (or an exception from tkinter
                    # itself under load) must never silently kill this
                    # loop's rescheduling below - that would freeze every
                    # future update (status/progress/log) while the scan
                    # keeps running in the background, unnoticed until
                    # it's long since finished. Report it into the log
                    # the same way an error in run_fn() itself would be.
                    import traceback
                    traceback.print_exc()
        finally:
            if not run_state["done"]:
                root.after(75, poll)

    def worker():
        try:
            run_fn()
        except KeyboardInterrupt:
            # run_fn (run_matching_agent) already catches KeyboardInterrupt
            # internally at every cancellable stage and returns normally -
            # this is just a safety net in case a future _check_cancelled()
            # call ever lands somewhere that isn't wrapped, so it prints a
            # clean message instead of a raw thread traceback (KeyboardInterrupt
            # isn't an Exception subclass, so the except below won't catch it).
            print("\nCancelled.")
        except Exception:
            import traceback
            traceback.print_exc()
            print("\nSomething went wrong (see error above). Please report this as a bug.")
        finally:
            q.put(("done",))

    def on_close():
        if run_state["done"]:
            _force_close(dnd_renamer, root)
        # Ignore the close button/X while the scan is still running -
        # closing the window can't stop the background thread, so
        # letting it through would orphan the scan instead of cancelling
        # it. Cancel is the right way to stop early.

    root.protocol("WM_DELETE_WINDOW", on_close)
    _center(root)

    if log_file_path is not None:
        print(f"Full log for this run: {log_file_path}\n")

    threading.Thread(target=worker, daemon=True).start()
    root.after(75, poll)
    root.mainloop()

    sys.stdout, sys.stderr = real_stdout, real_stderr
    dnd_renamer.PROGRESS_HOOK = None
    dnd_renamer.CONFIRM_HOOK = None
    dnd_renamer.YESNO_HOOK = None
    if prev_sigint_handler is not None:
        try:
            signal.signal(signal.SIGINT, prev_sigint_handler)
        except (ValueError, OSError):
            pass
    if log_file is not None:
        try:
            log_file.close()
        except Exception:
            pass


if __name__ == "__main__":
    # Quick manual check: `python dnd_renamer_gui.py` pops the setup
    # window with no pre-filled values and prints whatever comes back.
    print(configure_paths_gui({}, stale=False))
