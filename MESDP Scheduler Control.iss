[Setup]
AppId={{9A4C1B35-5CB5-4F73-9E4E-B1EBA988F4DF}
AppName=CLL MESDP Ticketing Worklog
AppVersion=1.0.0
AppPublisher=CLL
DefaultDirName={autopf}\CLL\MESDP Scheduler Control
DefaultGroupName=CLL MESDP Ticketing Worklog
OutputDir=dist
OutputBaseFilename=MESDP_Scheduler_Control_Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "dist\MESDP Scheduler Control\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\CLL MESDP Ticketing Worklog"; Filename: "{app}\MESDP Scheduler Control.exe"
Name: "{autodesktop}\CLL MESDP Ticketing Worklog"; Filename: "{app}\MESDP Scheduler Control.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\MESDP Scheduler Control.exe"; Description: "Launch CLL MESDP Ticketing Worklog"; Flags: nowait postinstall skipifsilent
