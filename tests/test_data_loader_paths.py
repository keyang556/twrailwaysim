from __future__ import annotations

from pathlib import Path

from railway_sim import data_loader


def _make_data_dir(root: Path, *, complete: bool = True) -> Path:
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    names = data_loader._REQUIRED_FILES if complete else ("stations.json",)
    for name in names:
        (data_dir / name).write_text("{}", encoding="utf-8")
    return data_dir


def test_default_data_dir_uses_pyinstaller_bundle(monkeypatch, tmp_path: Path) -> None:
    bundled_data = _make_data_dir(tmp_path)
    monkeypatch.delenv(data_loader.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(data_loader.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert data_loader.default_data_dir() == bundled_data


def test_default_data_dir_ignores_incomplete_app_adjacent_data(
    monkeypatch, tmp_path: Path
) -> None:
    app_directory = tmp_path / "app"
    app_directory.mkdir()
    executable = app_directory / "twrailwaysim.exe"
    executable.touch()
    _make_data_dir(app_directory, complete=False)

    bundled_root = tmp_path / "bundle"
    bundled_data = _make_data_dir(bundled_root)

    monkeypatch.delenv(data_loader.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(data_loader.sys, "frozen", True, raising=False)
    monkeypatch.setattr(data_loader.sys, "executable", str(executable))
    monkeypatch.setattr(data_loader.sys, "_MEIPASS", str(bundled_root), raising=False)

    assert data_loader.default_data_dir() == bundled_data
