; Inno Setup script (free: https://jrsoftware.org/isinfo.php). build_installer.bat runs it if Inno Setup 6 is installed.
; Keep AppVersion in sync with APP_VERSION in launcher.py.
#define AppName "Music Studio"
#define AppVersion "1.0.1"

[Setup]
AppId={{B7C3F1E2-5A4D-4E8B-9C21-3D6F0A7E1B94}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\MusicStudio
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
OutputDir=installer_output
OutputBaseFilename=MusicStudio_Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
DisableProgramGroupPage=yes
SetupIconFile=guitar.ico
UninstallDisplayIcon={app}\guitar.ico
[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Files]
Source: "dist\MusicStudio\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "guitar.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\MusicStudio.exe"
Name: "{autoprograms}\{#AppName} - Repair setup"; Filename: "{app}\MusicStudio.exe"; Parameters: "--setup"
Name: "{autoprograms}\{#AppName} - Start ACE-Step (optional, for AI Song)"; Filename: "{app}\start_ace_step.bat"; Check: FileExists(ExpandConstant('{app}\start_ace_step.bat'))
Name: "{autoprograms}\{#AppName} - Install ACE-Step (optional)"; Filename: "{app}\install_ace_step.bat"; Check: FileExists(ExpandConstant('{app}\install_ace_step.bat'))
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\MusicStudio.exe"; Tasks: desktopicon
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\MusicStudio.exe"; IconFilename: "{app}\guitar.ico"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\MusicStudio.exe"; IconFilename: "{app}\guitar.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\MusicStudio.exe"; Description: "Start {#AppName} now (first start sets up all components)"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; the downloaded components are removed; the user's own data in {localappdata}\MusicStudio\app
; (users.json, uploads, outputs) is kept on purpose.
Type: filesandordirs; Name: "{localappdata}\MusicStudio\venv"
Type: filesandordirs; Name: "{localappdata}\MusicStudio\python"
Type: filesandordirs; Name: "{localappdata}\MusicStudio\bin"
Type: files; Name: "{localappdata}\MusicStudio\setup_state.json"
