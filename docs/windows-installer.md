# Windows installer

The Windows installer is built with PyInstaller and Inno Setup. Install the
`installer` extra and Inno Setup, then run:

```powershell
python -m pip install ".[installer]"
.\scripts\build-installer.ps1
```

The resulting installer is written to
`dist\installer\twrailwaysim-setup-<version>.exe`.

To build and validate only the portable application when Inno Setup is not
available, add `-SkipInstaller` to the PowerShell command.

If Inno Setup is installed outside the system path, pass its `ISCC.exe` with
`-InnoCompiler <path-to-ISCC.exe>`.

The installer uses a per-user application directory. It creates dedicated
Console and wx executables and shortcuts; the Console shortcut is the default,
accessibility-tested workflow.

Bundled game data is replaced with each application update. To customize it,
copy the source `data` directory to a writable location and pass that path with
`--data-dir`, or set `RAILWAY_SIM_DATA_DIR`.
