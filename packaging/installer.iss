; Inno Setup script — SigmaCode Desk (per-user install, no admin rights needed)
#define AppName "SigmaCode Desk"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6E4B1C2A-5F0D-4C47-9C1E-51C0DE5D3E57}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=SigmaCode
DefaultDirName={localappdata}\Programs\SigmaCodeDesk
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=SigmaCodeDesk-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\SigmaCodeDesk.exe
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
ShowLanguageDialog=auto

[Languages]
#ifexist "Arabic.isl"
Name: "ar"; MessagesFile: "Arabic.isl"
#endif
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\SigmaCodeDesk\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\SigmaCodeDesk.exe"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\SigmaCodeDesk.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\SigmaCodeDesk.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM llama-server.exe /T"; Flags: runhidden; RunOnceId: "StopLlama"
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM SigmaCodeDesk.exe /T"; Flags: runhidden; RunOnceId: "StopApp"
; User data (%LOCALAPPDATA%\SigmaCodeDesk: documents, index, models) is kept on uninstall.
