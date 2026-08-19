"""語音後端載入安全性測試（DLL 綁架防護）。

語音是選用功能，但載入 DLL 的方式必須安全：以裸檔名呼叫 ``LoadLibrary``
會沿用 Windows 預設搜尋順序，攻擊者只要在搜尋路徑上放一個同名 DLL 就能
被載入遊戲程序。因此這裡驗證候選路徑一律為絕對路徑，且不含裸檔名。
"""

from __future__ import annotations

import hashlib
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
        assert speech.create_screen_reader() is None

    def test_controller_reports_unavailable_without_dll(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(speech, "trusted_dll_paths", list)
        controller = speech.NvdaController()
        assert controller.client_loaded is False
        assert controller.available is False
        assert controller.loaded_from is None
        assert controller.speak("測試") is False
        assert controller.braille("測試") is False
        assert controller.cancel() is False

    def test_missing_dll_reports_no_optional_capabilities(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """能力是探測出來的，不是猜的；探測不到就一律關閉。"""
        monkeypatch.setattr(speech, "trusted_dll_paths", list)
        controller = speech.NvdaController()
        assert controller.supports_ssml is False
        assert controller.supports_process_id is False
        assert controller.supports_is_speaking is False
        assert controller.process_id() is None

    def test_is_speaking_returns_none_when_unsupported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``None`` 是「這一版 NVDA 沒有這個功能」，不是「沒有在說話」。"""
        monkeypatch.setattr(speech, "trusted_dll_paths", list)
        assert speech.NvdaController().is_speaking() is None

    def test_nonexistent_absolute_path_is_not_loaded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(
            speech.DLL_ENV_VAR, str(tmp_path / "nvdaControllerClient64.dll")
        )
        controller = speech.NvdaController()
        assert controller.loaded_from is None
        assert controller.client_loaded is False
        assert controller.available is False


class TestBundledControllerClient:
    """發行版相依檔必須存在、可追溯，且 PyInstaller 會收進正確位置。"""

    _ROOT = Path(__file__).resolve().parents[1]
    _DIRECTORY = _ROOT / "third_party" / "nvda-controller-client" / "2026.1.1"
    _DLL = _DIRECTORY / "nvdaControllerClient.dll"

    def test_reviewed_x64_client_and_license_are_vendored(self) -> None:
        assert self._DLL.is_file()
        assert (self._DIRECTORY / "license.txt").is_file()
        assert hashlib.sha256(self._DLL.read_bytes()).hexdigest() == (
            "2fe60cf00be929aae32e95c1e1507a20ada4902c8fec273b3cc2d3bf5472932a"
        )

    def test_spec_places_the_client_beside_the_package(self) -> None:
        spec = (self._ROOT / "packaging" / "twrailwaysim.spec").read_text(
            encoding="utf-8"
        )
        assert "nvda-controller-client" in spec
        assert '"railway_sim/lib"' in spec

    def test_build_smoke_loads_the_frozen_client(self) -> None:
        build_script = (self._ROOT / "scripts" / "build-installer.ps1").read_text(
            encoding="utf-8"
        )
        assert '"--check-nvda-controller"' in build_script


class FakeFunction:
    """假的 DLL 匯出函式，記錄呼叫並回傳指定的錯誤碼。"""

    def __init__(self, results: list[int] | None = None) -> None:
        self.calls: list[tuple] = []
        self.results = list(results or [0])
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


class FakeDll:
    """假的 nvdaControllerClient.dll。

    ``missing`` 裡的函式會像舊版 DLL 一樣擲出 ``AttributeError``，這正是
    :class:`speech.NvdaController` 用來判斷「這一版有沒有」的依據。
    """

    def __init__(self, *, missing: tuple[str, ...] = (), results: dict | None = None):
        self._missing = set(missing)
        self._functions: dict[str, FakeFunction] = {}
        self._results = results or {}

    def __getattr__(self, name: str) -> FakeFunction:
        if not name.startswith("nvdaController_") or name in self._missing:
            raise AttributeError(name)
        if name not in self._functions:
            self._functions[name] = FakeFunction(self._results.get(name))
        return self._functions[name]

    def calls(self, name: str) -> list[tuple]:
        return self._functions[name].calls if name in self._functions else []


def make_controller(**kwargs) -> tuple[speech.NvdaController, FakeDll]:
    """建立一個接上假 DLL 的控制器（不碰真正的 NVDA）。"""
    controller = speech.NvdaController.__new__(speech.NvdaController)
    controller.loaded_from = None
    controller.supports_ssml = False
    controller.supports_process_id = False
    controller.supports_is_speaking = False
    dll = FakeDll(**kwargs)
    controller._dll = dll
    controller._declare_signatures()
    return controller, dll


class TestCapabilityProbing:
    """能力逐一探測，不比對版本號——玩家的 NVDA 是哪一版只有 DLL 知道。"""

    def test_modern_client_exposes_everything(self) -> None:
        controller, _ = make_controller()
        assert controller.supports_ssml
        assert controller.supports_process_id
        assert controller.supports_is_speaking

    def test_nvda_2026_2_has_ssml_but_not_is_speaking(self) -> None:
        """isSpeaking 要到 2026.3 才有；2026.2 上其餘功能都正常。"""
        controller, _ = make_controller(missing=("nvdaController_isSpeaking",))
        assert controller.supports_ssml
        assert controller.supports_process_id
        assert not controller.supports_is_speaking
        assert controller.is_speaking() is None

    def test_pre_2024_1_client_falls_back_to_plain_speech(self) -> None:
        controller, _ = make_controller(
            missing=(
                "nvdaController_speakSsml",
                "nvdaController_getProcessId",
                "nvdaController_isSpeaking",
            )
        )
        assert not controller.supports_ssml
        assert not controller.supports_process_id
        assert controller.process_id() is None

    def test_signatures_are_declared(self) -> None:
        """不宣告型別的話，指標參數在 x64 上很容易踩壞堆疊。"""
        _, dll = make_controller()
        assert dll.nvdaController_speakText.argtypes is not None
        assert dll.nvdaController_getProcessId.argtypes is not None
        assert dll.nvdaController_speakSsml.argtypes is not None


class TestSpeechPriority:
    """優先級交給 NVDA 排，而不是自己 cancelSpeech。"""

    def test_ssml_is_used_when_available(self) -> None:
        controller, dll = make_controller()
        assert controller.speak("超速五公里。", priority=speech.SpeechPriority.NOW)

        (ssml, symbol_level, priority, asynchronous), = dll.calls(
            "nvdaController_speakSsml"
        )
        assert ssml == "<speak>超速五公里。</speak>"
        assert symbol_level == speech.SymbolLevel.UNCHANGED
        assert priority == speech.SpeechPriority.NOW
        assert asynchronous == 1

    def test_urgent_speech_does_not_destroy_the_queue(self) -> None:
        """cancelSpeech 會把被打斷的內容整段丟掉，NOW 則會由 NVDA 接回去。"""
        controller, dll = make_controller()
        controller.speak("緊急制軔。", priority=speech.SpeechPriority.NOW)
        assert dll.calls("nvdaController_cancelSpeech") == []

    def test_special_characters_are_escaped(self) -> None:
        """播報文字直接塞進 SSML 會讓 < 與 & 變成無效的 XML。"""
        controller, dll = make_controller()
        controller.speak("速度 < 80 & 制軔")
        (ssml, *_), = dll.calls("nvdaController_speakSsml")
        assert "&lt;" in ssml and "&amp;" in ssml

    def test_old_nvda_behind_a_new_dll_falls_back(self) -> None:
        """符號在、介面不在：speakSsml 回 1717，之後一律改走 speakText。"""
        controller, dll = make_controller(
            results={"nvdaController_speakSsml": [1717]}
        )
        assert controller.speak("測試", interrupt=True)
        assert not controller.supports_ssml
        assert dll.calls("nvdaController_speakText")
        assert dll.calls("nvdaController_cancelSpeech")

        controller.speak("第二句")
        assert len(dll.calls("nvdaController_speakSsml")) == 1

    def test_plain_client_interrupts_with_cancel_speech(self) -> None:
        controller, dll = make_controller(missing=("nvdaController_speakSsml",))
        controller.speak("緊急制軔。", priority=speech.SpeechPriority.NOW)
        assert dll.calls("nvdaController_cancelSpeech")
        assert dll.calls("nvdaController_speakText")


class TestBraille:
    def test_message_is_forwarded(self) -> None:
        controller, dll = make_controller()
        assert controller.braille("下一站 板橋 1.2km")
        assert dll.calls("nvdaController_brailleMessage") == [("下一站 板橋 1.2km",)]

    def test_empty_message_is_not_sent(self) -> None:
        """空字串只會把顯示器清成空白，沒有任何資訊價值。"""
        controller, dll = make_controller()
        assert controller.braille("") is False
        assert dll.calls("nvdaController_brailleMessage") == []


class TestScreenReader:
    """介面層拿到的物件：可以當成 speak sink，也可以送點字。"""

    def test_is_usable_as_a_plain_speak_sink(self) -> None:
        controller, dll = make_controller()
        reader = speech.ScreenReader(controller)
        assert reader("電門一段。", False)
        assert dll.calls("nvdaController_speakSsml")

    def test_interrupt_maps_to_speak_now(self) -> None:
        controller, dll = make_controller()
        speech.ScreenReader(controller)("超速五公里。", True)
        (_, _, priority, _), = dll.calls("nvdaController_speakSsml")
        assert priority == speech.SpeechPriority.NOW

    def test_status_text_names_what_is_connected(self) -> None:
        """「沒有聲音」有好幾種原因，說清楚才不會被當成故障。"""
        controller, _ = make_controller()
        controller._dll.nvdaController_testIfRunning.results = [0]
        reader = speech.ScreenReader(controller)
        text = reader.status_text()
        assert "語音" in text and "點字" in text and "優先級" in text

    def test_status_text_when_nvda_is_not_running(self) -> None:
        controller, _ = make_controller(
            results={"nvdaController_testIfRunning": [1]}
        )
        text = speech.ScreenReader(controller).status_text()
        assert "未連接" in text
        assert "文字" in text

    def test_available_is_dynamic_for_the_same_reader(self) -> None:
        """遊戲先開、NVDA 後開或重開時，不可重建 backend 才能恢復。"""
        controller, _ = make_controller(
            results={"nvdaController_testIfRunning": [1, 0, 1, 0]}
        )
        reader = speech.ScreenReader(controller)

        assert reader.available is False
        assert reader.available is True
        assert reader.available is False
        assert reader.available is True

    def test_create_reader_keeps_loaded_client_when_nvda_starts_later(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        controller, _ = make_controller(
            results={"nvdaController_testIfRunning": [1, 0]}
        )
        monkeypatch.setattr(speech, "NvdaController", lambda: controller)

        reader = speech.create_screen_reader()

        assert reader is not None
        assert reader.available is False
        assert reader.available is True

    def test_is_speaking_compatibility_does_not_change_availability(self) -> None:
        controller, _ = make_controller(
            results={
                "nvdaController_testIfRunning": [0],
                "nvdaController_isSpeaking": [speech._RPC_UNKNOWN_INTERFACE],
            }
        )

        assert controller.is_speaking() is None
        assert controller.supports_is_speaking is False
        assert speech.ScreenReader(controller).available is True
