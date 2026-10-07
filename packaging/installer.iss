; Inno Setup script for Sky Monitor (built by scripts/build-windows.ps1).
; Installs per user, no administrator rights needed; optionally starts with Windows.

#define AppName "Sky Monitor"
#define AppVersion "0.1.0"
#define Dist "..\build\sky-monitor-dist\sky-monitor"

[Setup]
AppId={{7C2E0C6A-5C1B-4E0D-9B57-3C4B0E5F1A21}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Sky Monitor contributors
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\build
OutputBaseFilename=SkyMonitor-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\sky-monitor-tray.exe
CloseApplications=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "开机时自动启动 / Start with Windows"; Flags: checkedonce

[Files]
Source: "{#Dist}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\sky-monitor-tray.exe"
Name: "{group}\{#AppName} 命令行 (console)"; Filename: "{cmd}"; Parameters: "/k ""cd /d ""{app}"" && sky-monitor.exe --help"""
Name: "{userstartup}\{#AppName}"; Filename: "{app}\sky-monitor-tray.exe"; Tasks: autostart

[Run]
Filename: "{app}\sky-monitor-tray.exe"; Description: "启动 {#AppName} / Start {#AppName}"; Flags: nowait postinstall skipifsilent
