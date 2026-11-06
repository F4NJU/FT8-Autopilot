; Compile after build-integrated.ps1 with Inno Setup 6:
;   ISCC.exe /DAppSourceDir="...\dist\WSJTX-AutoPilot" packaging\WSJTX-AutoPilot.iss

#ifndef AppSourceDir
  #error AppSourceDir must point to dist\WSJTX-AutoPilot
#endif

[Setup]
AppId={{F8C1E36B-D6B9-40A2-A9D0-76B0C2AB89B8}
AppName=WSJTX AutoPilot
AppVersion=1.0.2
AppPublisher=F4NJU
DefaultDirName={autopf}\WSJTX-AutoPilot
DefaultGroupName=WSJTX AutoPilot
OutputDir=..\dist
OutputBaseFilename=WSJTX-AutoPilot-Setup
Compression=lzma2/ultra64
SolidCompression=yes
LicenseFile=..\LICENSE
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\WSJTX-AutoPilot.exe

[Files]
Source: "{#AppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\WSJTX AutoPilot"; Filename: "{app}\WSJTX-AutoPilot.exe"
Name: "{autodesktop}\WSJTX AutoPilot"; Filename: "{app}\WSJTX-AutoPilot.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Créer un raccourci sur le bureau"; Flags: unchecked

[Run]
Filename: "{app}\WSJTX-AutoPilot.exe"; Description: "Lancer WSJTX AutoPilot"; Flags: nowait postinstall skipifsilent
