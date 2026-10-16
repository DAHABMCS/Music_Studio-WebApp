; Inno Setup script (free: https://jrsoftware.org/isinfo.php). build_installer.bat runs it if Inno Setup 6 is installed.
; Keep AppVersion in sync with APP_VERSION in launcher.py.
#define AppName "Music Studio"
#define AppVersion "1.0.4"

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
Name: "desktopicon"; Description: "Create a &desktop shortcut"
Name: "opttools"; Description: "Install optional tools: Ollama (AI lyrics) and MuseScore (piano sheets) - internet needed, several GB"

[Files]
Source: "dist\MusicStudio\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "guitar.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName} - Repair setup"; Filename: "{app}\MusicStudio.exe"; Parameters: "--setup"
Name: "{autoprograms}\{#AppName} - Start ACE-Step (optional, for AI Song)"; Filename: "{app}\start_ace_step.bat"; Check: FileExists(ExpandConstant('{app}\start_ace_step.bat'))
Name: "{autoprograms}\{#AppName} - Install Ollama + MuseScore (optional)"; Filename: "{app}\install_optional_tools.bat"; Check: FileExists(ExpandConstant('{app}\install_optional_tools.bat'))
Name: "{autoprograms}\{#AppName} - Install ACE-Step (optional)"; Filename: "{app}\install_ace_step.bat"; Check: FileExists(ExpandConstant('{app}\install_ace_step.bat'))
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\MusicStudio.exe"; IconFilename: "{app}\guitar.ico"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\MusicStudio.exe"; IconFilename: "{app}\guitar.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\install_optional_tools.bat"; Parameters: "gemma3:4b /nopause"; WorkingDir: "{app}"; Tasks: opttools; StatusMsg: "Installing Ollama and MuseScore (can take several minutes)..."; Flags: waituntilterminated; Check: FileExists(ExpandConstant('{app}\install_optional_tools.bat'))
Filename: "{app}\MusicStudio.exe"; Description: "Start {#AppName} now (first start sets up all components)"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; the downloaded components are removed. The user's own data is kept on purpose:
; users.json (+ users.json.key), uploads, outputs, jobs, secret_key.txt and lyrics_ai.env
; now live directly in {localappdata}\MusicStudio (older versions kept them in ...\MusicStudio\app;
; the app moves them up automatically on first start).
Type: filesandordirs; Name: "{localappdata}\MusicStudio\venv"
Type: filesandordirs; Name: "{localappdata}\MusicStudio\python"
Type: filesandordirs; Name: "{localappdata}\MusicStudio\bin"
Type: files; Name: "{localappdata}\MusicStudio\setup_state.json"
