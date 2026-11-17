; Nil Psikoloji - Klinik installer (Inno Setup 6). Built by scripts/build_installer.py,
; which passes /DStage=<staged files> /DAppVersion=<x.y.z> /DOutDir=<dist>.
;
; Two kinds of installation:
;   - office computer: the main installation (database, backups, daily snapshot, served
;     to the office network over HTTPS for the owner's laptop to sync with);
;   - owner's laptop: a copy that works offline and syncs with the office (needs the
;     pairing file made at the office: Ayarlar > Dizustu kopya).

#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif

[Setup]
AppId={{6E7A1C52-4B8D-4F1A-9C3E-2D5B7A9E1F40}
AppName=Nil Psikoloji Klinik
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
OutputBaseFilename=NilPsikoloji-Klinik-Kurulum-{#AppVersion}
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
Filename: "{commondesktop}\Nil Psikoloji Klinik.url"; Section: "InternetShortcut"; Key: "URL"; String: "https://localhost:8443"
Filename: "{commondesktop}\Nil Psikoloji Klinik.url"; Section: "InternetShortcut"; Key: "IconFile"; String: "{app}\assets\klinik.ico"
Filename: "{commondesktop}\Nil Psikoloji Klinik.url"; Section: "InternetShortcut"; Key: "IconIndex"; String: "0"
Filename: "{group}\Nil Psikoloji Klinik.url"; Section: "InternetShortcut"; Key: "URL"; String: "https://localhost:8443"

[Icons]
Name: "{group}\Kayıtlar (log) klasörü"; Filename: "{commonappdata}\Nil Psikoloji\Klinik\logs"

[InstallDelete]
; Shortcut of the earlier (cloud) version.
Type: files; Name: "{commondesktop}\Klinik Kasa.url"
Type: files; Name: "{group}\Klinik Kasa.url"

[Run]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\setup\setup.ps1"" -Params ""{tmp}\klinik-params.txt"""; \
  StatusMsg: "Veritabanı ve servisler kuruluyor..."; Flags: waituntilterminated
Filename: "https://localhost:8443"; Description: "Programı aç"; Flags: postinstall shellexec nowait skipifsilent

[UninstallRun]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\setup\uninstall.ps1"" -App ""{app}"""; \
  Flags: waituntilterminated runhidden; RunOnceId: "RemoveServices"

[UninstallDelete]
Type: files; Name: "{commondesktop}\Nil Psikoloji Klinik.url"
Type: filesandordirs; Name: "{app}\services"

[Code]
var
  ModePage: TInputOptionWizardPage;
  AdminPage: TInputQueryWizardPage;
  DrivePage: TInputOptionWizardPage;
  PairingFilePage: TInputFileWizardPage;
  PairingPassPage: TInputQueryWizardPage;

function DataDir(): String;
begin
  Result := ExpandConstant('{commonappdata}\Nil Psikoloji\Klinik');
end;

function IsUpgrade(): Boolean;
begin
  Result := FileExists(DataDir() + '\pgdata\PG_VERSION');
end;

function IsLaptop(): Boolean;
begin
  Result := (not IsUpgrade()) and ModePage.Values[1];
end;

procedure InitializeWizard();
begin
  ModePage := CreateInputOptionPage(wpSelectDir,
    'Kurulum türü',
    'Bu bilgisayar hangisi?',
    'Program ofis bilgisayarında çalışır. Klinik sahibinin dizüstü bilgisayarına, ofis ' +
    'ağından uzaktayken de çalışan ve ofise bağlanınca eşitlenen bir kopya kurulabilir.',
    True, False);
  ModePage.Add('Ofis bilgisayarı (ana kurulum)');
  ModePage.Add('Klinik sahibinin dizüstü bilgisayarı (ofisle eşitlenen kopya)');
  ModePage.Values[0] := True;

  AdminPage := CreateInputQueryPage(ModePage.ID,
    'Yönetici hesabı',
    'Programın ilk kullanıcısı: klinik sahibi',
    'Yönetici her şeyi görür ve terapistlerin hesaplarını programın Ayarlar > Kullanıcılar ' +
    'sayfasından ekler. Geçici şifre, kurulum sonunda açılan anahtar sayfasında yazar.');
  AdminPage.Add('Ad soyad:', False);
  AdminPage.Add('Kullanıcı adı (ör. nil.yilmaz):', False);

  DrivePage := CreateInputOptionPage(AdminPage.ID,
    'Google Drive',
    'Yedekler ve günlük özet',
    'Şifreli yedekler ve her gün 17:00''de oluşturulan şifreli özet dosyaları kliniğin ' +
    'Google Drive''ındaki "Nil Psikoloji" klasörüne kopyalanır. Kurulum sırasında bir tarayıcı ' +
    'penceresi açılır; kliniğin Google hesabıyla giriş yapıp izin verin.',
    False, False);
  DrivePage.Add('Google Drive''a bağla (önerilir)');
  DrivePage.Values[0] := True;

  PairingFilePage := CreateInputFilePage(DrivePage.ID,
    'Eşleştirme dosyası',
    'Ofis bilgisayarında oluşturulan dosya',
    'Ofis bilgisayarındaki programda Ayarlar > Dizüstü kopya sayfasından eşleştirme dosyasını ' +
    'indirin ve bu bilgisayara (ör. USB bellekle) getirin.');
  PairingFilePage.Add('Eşleştirme dosyası:', 'Eşleştirme dosyası|*.json|Tüm dosyalar|*.*', '.json');

  PairingPassPage := CreateInputQueryPage(PairingFilePage.ID,
    'Eşleştirme parolası',
    'Dosya oluşturulurken seçilen parola',
    'Kurulum için dizüstü bilgisayar ofis ağına bağlı olmalıdır: kullanıcılar ve kayıtlar ' +
    'ilk eşitlemeyle ofisten gelir.');
  PairingPassPage.Add('Parola:', True);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  { An upgrade keeps its kind and its users; only the Drive choice may still change. }
  if IsUpgrade() and ((PageID = ModePage.ID) or (PageID = AdminPage.ID) or
     (PageID = PairingFilePage.ID) or (PageID = PairingPassPage.ID)) then
    Result := True
  else if IsLaptop() and ((PageID = AdminPage.ID) or (PageID = DrivePage.ID)) then
    Result := True
  else if (not IsLaptop()) and ((PageID = PairingFilePage.ID) or (PageID = PairingPassPage.ID)) then
    Result := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = AdminPage.ID) and not IsUpgrade() then
  begin
    if (Trim(AdminPage.Values[0]) = '') or (Length(Trim(AdminPage.Values[1])) < 3) then
    begin
      MsgBox('Ad soyad ve en az 3 karakterlik bir kullanıcı adı girin.', mbError, MB_OK);
      Result := False;
    end;
  end;
  if (CurPageID = PairingFilePage.ID) and IsLaptop() then
  begin
    if (PairingFilePage.Values[0] = '') or not FileExists(PairingFilePage.Values[0]) then
    begin
      MsgBox('Eşleştirme dosyasını seçin.', mbError, MB_OK);
      Result := False;
    end;
  end;
  if (CurPageID = PairingPassPage.ID) and IsLaptop() then
  begin
    if Length(PairingPassPage.Values[0]) < 10 then
    begin
      MsgBox('Parola en az 10 karakterdir.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Lines: TArrayOfString;
  Drive, Mode: String;
begin
  if CurStep = ssInstall then
  begin
    if DrivePage.Values[0] then Drive := '1' else Drive := '0';
    if IsLaptop() then Mode := 'laptop' else Mode := 'office';
    SetArrayLength(Lines, 8);
    Lines[0] := 'app=' + ExpandConstant('{app}');
    Lines[1] := 'data=' + DataDir();
    Lines[2] := 'mode=' + Mode;
    Lines[3] := 'admin_name=' + Trim(AdminPage.Values[0]);
    Lines[4] := 'admin_user=' + Lowercase(Trim(AdminPage.Values[1]));
    Lines[5] := 'drive=' + Drive;
    Lines[6] := 'pairing_file=' + PairingFilePage.Values[0];
    Lines[7] := 'pairing_pass=' + PairingPassPage.Values[0];
    SaveStringsToUTF8File(ExpandConstant('{tmp}\klinik-params.txt'), Lines, False);
  end;
end;
