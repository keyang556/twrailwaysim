#ifndef AppVersion
  #error AppVersion must be supplied by the build script.
#endif

#ifndef SourceDir
  #error SourceDir must be supplied by the build script.
#endif

#ifndef OutputDir
  #error OutputDir must be supplied by the build script.
#endif

#define AppName "TW Railway Simulator"
#define GuiExecutable "twrailwaysim.exe"
#define ConsoleExecutable "twrailwaysim-console.exe"

[Setup]
AppId={{437B7551-65DE-46C5-B278-34821D0F60C7}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=twrailwaysim-setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#GuiExecutable}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}\{#AppName} (Console)"; Filename: "{app}\{#ConsoleExecutable}"
Name: "{autoprograms}\{#AppName}\{#AppName} (wx)"; Filename: "{app}\{#GuiExecutable}"
Name: "{autodesktop}\{#AppName} (Console)"; Filename: "{app}\{#ConsoleExecutable}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#ConsoleExecutable}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
