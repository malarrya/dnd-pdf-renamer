; Inno Setup script for D&D Renamer.
;
; Build with (from repo root, after `pyinstaller --onefile --windowed --name
; dnd_renamer --icon installer\icon.ico dnd_renamer.py` has produced
; dist\dnd_renamer.exe):
;   "C:\Users\<you>\AppData\Local\Programs\Inno Setup 7\ISCC.exe" installer\dnd_renamer.iss
; Output lands in installer\output\DnD_Renamer_Setup.exe.

#define MyAppName "D&D Renamer"
#define MyAppVersion "1.1.3"
#define MyAppExeName "dnd_renamer.exe"

[Setup]
AppId={{8F3B6E2A-6E9F-4B2C-9A2E-6F6F6A8C6B10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\DnD Renamer
DefaultGroupName={#MyAppName}
; No admin rights required: installs under Program Files if run elevated,
; otherwise falls back to a per-user location automatically ({autopf}).
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
OutputDir=output
OutputBaseFilename=DnD_Renamer_Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
; SetupIconFile only affects the installer/uninstaller icon, not the
; launched app's window (that comes from the icon baked into
; dnd_renamer.exe itself - see BUILD.md's --icon flag).
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "installocr"; Description: "Install the Tesseract OCR engine (lets the tool identify scanned PDFs with no text layer; requires internet access)"; GroupDescription: "Optional components:"

[Files]
Source: "..\dist\dnd_renamer.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dnd_renamer_config.example.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dnd_renamer_cache.example.json"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; Tesseract OCR is a separate native engine the script can't pip-install -
; winget is the only way to get it onto the machine without bundling a
; ~50MB third-party installer in this repo. Best-effort only: skipped
; outright if winget isn't available or Tesseract already is, and never
; treated as fatal if it fails (the app already degrades gracefully and
; reports OCR as unavailable at startup either way).
Filename: "{cmd}"; Parameters: "/C winget install --id UB-Mannheim.TesseractOCR -e --silent --accept-package-agreements --accept-source-agreements"; StatusMsg: "Installing Tesseract OCR (optional - enables scanned-PDF support)..."; Flags: runhidden; Check: ShouldInstallTesseract
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: postinstall skipifsilent nowait

[Code]
function TesseractInstalled: Boolean;
begin
  Result := FileExists(ExpandConstant('{pf}\Tesseract-OCR\tesseract.exe'))
    or FileExists(ExpandConstant('{pf32}\Tesseract-OCR\tesseract.exe'));
end;

function WingetAvailable: Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'), '/C where winget >nul 2>nul', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function ShouldInstallTesseract: Boolean;
begin
  Result := WizardIsTaskSelected('installocr') and (not TesseractInstalled) and WingetAvailable;
end;
