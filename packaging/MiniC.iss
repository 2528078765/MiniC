; MiniC 桌面端安装包（Inno Setup 6）
; 编译：在项目根执行  ISCC.exe packaging\MiniC.iss
; 产物：dist\MiniC-Setup-0.0.1.exe（打包 dist\MiniC.exe，需先跑 PyInstaller）

#define MyAppName "MiniC"
#define MyAppVersion "0.2"
#define MyAppPublisher "MiniC"
#define MyAppExeName "MiniC.exe"

[Setup]
AppId={{F3B0A7E2-6C1D-4E9A-9B4F-2D8C5A1E7D30}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=MiniC-Setup-0.2
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\icon\Log.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\MiniC.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
