; Inno Setup script for the per-user Windows installer.
; Built in CI: iscc release\windows_installer.iss  (driven by env vars).
#define AppName    GetEnv("PDFTE_APP_NAME")
#define AppVersion GetEnv("PDFTE_APP_VERSION")
#define SrcDir     GetEnv("PDFTE_SRC_DIR")
#define BundleId   GetEnv("PDFTE_BUNDLE_ID")
#define OutName    GetEnv("PDFTE_OUT_NAME")

[Setup]
AppId={#BundleId}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=.
OutputBaseFilename={#OutName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppName}.exe"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
