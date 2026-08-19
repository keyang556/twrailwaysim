from pathlib import Path


PROJECT_ROOT = Path(SPECPATH).resolve().parent
NVDA_CONTROLLER_CLIENT = (
    PROJECT_ROOT
    / "third_party"
    / "nvda-controller-client"
    / "2026.1.1"
    / "nvdaControllerClient.dll"
)
NVDA_CONTROLLER_LICENSE = NVDA_CONTROLLER_CLIENT.with_name("license.txt")

for required_file in (NVDA_CONTROLLER_CLIENT, NVDA_CONTROLLER_LICENSE):
    if not required_file.is_file():
        raise SystemExit(f"Required NVDA Controller Client file is missing: {required_file}")

a = Analysis(
    [
        str(PROJECT_ROOT / "src" / "railway_sim" / "__main__.py"),
        str(PROJECT_ROOT / "src" / "railway_sim" / "gui_launcher.py"),
    ],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=[(str(NVDA_CONTROLLER_CLIENT), "railway_sim/lib")],
    datas=[
        (str(PROJECT_ROOT / "data"), "data"),
        (str(NVDA_CONTROLLER_LICENSE), "third_party_licenses/nvda-controller-client"),
    ],
    hiddenimports=["railway_sim.ui.wx_app"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

console_script = [script for script in a.scripts if script[0] == "__main__"]
gui_script = [script for script in a.scripts if script[0] == "gui_launcher"]
if len(console_script) != 1 or len(gui_script) != 1:
    raise SystemExit("Could not identify the console and GUI entry points.")

console_exe = EXE(
    pyz,
    console_script,
    [],
    exclude_binaries=True,
    name="twrailwaysim-console",
    console=True,
)

gui_exe = EXE(
    pyz,
    gui_script,
    [],
    exclude_binaries=True,
    name="twrailwaysim",
    console=False,
)

coll = COLLECT(
    console_exe,
    gui_exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="twrailwaysim",
)
