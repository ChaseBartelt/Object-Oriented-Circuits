; Build with build_installer.py. Runtime and dependencies are bundled, not downloaded by Setup.
#ifndef AppVersion
  #error AppVersion is required
#endif
#define Bundle "versions\" + AppVersion

[Setup]
AppId={{96DCC84C-C824-45CC-A49E-BC6AA67B39C2}
AppName=Object-Oriented Circuits
AppVersion={#AppVersion}
AppPublisher=EchoingFarts
AppPublisherURL=https://github.com/EchoingFarts/Object-Oriented-Circuits
AppUpdatesURL=https://github.com/EchoingFarts/Object-Oriented-Circuits/releases
DefaultDirName={localappdata}\Programs\OOC
DefaultGroupName=Object-Oriented Circuits
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
WizardStyle=modern
Compression=lzma2/fast
SolidCompression=yes
CloseApplications=no
RestartApplications=no
SetupLogging=yes
CreateUninstallRegKey=not IsPortable
LicenseFile={#SourceRoot}\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename=OOC-Setup-{#AppVersion}-windows-x64
UninstallDisplayName=Object-Oriented Circuits
UninstallDisplayIcon={app}\{#Bundle}\runtime\pythonw.exe
ChangesAssociations=no

[Tasks]
Name: "vscode"; Description: "Install or update the OOC extension in VS Code"; Flags: checkedonce
Name: "desktopicon"; Description: "Create a desktop shortcut for the viewer"; Flags: unchecked

[Files]
Source: "{#Payload}\*"; DestDir: "{app}\{#Bundle}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\OOC Viewer"; Filename: "{app}\{#Bundle}\runtime\pythonw.exe"; Parameters: "-m circuitlab --editor"; WorkingDir: "{userdocs}"; Check: not IsPortable
Name: "{group}\OOC Getting Started"; Filename: "{app}\{#Bundle}\Getting-Started.txt"; Check: not IsPortable
Name: "{group}\Uninstall OOC"; Filename: "{uninstallexe}"; Check: not IsPortable
Name: "{autodesktop}\OOC Viewer"; Filename: "{app}\{#Bundle}\runtime\pythonw.exe"; Parameters: "-m circuitlab --editor"; Tasks: desktopicon; Check: not IsPortable

[Run]
Filename: "{app}\{#Bundle}\runtime\pythonw.exe"; Parameters: "-m circuitlab --editor"; Description: "Open the OOC viewer"; Flags: postinstall nowait skipifsilent unchecked
Filename: "https://code.visualstudio.com/download"; Description: "Download VS Code (not found on this computer)"; Flags: shellexec postinstall skipifsilent unchecked; Check: CodeMissing

[UninstallDelete]
Type: files; Name: "{app}\install.json"
Type: files; Name: "{app}\install.pending.json"
Type: files; Name: "{app}\update.log"
Type: files; Name: "{app}\extension-install.log"
Type: files; Name: "{app}\updates\OOC-Setup-*-windows-x64.exe"
Type: dirifempty; Name: "{app}\updates"

[Code]
var
  CodePage: TInputFileWizardPage;
  ExtensionFailed: Boolean;

function IsPortable: Boolean;
begin
  Result := ExpandConstant('{param:PORTABLE|0}') = '1';
end;

function FindCode: String;
var Candidate: String;
begin
  Result := ExpandConstant('{param:CODE|}');
  if Result <> '' then exit;
  Candidate := ExpandConstant('{localappdata}\Programs\Microsoft VS Code\Code.exe');
  if FileExists(Candidate) then begin Result := Candidate; exit; end;
  Candidate := ExpandConstant('{pf64}\Microsoft VS Code\Code.exe');
  if FileExists(Candidate) then Result := Candidate;
end;

function CodeMissing: Boolean;
begin
  Result := not FileExists(CodePage.Values[0]);
end;

procedure InitializeWizard;
begin
  CodePage := CreateInputFilePage(wpSelectTasks, 'VS Code integration',
    'Choose your VS Code installation',
    'OOC includes its own Python and chip library. If VS Code is not installed, the viewer can still be installed now. Install VS Code, then run this installer again to add the extension.');
  CodePage.Add('VS Code executable:', 'VS Code|Code.exe', '.exe');
  CodePage.Values[0] := FindCode;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = CodePage.ID) and not WizardIsTaskSelected('vscode');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = CodePage.ID) and (CodePage.Values[0] <> '') and CodeMissing then begin
    MsgBox('Choose Code.exe, or clear the field to install the viewer now.', mbError, MB_OK);
    Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var Python, Arguments, Manifest, Extra, Code: String; ExitCode: Integer;
begin
  if CurStep <> ssPostInstall then exit;
  Python := ExpandConstant('{app}\{#Bundle}\runtime\python.exe');
  Manifest := ExpandConstant('{app}\{#Bundle}\install.json');
  Arguments := '-m circuitlab.tooling.distribution activate --root ' + AddQuotes(ExpandConstant('{app}')) + ' --manifest ' + AddQuotes(Manifest);
  if not Exec(Python, Arguments, '', SW_HIDE, ewWaitUntilTerminated, ExitCode) or (ExitCode <> 0) then
    RaiseException('OOC installation validation failed. The previous active installation was preserved.');
  if WizardIsTaskSelected('vscode') then begin
    if CodeMissing then begin
      ExtensionFailed := True;
      Log('VS Code was not found; the extension is bundled but was not installed.');
    end else begin
      Code := CodePage.Values[0];
      Arguments := '-m circuitlab.tooling.distribution install-extension --code ' + AddQuotes(Code) +
        ' --vsix ' + AddQuotes(ExpandConstant('{app}\{#Bundle}\extension\ooc-language-{#ExtensionVersion}.vsix')) +
        ' --log ' + AddQuotes(ExpandConstant('{app}\extension-install.log'));
      Extra := ExpandConstant('{param:CODEUSERDATA|}');
      if Extra <> '' then Arguments := Arguments + ' --user-data ' + AddQuotes(Extra);
      Extra := ExpandConstant('{param:CODEEXTENSIONS|}');
      if Extra <> '' then Arguments := Arguments + ' --extensions-dir ' + AddQuotes(Extra);
      ExtensionFailed := not Exec(Python, Arguments, '', SW_HIDE, ewWaitUntilTerminated, ExitCode) or (ExitCode <> 0);
      if ExtensionFailed then Log('VS Code extension installation failed; rerun Setup to retry.');
    end;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and ExtensionFailed then
    WizardForm.FinishedLabel.Caption := 'The OOC compiler, viewer and chip library are installed. The VS Code extension could not be installed. Install or close VS Code and rerun Setup, or install the bundled VSIX manually.';
end;
