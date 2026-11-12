; Build with Inno Setup 6.7+: ISCC /DSourceDir=... /DOutputDir=... /DAppVersion=... RainClassroomBot.iss
#ifndef SourceDir
  #define SourceDir "..\dist\RainClassroomBot"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\installer"
#endif
#ifndef AppVersion
  #define AppVersion "0.8.9"
#endif

[Setup]
; Stable across releases so upgrades reuse the existing installation directory.
AppId=RainClassroomBot.Desktop
AppName=雨课堂学习工作台
AppVersion={#AppVersion}
AppPublisher=RainClassroomBot Contributors
AppPublisherURL=https://github.com/ygstgddr/RainClassroomBot
AppSupportURL=https://github.com/ygstgddr/RainClassroomBot/issues
DefaultDirName={localappdata}\Programs\RainClassroomBot
DefaultGroupName=雨课堂学习工作台
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
UsePreviousAppDir=yes
DisableDirPage=no
DisableProgramGroupPage=yes
DisableWelcomePage=no
WizardStyle=modern dynamic windows11
WizardSizePercent=110
SetupIconFile=..\gui\assets\yuketang.ico
UninstallDisplayIcon={app}\RainClassroomBot.exe
InfoBeforeFile=install-notes.txt
OutputDir={#OutputDir}
OutputBaseFilename=RainClassroomBot-{#AppVersion}-Windows-x64-Setup
Compression=lzma2
SolidCompression=yes
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
Uninstallable=yes
UninstallDisplayName=雨课堂学习工作台 {#AppVersion}

[Languages]
Name: "chinesesimp"; MessagesFile: "languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
; An explicit allowlist prevents profiles, cookies and task databases entering Setup.
Source: "{#SourceDir}\RainClassroomBot.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\VERSION.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\*.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\docs\*"; DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\third_party_licenses\*"; DestDir: "{app}\third_party_licenses"; Flags: ignoreversion recursesubdirs createallsubdirs
; Settings belong to the user: create defaults only on a fresh installation.
Source: "{#SourceDir}\config\config.yaml"; DestDir: "{app}\config"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\雨课堂学习工作台"; Filename: "{app}\RainClassroomBot.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\雨课堂学习工作台"; Filename: "{app}\RainClassroomBot.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{group}\卸载雨课堂学习工作台"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\RainClassroomBot.exe"; Description: "启动雨课堂学习工作台"; Flags: nowait postinstall skipifsilent unchecked

; Deliberately no [InstallDelete]/[UninstallDelete]: runtime/user data is retained.
[Code]
function NextButtonClick(CurPageID: Integer): Boolean;
var
  ProbeName: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    { The application stores data beside its exe. Reject an unwritable target
      instead of installing successfully then failing at the first launch. }
    if not ForceDirectories(WizardDirValue) then
    begin
      MsgBox('无法创建安装目录，请选择当前用户可以写入的文件夹。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    ProbeName := WizardDirValue + '\.rain-installer-write-check.tmp';
    if FileExists(ProbeName) then
    begin
      MsgBox('安装目录中已有写入检查文件，请删除该临时文件后重试。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    Result := SaveStringToFile(ProbeName, 'write-check', False);
    if Result then
      DeleteFile(ProbeName)
    else
      MsgBox('安装目录不可写，请选择当前用户目录或其他可写文件夹。', mbError, MB_OK);
  end;
end;
