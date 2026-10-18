
; STANDALONE installer for the User Manager only (optional - the normal route is installer.iss,
; which already includes it). Build AFTER:  pyinstaller --noconfirm --clean Securit_Management.spec
; Run from the project root:  ISCC.exe Security_Management.iss
#define MyAppName "Music Studio User Manager"
#define MyAppVersion "1.5"
#define MyAppPublisher "ABOUELDAHABMCS"
#define MyAppExeName "Securit_Management.exe"

[Setup]
AppId={{11C236B4-EAE3-41D0-9E01-80F37B183556}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\MusicStudioUserManager
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=installer_output
OutputBaseFilename=MusicStudio_UserManager_Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\Securit_Management\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

; users.json and users.json.key live in %LOCALAPPDATA%\MusicStudio and are deliberately NOT removed on uninstall.
