# Building the Windows installer

This produces `installer/output/DnD_Renamer_Setup.exe` - a standalone installer
that needs nothing pre-installed (no Python, no pip packages) and, if the user
opts in, installs the Tesseract OCR engine for them too.

## 1. Build the standalone exe

From the repo root, in a fresh virtual environment:

```
python -m venv build_venv
build_venv\Scripts\pip install pyinstaller pypdf pytesseract Pillow imagehash
build_venv\Scripts\python -m PyInstaller --onefile --console --name dnd_renamer --icon installer\icon.ico --clean dnd_renamer.py
```

This produces `dist\dnd_renamer.exe`, a single file with pypdf, pytesseract,
Pillow, and imagehash all bundled in - end users don't need any of them
installed separately. (Tesseract OCR itself is a native engine, not a Python
package, and can't be bundled this way - see step 2.) `--icon` bakes
`installer/icon.ico` (a placeholder d20 glyph) into the exe itself, which is
also what Explorer, the taskbar, and the Start Menu/Desktop shortcuts show -
swap that file for real artwork whenever it's available and rebuild.

## 2. Build the installer

Requires [Inno Setup](https://jrsoftware.org/isinfo.php) (`winget install
JRSoftware.InnoSetup.7`). Then:

```
"C:\Users\<you>\AppData\Local\Programs\Inno Setup 7\ISCC.exe" installer\dnd_renamer.iss
```

Output: `installer\output\DnD_Renamer_Setup.exe`. It installs the exe plus
`README.md` and the two `.example.json` files, creates Start Menu / optional
Desktop shortcuts, and - if the user leaves the "Install Tesseract OCR"
task checked - runs `winget install UB-Mannheim.TesseractOCR` silently after
copying files (skipped if winget isn't available or Tesseract's already
installed; never treated as a fatal install error either way).

## Notes

- `PrivilegesRequired=lowest` in the .iss: run the installer as a normal
  user and it installs per-user (no UAC prompt); run it elevated and it
  installs to Program Files instead. Either way works.
- Config/cache/scan-index files are written next to the *exe*, not into a
  PyInstaller temp extraction dir - see `_APP_DIR` in `dnd_renamer.py`. If
  you ever change how the app is packaged (onedir instead of onefile, a
  different bootloader, etc.), re-verify that still holds before shipping.
- No code signing is set up. Unsigned installers get a Windows
  SmartScreen warning ("Windows protected your PC") on first run; users
  need to click "More info" -> "Run anyway". A code-signing certificate
  would remove that, but is a paid, ongoing cost - out of scope unless
  this starts seeing wider distribution.
