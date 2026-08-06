"""鍵位設定測試（規格 §2.1、§7，以及與 OpenBVE 一致的鍵位要求）。"""

from __future__ import annotations

from typing import ClassVar

import pytest

from railway_sim.data_loader import GameData
from railway_sim.input.keyboard import KeyDispatcher
from railway_sim.input.keymap import Keymap, display_key, normalise_key


@pytest.fixture
def driver_keymap(game_data: GameData) -> Keymap:
    return Keymap.from_dict(game_data.keymap_raw, "driver")


@pytest.fixture
def legacy_keymap(game_data: GameData) -> Keymap:
    return Keymap.from_dict(game_data.keymap_raw, "driver_legacy")


class TestNoConflicts:
    """快捷鍵不得互相衝突（規格 §2.1、§7.1）。"""

    def test_driver_profile_has_no_conflicts(self, driver_keymap: Keymap) -> None:
        conflicts = driver_keymap.conflicts()
        assert conflicts == [], [str(c) for c in conflicts]

    def test_legacy_profile_has_no_conflicts(self, legacy_keymap: Keymap) -> None:
        conflicts = legacy_keymap.conflicts()
        assert conflicts == [], [str(c) for c in conflicts]

    def test_conflict_detection_actually_works(self) -> None:
        """確認衝突檢查不是永遠回傳空清單。"""
        broken = Keymap.from_dict(
            {
                "profiles": {
                    "driver": {
                        "power_up": {"keys": ["D"], "label": "電門"},
                        "horn": {"keys": ["D"], "label": "鳴笛"},
                    }
                }
            },
            "driver",
        )
        conflicts = broken.conflicts()
        assert len(conflicts) == 1
        assert conflicts[0].key == "D"
        assert set(conflicts[0].actions) == {"power_up", "horn"}

    def test_global_keys_are_included_in_conflict_check(self, game_data: GameData) -> None:
        """global 區段的鍵位必須一併納入檢查。"""
        raw = {
            "profiles": {
                "global": {"show_help": {"keys": ["F1"], "label": "說明"}},
                "driver": {"horn": {"keys": ["F1"], "label": "鳴笛"}},
            }
        }
        assert Keymap.from_dict(raw, "driver").conflicts() != []


