; Inno Setup script - builds 360LiveMods-Setup-<version>.exe from the PyInstaller output.
; Built automatically by GitHub Actions; to build by hand: iscc /DAppVersion=1.0.0 installer\360LiveMods.iss

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{CA57EF03-DCEE-475D-993C-65FE28E30DB0}
AppName=360 LiveMods
AppVersion={#AppVersion}
AppPublisher=360 LiveMods contributors
DefaultDirName={localappdata}\Programs\360 LiveMods
DefaultGroupName=360 LiveMods
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=360LiveMods-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\360LiveMods.exe
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\LICENSE

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\360LiveMods\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\360 LiveMods"; Filename: "{app}\360LiveMods.exe"
Name: "{group}\Uninstall 360 LiveMods"; Filename: "{uninstallexe}"
Name: "{userdesktop}\360 LiveMods"; Filename: "{app}\360LiveMods.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\360LiveMods.exe"; Description: "Launch 360 LiveMods"; Flags: nowait postinstall skipifsilent
