; Inno Setup script for the Windows setup.
;
; Compile with the Inno Setup IDE, or from a Developer Command Prompt:
;
;     iscc installer\windows.iss
;
; Every path here is relative to this file, so it compiles from a clone with
; nothing to edit. Run build.py first: this script packages the executable that
; build.py leaves in the project root.

#define MyAppName "Snakeee"
; Kept in step with APP_VERSION in app.py by hand. build.py does not write this
; file, so bump both together when releasing.
#include "version.iss"
#define MyAppPublisher "Tomiwa Adesanya"
#define MyAppURL "https://github.com/tomiwa-adesanya/snakeee"
#define MyAppExeName "snakeee.exe"
#expr EmitLanguagesSection

[Setup]
; This GUID identifies the application to Windows. It must never change: it is
; what makes a new setup upgrade an existing install in place instead of
; putting a second copy beside it. It is deliberately independent of the name,
; so a renamed build still upgrades correctly.
AppId={{952B9BFA-C5DB-4646-BEE5-8E59DD3671E6}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
; Offer the per-user install as well as the per-machine one. The game writes
; nothing outside its own data directory, so it does not need administrator
; rights to be useful.
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=snakeee-v{#MyAppVersion}-windows-x64-setup
SetupIconFile=..\static\icons\app\icon.ico
SolidCompression=yes
WizardStyle=classic windows11
LicenseFile=..\LICENSE

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; build.py produces a single self-extracting executable, so this is the whole
; of what gets installed. There is no data directory to copy beside it: the
; static files are compiled into the binary.
Source: "..\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
