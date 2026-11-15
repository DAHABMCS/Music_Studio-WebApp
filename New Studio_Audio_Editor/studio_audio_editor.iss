; Studio audio Editor installer (standalone one-file exe)
; Compile via build_standalone.bat, or open in Inno Setup after building dist\studio_audio_editor.exe

#define MyAppName "Studio audio Editor"
#define MyAppVersion "1.5"
#define MyAppPublisher "ABOULDAHABMCS"
#define MyAppExeName "studio_audio_editor.exe"

#define MyAppAssocName MyAppName + " File"
#define MyAppAssocExt ".myp"
#define MyAppAssocKey StringChange(MyAppAssocName, " ", "") + MyAppAssocExt

; Icon: prefer music_icon_white_bg.ico next to this script, then icon.ico, else the old G:\ path, else none.
#if FileExists(SourcePath + "music_icon_white_bg.ico")
  #define MyIcon SourcePath + "music_icon_white_bg.ico"
#elif FileExists(SourcePath + "icon.ico")
  #define MyIcon SourcePath + "icon.ico"
#elif FileExists("G:\music_icon_white_bg.ico")
  #define MyIcon "G:\music_icon_white_bg.ico"
#else
  #define MyIcon ""
#endif

[Setup]
; Unique AppId - do not reuse in other installers.
AppId={{AC10A2EF-A769-422C-90E5-150B9EC91D46}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}

; 64-bit only (x64 + Windows 11 on Arm)
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Output: <script folder>\Output\Studio_audio_Editor_Setup.exe
OutputDir=Output
OutputBaseFilename=Studio_audio_Editor_Setup

#if MyIcon != ""
SetupIconFile={#MyIcon}
#endif

SolidCompression=yes
ChangesAssociations=yes
DisableProgramGroupPage=yes
WizardStyle=modern dark

; Password + encryption (uncomment the next 2 lines to require a password to install)
;Password=2348
;Encryption=yes

; Uncomment to install per-user only (no admin):
;PrivilegesRequired=lowest

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
#if FileExists(CompilerPath + "Languages\Arabic.isl")
Name: "arabic";  MessagesFile: "compiler:Languages\Arabic.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Single standalone exe (ffmpeg is bundled inside it)
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
Root: HKA; Subkey: "Software\Classes\{#MyAppAssocExt}\OpenWithProgids"; ValueType: string; ValueName: "{#MyAppAssocKey}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\{#MyAppAssocKey}"; ValueType: string; ValueName: ""; ValueData: "{#MyAppAssocName}"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\{#MyAppAssocKey}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"
Root: HKA; Subkey: "Software\Classes\{#MyAppAssocKey}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent