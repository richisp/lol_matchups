; Inno Setup script for LoL Draft Helper.
;
; Version is injected via the command line:
;   ISCC.exe /DAppVersion=1.2.3 installer.iss
;
; Per-user install (PrivilegesRequired=lowest) so the auto-updater can
; overwrite the .exe without UAC prompts.

#ifndef AppVersion
#define AppVersion "0.0.0"
#endif

[Setup]
AppName=LoL Draft Helper
AppVersion={#AppVersion}
AppPublisher=richisp
AppPublisherURL=https://github.com/richisp/lol_matchups
; {autopf} resolves to Program Files when installing as admin (per-machine)
; or to %LOCALAPPDATA%\Programs when installing as the current user.
; PrivilegesRequiredOverridesAllowed=dialog gives the user that choice on
; the second wizard page. We default to per-user so auto-update can write
; to the install dir without UAC; users can elevate if they want a per-
; machine install in Program Files (auto-update will then be effectively
; disabled and they'll need to reinstall to upgrade).
DefaultDirName={autopf}\LoLDraftHelper
DefaultGroupName=LoL Draft Helper
OutputDir=installer
OutputBaseFilename=lol-draft-helper-setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
DisableProgramGroupPage=yes
WizardStyle=modern
MinVersion=10.0
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\lol-draft-helper.exe

[Files]
Source: "dist\lol-draft-helper.exe"; DestDir: "{app}"; Flags: ignoreversion
; Bundle the latest crawled DB so the app works offline immediately. The
; auto-sync will pick up newer snapshots on subsequent launches. If the
; build pipeline didn't fetch a DB (first build before the crawler ran),
; the file may be missing — skip rather than fail.
Source: "lolalytics.db"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\LoL Draft Helper"; Filename: "{app}\lol-draft-helper.exe"
Name: "{userdesktop}\LoL Draft Helper"; Filename: "{app}\lol-draft-helper.exe"; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{app}\lol-draft-helper.exe"; Description: "Launch LoL Draft Helper"; Flags: nowait postinstall skipifsilent
