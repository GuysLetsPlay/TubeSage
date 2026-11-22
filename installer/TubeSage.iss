#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{C6488A8C-82AB-4E49-82B9-89BCF426879A}
AppName=TubeSage
AppVersion={#AppVersion}
AppPublisher=GuysLetsPlay
DefaultDirName={code:GetPreviousInstallPath}
DefaultGroupName=TubeSage
DisableProgramGroupPage=yes
UsePreviousAppDir=no
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64
OutputDir=..\dist\installer
OutputBaseFilename=TubeSage-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\TubeSage.exe

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\TubeSage\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\TubeSage"; Filename: "{app}\TubeSage.exe"
Name: "{autodesktop}\TubeSage"; Filename: "{app}\TubeSage.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\TubeSage.exe"; Description: "Launch TubeSage"; Flags: postinstall nowait skipifsilent

[Code]
function GetPreviousInstallPath(Param: string): string;
var
  PreviousPath: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER,
    'Software\Microsoft\Windows\CurrentVersion\Uninstall\{C6488A8C-82AB-4E49-82B9-89BCF426879A}_is1',
    'Inno Setup: App Path', PreviousPath) then
    if not RegQueryStringValue(HKEY_LOCAL_MACHINE,
      'Software\Microsoft\Windows\CurrentVersion\Uninstall\{C6488A8C-82AB-4E49-82B9-89BCF426879A}_is1',
      'Inno Setup: App Path', PreviousPath) then
      PreviousPath := ExpandConstant('{autopf}\TubeSage');
  Result := PreviousPath;
end;