class TestOpenBveParity:
    """功能相同者，鍵位必須與 OpenBVE 相同。

    對照表逐字取自 OpenBVE 的 ``assets/Controls/Default.controls``，格式為
    ``指令, keyboard, 鍵, 修飾鍵位元``，修飾鍵 Shift=1、Ctrl=2、Alt=4
    （``OpenBveApi/Interface/Input/KeyboardModifier.cs``）。
    """

    #: (OpenBVE 指令, OpenBVE 按鍵設定, 本專案動作)
    SHARED_COMMANDS: ClassVar[list[tuple[str, str, str]]] = [
        ("POWER_INCREASE", "Z", "power_up"),
        ("BRAKE_INCREASE", "Period", "brake_up"),
        ("BRAKE_EMERGENCY", "Slash", "emergency_brake"),
        ("HORN_PRIMARY", "Enter", "horn"),
        ("ACCESSIBILITY_CURRENT_SPEED", "Ctrl+Shift+S", "announce_speed"),
        ("ACCESSIBILITY_NEXT_SIGNAL", "Ctrl+Shift+A", "announce_signal"),
        ("ACCESSIBILITY_NEXT_STATION", "Ctrl+Shift+T", "announce_next_station"),
        ("MENU_ACTIVATE", "Escape", "pause_menu"),
    ]

    #: OpenBVE 的鍵名與本專案正規化代碼的對應。
    KEY_NAMES: ClassVar[dict[str, str]] = {
        "Period": "PERIOD",
        "Slash": "SLASH",
        "Enter": "ENTER",
        "Escape": "ESC",
        "Comma": "COMMA",
    }

    def _expected(self, openbve_key: str) -> str:
        *modifiers, base = openbve_key.split("+")
        base = self.KEY_NAMES.get(base, base.upper())
        return normalise_key("+".join([*modifiers, base]))

    @pytest.mark.parametrize(("command", "openbve_key", "action"), SHARED_COMMANDS)
    def test_shared_function_uses_the_openbve_key(
        self, driver_keymap: Keymap, command: str, openbve_key: str, action: str
    ) -> None:
        expected = self._expected(openbve_key)
        assert expected in driver_keymap.keys_for(action), (
            f"{action} 應包含 OpenBVE {command} 的 {expected}"
        )

    def test_decrease_covers_both_openbve_decrease_commands(
        self, driver_keymap: Keymap
    ) -> None:
        """本專案的減段是單一動作，對應 OpenBVE 的兩個緩解指令。

        OpenBVE ``POWER_DECREASE`` 為 ``A``、``BRAKE_DECREASE`` 為 ``Comma``；
        兩者都應叫到本專案的 ``notch_down``。
        """
        assert driver_keymap.action_for("A") == "notch_down"
        assert driver_keymap.action_for(",") == "notch_down"

    def test_emergency_brake_remains_a_dedicated_key(self, driver_keymap: Keymap) -> None:
        """chat.md：緊急制軔使用獨立按鍵（OpenBVE 的 Slash 同樣是獨立鍵）。"""
        keys = driver_keymap.keys_for("emergency_brake")
        assert keys == ("SLASH",)
        assert driver_keymap.keys_for("brake_up") != keys
        assert driver_keymap.keys_for("power_up") != keys

    def test_every_binding_records_its_source(self, driver_keymap: Keymap) -> None:
        """每個鍵位都要記錄依據，方便追溯是沿用 OpenBVE 還是本專案自訂。"""
        for binding in driver_keymap.bindings:
            assert binding.source, f"{binding.action} 沒有記錄鍵位依據"

    def test_console_aliases_exist_for_modifier_bindings(
        self, driver_keymap: Keymap
    ) -> None:
        """帶修飾鍵的播報動作必須另有單鍵別名。

        終端機無法區分 Ctrl 與 Ctrl+Shift，主控台介面是唯一保證螢幕閱讀器
        行為的介面，因此不能只有修飾鍵組合可用。
        """
        for action in ("announce_speed", "announce_signal", "announce_next_station"):
            keys = driver_keymap.keys_for(action)
            assert any("+" not in key for key in keys), f"{action} 缺少單鍵別名"


class TestLegacyProfile:
    """先前依 chat.md 訂下的配置仍可選用。"""

    @pytest.mark.parametrize(
        ("key", "action"),
        [
            ("D", "power_up"),
            ("A", "brake_up"),
            ("S", "notch_down"),
            ("SPACE", "emergency_brake"),
            ("R", "release_brake"),
            ("H", "horn"),
            ("V", "announce_speed"),
            ("E", "release_emergency"),
        ],
    )
    def test_legacy_bindings(self, legacy_keymap: Keymap, key: str, action: str) -> None:
        assert legacy_keymap.action_for(key) == action

    def test_both_profiles_cover_the_same_actions(
        self, driver_keymap: Keymap, legacy_keymap: Keymap
    ) -> None:
        assert set(driver_keymap.actions) == set(legacy_keymap.actions)


class TestSpecBindings:
    """規格明確要求、OpenBVE 沒有對應指令的鍵位。"""

    @pytest.mark.parametrize(
        ("key", "action"),
        [
            ("R", "release_brake"),
            ("E", "release_emergency"),
            ("P", "announce_position"),
            ("T", "announce_train_status"),
            ("F1", "show_help"),
            ("F2", "repeat_last"),
        ],
    )
    def test_project_specific_bindings(
        self, driver_keymap: Keymap, key: str, action: str
    ) -> None:
        assert driver_keymap.action_for(key) == action


class TestKeyNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("d", "D"),
            ("D", "D"),
            (" ", "SPACE"),
            ("\x1b", "ESC"),
            ("f1", "F1"),
            (".", "PERIOD"),
            (",", "COMMA"),
            ("/", "SLASH"),
        ],
    )
    def test_normalise(self, raw: str, expected: str) -> None:
        assert normalise_key(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Ctrl+Shift+S", "CTRL+SHIFT+S"),
            ("shift+ctrl+s", "CTRL+SHIFT+S"),
            ("CTRL+SHIFT+S", "CTRL+SHIFT+S"),
            ("control+s", "CTRL+S"),
            ("alt+ctrl+shift+a", "CTRL+SHIFT+ALT+A"),
            ("ctrl+.", "CTRL+PERIOD"),
        ],
    )
    def test_modifier_order_is_canonical(self, raw: str, expected: str) -> None:
        """修飾鍵順序不同也要視為同一鍵。"""
        assert normalise_key(raw) == expected

    def test_modifier_and_plain_key_are_different(self, driver_keymap: Keymap) -> None:
        """``A`` 與 ``Ctrl+Shift+A`` 必須是兩個不同的鍵。"""
        assert driver_keymap.action_for("A") == "notch_down"
        assert driver_keymap.action_for("Ctrl+Shift+A") == "announce_signal"

    def test_lowercase_key_still_dispatches(self, driver_keymap: Keymap) -> None:
        assert driver_keymap.action_for("z") == "power_up"

    def test_empty_key(self) -> None:
        assert normalise_key("") == ""


class TestHelpText:
    """所有快捷鍵必須可查詢（規格 §2.1）。"""

    def test_every_action_appears_in_help(self, driver_keymap: Keymap) -> None:
        text = driver_keymap.help_text()
        for binding in driver_keymap.bindings:
            assert binding.label in text

    def test_help_is_grouped_by_category(self, driver_keymap: Keymap) -> None:
        lines = driver_keymap.help_lines()
        assert "【駕駛操作】" in lines
        assert "【狀態查詢】" in lines
        assert "【系統】" in lines

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("SPACE", "空白鍵"),
            ("PERIOD", "句號（.）"),
            ("SLASH", "斜線（/）"),
            ("CTRL+SHIFT+S", "Ctrl＋Shift＋S"),
        ],
    )
    def test_keys_are_displayed_readably(self, key: str, expected: str) -> None:
        assert display_key(key) == expected

    def test_punctuation_keys_are_not_shown_raw(self, driver_keymap: Keymap) -> None:
        """句號、斜線這種鍵直接印出來看不出是什麼，說明必須寫清楚。"""
        binding = driver_keymap.binding_for("emergency_brake")
        assert binding is not None
        assert binding.keys_text == "斜線（/）"


class TestDispatcher:
    """按鍵派送（規格 §7.2）。"""

    def test_dispatch_calls_handler(self, driver_keymap: Keymap) -> None:
        calls: list[str] = []
        dispatcher = KeyDispatcher(driver_keymap)
        dispatcher.register("power_up", lambda: calls.append("power"))

        result = dispatcher.dispatch("Z")
        assert result.handled
        assert result.action == "power_up"
        assert calls == ["power"]

    def test_dispatch_accepts_modifier_combination(self, driver_keymap: Keymap) -> None:
        calls: list[str] = []
        dispatcher = KeyDispatcher(driver_keymap)
        dispatcher.register("announce_speed", lambda: calls.append("speed"))

        assert dispatcher.dispatch("Ctrl+Shift+S").handled
        assert calls == ["speed"]

    def test_unbound_key_reports_reason(self, driver_keymap: Keymap) -> None:
        """未綁定的按鍵也要有回饋，不可靜默（規格 §7.2）。"""
        result = KeyDispatcher(driver_keymap).dispatch("Q")
        assert not result.handled
        assert result.reason == "unbound_key"

    def test_registering_unknown_action_raises(self, driver_keymap: Keymap) -> None:
        with pytest.raises(KeyError):
            KeyDispatcher(driver_keymap).register("open_doors", lambda: None)

    def test_all_driver_actions_have_handlers(
        self, driver_keymap: Keymap, local_session
    ) -> None:
        """鍵位表中的每個動作都必須有對應實作，不能有空按鍵。"""
        dispatcher = KeyDispatcher(driver_keymap)
        dispatcher.register_all(local_session.action_handlers())
        dispatcher.register_all(
            {
                "show_help": lambda: None,
                "repeat_last": lambda: None,
                "pause_menu": lambda: None,
            }
        )
        assert dispatcher.unbound_actions() == []
