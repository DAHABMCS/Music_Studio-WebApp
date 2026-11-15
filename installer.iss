; Inno Setup script (free: https://jrsoftware.org/isinfo.php). build_installer.bat runs it if Inno Setup 6 is installed.
; Keep AppVersion in sync with APP_VERSION in launcher.py.
#define AppName "Music Studio"
#define AppVersion "1.0.5"

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
Name: "usermgr"; Description: "Install the &User Manager (add / edit / delete Music Studio logins)"
Name: "opttools"; Description: "Install optional tools: Ollama (AI lyrics) and MuseScore (piano sheets) - internet needed, several GB"

[Files]
Source: "dist\MusicStudio\*"; DestDir: "{app}"; Excludes: "UserManager\*"; Flags: recursesubdirs ignoreversion
Source: "dist\MusicStudio\UserManager\*"; DestDir: "{app}\UserManager"; Flags: recursesubdirs ignoreversion; Tasks: usermgr; Check: UserMgrAllowed
Source: "guitar.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName} - Repair setup"; Filename: "{app}\MusicStudio.exe"; Parameters: "--setup"
Name: "{autoprograms}\{#AppName} - Start ACE-Step (optional, for AI Song)"; Filename: "{app}\start_ace_step.bat"; Check: FileExists(ExpandConstant('{app}\start_ace_step.bat'))
Name: "{autoprograms}\{#AppName} - Install Ollama + MuseScore (optional)"; Filename: "{app}\install_optional_tools.bat"; Check: FileExists(ExpandConstant('{app}\install_optional_tools.bat'))
Name: "{autoprograms}\{#AppName} - Install ACE-Step (optional)"; Filename: "{app}\install_ace_step.bat"; Check: FileExists(ExpandConstant('{app}\install_ace_step.bat'))
Name: "{autoprograms}\{#AppName} - User Manager"; Filename: "{app}\UserManager\Securit_Management.exe"; IconFilename: "{app}\guitar.ico"; Tasks: usermgr; Check: UserMgrAllowed
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

[Code]
const
  // SHA-256 of  'MS-salt-' + <admin password>.  Never store the plain password here.
  // Make it with:  python -c "import hashlib;print(hashlib.sha256(b'MS-salt-YourPassword').hexdigest())"
  AdminPassHash = 'f6e5ff547d5a47ff234d9a5e91f57ee2eed7df94375fdbbec7056fef90fc4c7f';

var
  PwPage: TInputQueryWizardPage;
  PwOK: Boolean;

function PassMatches(const Pw: String): Boolean;
begin
  Result := CompareText(GetSHA256OfString(Pw), AdminPassHash) = 0;
end;

procedure InitializeWizard;
begin
  PwOK := False;
  PwPage := CreateInputQueryPage(wpSelectTasks,
    'Administrator authorization',
    'A password is required to install the User Manager.',
    'Enter the administrator password:');
  PwPage.Add('Password:', True);
  // silent installs have no wizard pages: they must pass /ADMINPASS=xxxx
  if WizardSilent then
    PwOK := PassMatches(ExpandConstant('{param:ADMINPASS|}'));
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if (PwPage <> nil) and (PageID = PwPage.ID) then
    Result := (not WizardIsTaskSelected('usermgr')) or PwOK;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (PwPage <> nil) and (CurPageID = PwPage.ID) then
  begin
    PwOK := PassMatches(PwPage.Values[0]);
    if not PwOK then
    begin
      MsgBox('Incorrect password.', mbError, MB_OK);
      PwPage.Values[0] := '';
      Result := False;
    end;
  end;
end;

// used by the [Files] and [Icons] entries of the User Manager
function UserMgrAllowed: Boolean;
begin
  Result := PwOK;
end;
