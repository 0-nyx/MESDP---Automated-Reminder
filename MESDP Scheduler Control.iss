[Setup]
AppId={{9A4C1B35-5CB5-4F73-9E4E-B1EBA988F4DF}
AppName=CLL MESDP Ticketing Worklog
AppVersion=1.0.0
AppPublisher=Jijoi.Inc
DefaultDirName={autopf}\CLL\MESDP Scheduler Control
DefaultGroupName=CLL MESDP Ticketing Worklog
OutputDir=dist
OutputBaseFilename=MESDP_Scheduler_Control_Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=dist\app_icon.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "dist\MESDP Scheduler Control\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\CLL MESDP Ticketing Worklog"; Filename: "{app}\MESDP Scheduler Control.exe"; IconFilename: "{app}\_internal\app_icon.ico"
Name: "{autodesktop}\CLL MESDP Ticketing Worklog"; Filename: "{app}\MESDP Scheduler Control.exe"; IconFilename: "{app}\_internal\app_icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\MESDP Scheduler Control.exe"; Description: "Launch CLL MESDP Ticketing Worklog"; Flags: nowait postinstall skipifsilent

[Code]
var
	MaintenancePage: TInputOptionWizardPage;
	ExistingInstallFound: Boolean;
	ExistingUninstallCmd: string;
	ExistingInstallDir: string;
	SelectedMode: string;

function TryGetExistingInstallInfo(var UninstallCmd: string; var InstallDir: string): Boolean;
var
	KeyPath: string;
begin
	Result := False;
	UninstallCmd := '';
	InstallDir := '';
	KeyPath := 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{9A4C1B35-5CB5-4F73-9E4E-B1EBA988F4DF}_is1';

	if RegQueryStringValue(HKLM64, KeyPath, 'UninstallString', UninstallCmd) then
		Result := True
	else if RegQueryStringValue(HKLM, KeyPath, 'UninstallString', UninstallCmd) then
		Result := True
	else if RegQueryStringValue(HKCU, KeyPath, 'UninstallString', UninstallCmd) then
		Result := True;

	if Result then
	begin
		if not RegQueryStringValue(HKLM64, KeyPath, 'Inno Setup: App Path', InstallDir) then
			if not RegQueryStringValue(HKLM, KeyPath, 'Inno Setup: App Path', InstallDir) then
				RegQueryStringValue(HKCU, KeyPath, 'Inno Setup: App Path', InstallDir);
	end;
end;

procedure InitializeWizard;
begin
	SelectedMode := 'patch';
	ExistingInstallFound := TryGetExistingInstallInfo(ExistingUninstallCmd, ExistingInstallDir);

	if not ExistingInstallFound then
		exit;

	MaintenancePage := CreateInputOptionPage(
		wpWelcome,
		'Installation Mode',
		'Existing installation detected',
		'Choose how you want to proceed for this update.',
		True,
		False);

	MaintenancePage.Add('Patch (recommended) - update changed files and keep your settings');
	MaintenancePage.Add('Repair - reinstall all packaged files and keep your settings');
	MaintenancePage.Add('Re-install - uninstall current app first, then install fresh');
	MaintenancePage.Add('Uninstall - remove current app and exit setup');
	MaintenancePage.SelectedValueIndex := 0;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
	ExecCode: Integer;
	Cmd: string;
	Params: string;
begin
	Result := True;

	if ExistingInstallFound and (CurPageID = MaintenancePage.ID) then
	begin
		case MaintenancePage.SelectedValueIndex of
			0: SelectedMode := 'patch';
			1: SelectedMode := 'repair';
			2: SelectedMode := 'reinstall';
			3: SelectedMode := 'uninstall';
		else
			SelectedMode := 'patch';
		end;

		if SelectedMode = 'uninstall' then
		begin
			if ExistingUninstallCmd = '' then
			begin
				MsgBox('Uninstall command not found for existing installation.', mbError, MB_OK);
				Result := False;
				exit;
			end;

			if MsgBox('This will uninstall the current app and close setup. Continue?', mbConfirmation, MB_YESNO) <> IDYES then
			begin
				Result := False;
				exit;
			end;

			Cmd := RemoveQuotes(ExistingUninstallCmd);
			Params := '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART';
			if Exec(Cmd, Params, '', SW_SHOW, ewWaitUntilTerminated, ExecCode) and (ExecCode = 0) then
			begin
				MsgBox('Uninstall completed. Setup will now close.', mbInformation, MB_OK);
				WizardForm.Close;
			end
			else
			begin
				MsgBox('Uninstall failed. Please close running app/processes and try again.', mbError, MB_OK);
			end;

			Result := False;
			exit;
		end;
	end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
	ExecCode: Integer;
	Cmd: string;
	Params: string;
begin
	if CurStep = ssInstall then
	begin
		{ Kill any running instance before installing }
		Exec('taskkill.exe', '/F /IM "MESDP Scheduler Control.exe"', '', SW_HIDE, ewNoWait, ExecCode);
		Sleep(500);
	end;

	if (CurStep <> ssInstall) or (SelectedMode <> 'reinstall') then
		exit;

	if ExistingUninstallCmd = '' then
		exit;

	Cmd := RemoveQuotes(ExistingUninstallCmd);
	Params := '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART';

	if not (Exec(Cmd, Params, '', SW_SHOW, ewWaitUntilTerminated, ExecCode) and (ExecCode = 0)) then
		MsgBox('Re-install mode could not uninstall previous version automatically. Setup will continue and overwrite files instead.', mbInformation, MB_OK);
end;
