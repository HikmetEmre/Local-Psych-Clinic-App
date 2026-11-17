; Nil Psikoloji - Klinik Kasa installer (Inno Setup 6). Built by scripts/build_installer.py,
; which passes /DStage=<staged files> /DAppVersion=<x.y.z> /DOutDir=<dist>.

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{6E7A1C52-4B8D-4F1A-9C3E-2D5B7A9E1F40}
AppName=Nil Psikoloji Klinik Kasa
AppVersion={#AppVersion}
AppPublisher=Nil Psikoloji
DefaultDirName={autopf}\Nil Psikoloji\Klinik Kasa
DefaultGroupName=Nil Psikoloji
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
OutputDir={#OutDir}
OutputBaseFilename=NilPsikoloji-KlinikKasa-Kurulum-{#AppVersion}
SetupIconFile={#Stage}\assets\klinik.ico
UninstallDisplayIcon={app}\assets\klinik.ico
WizardStyle=modern
WizardImageFile={#Stage}\assets\wizard.bmp
WizardSmallImageFile={#Stage}\assets\wizard-small.bmp
Compression=lzma2/ultra64
SolidCompression=yes
CloseApplications=no

[Languages]
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"

[Files]
Source: "{#Stage}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[INI]
Filename: "{commondesktop}\Klinik Kasa.url"; Section: "InternetShortcut"; Key: "URL"; String: "http://127.0.0.1:8090"
Filename: "{commondesktop}\Klinik Kasa.url"; Section: "InternetShortcut"; Key: "IconFile"; String: "{app}\assets\klinik.ico"
Filename: "{commondesktop}\Klinik Kasa.url"; Section: "InternetShortcut"; Key: "IconIndex"; String: "0"
Filename: "{group}\Klinik Kasa.url"; Section: "InternetShortcut"; Key: "URL"; String: "http://127.0.0.1:8090"

[Icons]
Name: "{group}\Kayıtlar (log) klasörü"; Filename: "{commonappdata}\Nil Psikoloji\Klinik\logs"

[Run]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\setup\setup.ps1"" -Params ""{tmp}\klinik-params.txt"""; \
  StatusMsg: "Veritabanı ve servisler kuruluyor..."; Flags: waituntilterminated
Filename: "http://127.0.0.1:8090"; Description: "Klinik Kasa'yı aç"; Flags: postinstall shellexec nowait skipifsilent

[UninstallRun]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\setup\uninstall.ps1"" -App ""{app}"""; \
  Flags: waituntilterminated runhidden; RunOnceId: "RemoveServices"

[UninstallDelete]
Type: files; Name: "{commondesktop}\Klinik Kasa.url"
Type: filesandordirs; Name: "{app}\services"

[Code]
var
  CloudPage: TInputQueryWizardPage;
  UsersPage: TInputFileWizardPage;
  DrivePage: TInputOptionWizardPage;

function DataDir(): String;
begin
  Result := ExpandConstant('{commonappdata}\Nil Psikoloji\Klinik');
end;

function IsUpgrade(): Boolean;
begin
  Result := FileExists(DataDir() + '\pgdata\PG_VERSION');
end;

procedure InitializeWizard();
begin
  CloudPage := CreateInputQueryPage(wpSelectDir,
    'Klinik Web bağlantısı',
    'Randevuların bulut takvimden bu bilgisayara gelmesi için',
    'Klinik Web adresini ve bulutta oluşturulan eşitleme anahtarını girin. ' +
    'Şimdi bilmiyorsanız boş bırakın; daha sonra kurulumu tekrar çalıştırıp ekleyebilirsiniz.');
  CloudPage.Add('Klinik Web adresi (ör. https://web.nilpsikoloji.com):', False);
  CloudPage.Add('Eşitleme anahtarı (sync_...):', True);

  UsersPage := CreateInputFilePage(CloudPage.ID,
    'Kullanıcılar',
    'Terapistlerin hesapları',
    'Klinik Web kurulurken kullanılan kullanıcı dosyasını (users.toml) seçin. ' +
    'Aynı dosya kullanılınca kullanıcılar iki sistemde de aynı kimliğe sahip olur.');
  UsersPage.Add('Kullanıcı dosyası:', 'TOML dosyası|*.toml|Tüm dosyalar|*.*', '.toml');

  DrivePage := CreateInputOptionPage(UsersPage.ID,
    'Google Drive',
    'Yedekler ve günlük özet',
    'Şifreli yedekler ve her gün 17:00''de oluşturulan şifreli özet dosyaları kliniğin ' +
    'Google Drive''ındaki "Nil Psikoloji" klasörüne kopyalanır. Kurulum sırasında bir tarayıcı ' +
    'penceresi açılır; kliniğin Google hesabıyla giriş yapıp izin verin.',
    False, False);
  DrivePage.Add('Google Drive''a bağla (önerilir)');
  DrivePage.Values[0] := True;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  { On an upgrade users already exist; the other pages may still change settings. }
  if (PageID = UsersPage.ID) and IsUpgrade() then
    Result := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Url: String;
begin
  Result := True;
  if CurPageID = CloudPage.ID then
  begin
    Url := Trim(CloudPage.Values[0]);
    if (Url <> '') and (Pos('https://', Lowercase(Url)) <> 1) then
    begin
      MsgBox('Klinik Web adresi https:// ile başlamalı.', mbError, MB_OK);
      Result := False;
    end;
  end;
  if (CurPageID = UsersPage.ID) and not IsUpgrade() then
  begin
    if (UsersPage.Values[0] = '') or not FileExists(UsersPage.Values[0]) then
    begin
      MsgBox('İlk kurulumda kullanıcı dosyası gerekli.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Lines: TArrayOfString;
  Drive: String;
begin
  if CurStep = ssInstall then
  begin
    if DrivePage.Values[0] then Drive := '1' else Drive := '0';
    SetArrayLength(Lines, 6);
    Lines[0] := 'app=' + ExpandConstant('{app}');
    Lines[1] := 'data=' + DataDir();
    Lines[2] := 'web_url=' + Trim(CloudPage.Values[0]);
    Lines[3] := 'sync_token=' + Trim(CloudPage.Values[1]);
    Lines[4] := 'users_file=' + UsersPage.Values[0];
    Lines[5] := 'drive=' + Drive;
    SaveStringsToUTF8File(ExpandConstant('{tmp}\klinik-params.txt'), Lines, False);
  end;
end;
