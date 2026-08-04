"""語音後端載入安全性測試（DLL 綁架防護）。

語音是選用功能，但載入 DLL 的方式必須安全：以裸檔名呼叫 ``LoadLibrary``
會沿用 Windows 預設搜尋順序，攻擊者只要在搜尋路徑上放一個同名 DLL 就能
被載入遊戲程序。因此這裡驗證候選路徑一律為絕對路徑，且不含裸檔名。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from railway_sim.accessibility import speech


class TestTrustedDllPaths:
    def test_all_candidates_are_absolute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """候選路徑一律為絕對路徑，不得出現裸檔名。"""
        monkeypatch.delenv(speech.DLL_ENV_VAR, raising=False)
        candidates = speech.trusted_dll_paths()

        assert candidates
        for path in candidates:
            assert path.is_absolute(), f"候選路徑不是絕對路徑：{path}"
            assert Path(path.name) != path, f"候選路徑是裸檔名：{path}"

    def test_candidates_live_in_the_bundled_package_directory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """預設只從套件內建的 lib 目錄尋找，不從工作目錄或系統搜尋路徑。"""
        monkeypatch.delenv(speech.DLL_ENV_VAR, raising=False)
        package_dir = Path(speech.__file__).resolve().parent.parent

        for path in speech.trusted_dll_paths():
            assert path.parent == package_dir / "lib"

    def test_absolute_env_override_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        override = Path.cwd().resolve() / "custom" / "nvdaControllerClient64.dll"
        monkeypatch.setenv(speech.DLL_ENV_VAR, str(override))
        assert speech.trusted_dll_paths() == [override]

    def test_relative_env_override_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """相對路徑會隨工作目錄改變，等同重新引入不受控的搜尋行為。"""
        monkeypatch.setenv(speech.DLL_ENV_VAR, "nvdaControllerClient64.dll")
        assert speech.trusted_dll_paths() == []

    def test_relative_subdirectory_override_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(speech.DLL_ENV_VAR, "./lib/nvdaControllerClient64.dll")
        assert speech.trusted_dll_paths() == []


class TestControllerFallback:
    def test_missing_dll_yields_no_sink(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """找不到可信任的 DLL 時必須安靜地退回文字輸出。"""
        monkeypatch.setattr(speech, "trusted_dll_paths", list)
        assert speech.create_speech_sink() is None

    def test_controller_reports_unavailable_without_dll(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(speech, "trusted_dll_paths", list)
        controller = speech.NvdaController()
        assert controller.available is False
        assert controller.loaded_from is None
        assert controller.speak("測試") is False

    def test_nonexistent_absolute_path_is_not_loaded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(
            speech.DLL_ENV_VAR, str(tmp_path / "nvdaControllerClient64.dll")
        )
        controller = speech.NvdaController()
        assert controller.loaded_from is None
        assert controller.available is False
