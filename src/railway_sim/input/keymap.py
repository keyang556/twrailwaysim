"""鍵位設定（規格 §7）。

規格要求：

- 預設鍵位由 ``data/keymap.json`` 載入，方便日後修改（§7）。
- 所有快捷鍵必須可查詢（§2.1）→ :meth:`Keymap.help_lines`。
- 快捷鍵不得互相衝突（§2.1、§7.1）→ :meth:`Keymap.conflicts`，並由
  ``tests/test_keymap.py`` 強制為空。

鍵位衝突處理原則
----------------

規格 §7.1 的鍵位表與 chat.md 的結論一致：

- ``D`` 增加電門（chat.md：「保持 D 鍵電門」）
- ``A`` 增加常用制軔（chat.md：「保持 A 鍵制軔」）
- 緊急制軔使用獨立按鍵（chat.md：「緊急制軔使用獨立按鍵」）→ ``Space``

兩份文件在鍵位上沒有實際衝突。依使用者指示，若日後出現衝突，一律以
chat.md 的結論為準；本檔的 ``source`` 欄位即記錄每個鍵位的依據。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["KeyBinding", "Keymap", "KeymapConflict", "normalise_key"]

#: 特殊鍵的正規化名稱。
_SPECIAL_KEYS = {
    " ": "SPACE",
    "\x1b": "ESC",
    "\r": "ENTER",
    "\n": "ENTER",
    "\t": "TAB",
}


def normalise_key(raw: str) -> str:
    """把按鍵字串正規化成統一的代碼。

    單一字元轉大寫，特殊字元轉為 ``SPACE``、``ESC`` 等名稱，其餘（``F1``、
    ``UP``…）轉大寫後原樣保留。
    """
    if not raw:
        return ""
    if raw in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[raw]
    upper = raw.strip().upper()
    return _SPECIAL_KEYS.get(upper, upper)


@dataclass(frozen=True)
class KeyBinding:
    """一個動作與其按鍵。"""

    action: str
    keys: tuple[str, ...]
    label: str
    category: str = "general"
    source: str = ""

    @property
    def keys_text(self) -> str:
        """給玩家看的按鍵說明。"""
        return "、".join(_DISPLAY_NAMES.get(k, k) for k in self.keys)


#: 按鍵在說明中的顯示名稱。
_DISPLAY_NAMES = {
    "SPACE": "空白鍵",
    "ESC": "Esc",
    "ENTER": "Enter",
}


@dataclass(frozen=True)
class KeymapConflict:
    """一組衝突的鍵位。"""

    key: str
    actions: tuple[str, ...]

    def __str__(self) -> str:  # pragma: no cover - 便利用途
        return f"按鍵 {self.key} 同時對應到：{'、'.join(self.actions)}"


@dataclass
class Keymap:
    """單一模式（profile）可用的鍵位表。"""

    profile: str
    bindings: tuple[KeyBinding, ...] = ()
    _by_key: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._by_key = {}
        for binding in self.bindings:
            for key in binding.keys:
                self._by_key.setdefault(key, binding.action)

    # ------------------------------------------------------------------
    # 查詢
    # ------------------------------------------------------------------
    def action_for(self, key: str) -> str | None:
        """按鍵對應的動作代碼。"""
        return self._by_key.get(normalise_key(key))

    def binding_for(self, action: str) -> KeyBinding | None:
        for binding in self.bindings:
            if binding.action == action:
                return binding
        return None

    def keys_for(self, action: str) -> tuple[str, ...]:
        binding = self.binding_for(action)
        return binding.keys if binding else ()

    @property
    def actions(self) -> tuple[str, ...]:
        return tuple(b.action for b in self.bindings)

    # ------------------------------------------------------------------
    # 衝突檢查（§2.1、§7.1）
    # ------------------------------------------------------------------
    def conflicts(self) -> list[KeymapConflict]:
        """回傳所有一鍵對應多個動作的情形。"""
        owners: dict[str, list[str]] = {}
        for binding in self.bindings:
            for key in binding.keys:
                owners.setdefault(key, []).append(binding.action)
        return [
            KeymapConflict(key=key, actions=tuple(actions))
            for key, actions in sorted(owners.items())
            if len(actions) > 1
        ]

    # ------------------------------------------------------------------
    # 說明（§2.1 所有快捷鍵必須可查詢）
    # ------------------------------------------------------------------
    def help_lines(self) -> list[str]:
        """產生純文字的快捷鍵說明，依分類排列。"""
        lines: list[str] = []
        seen_categories: list[str] = []
        for binding in self.bindings:
            if binding.category not in seen_categories:
                seen_categories.append(binding.category)
        for category in seen_categories:
            lines.append(f"【{_CATEGORY_NAMES.get(category, category)}】")
            for binding in self.bindings:
                if binding.category == category:
                    lines.append(f"{binding.keys_text}：{binding.label}")
        return lines

    def help_text(self) -> str:
        return "\n".join(self.help_lines())

    # ------------------------------------------------------------------
    # 載入
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: dict[str, Any], profile: str) -> Keymap:
        """由設定字典建立鍵位表。

        ``global`` 區段的鍵位會併入所有模式，衝突檢查也一併涵蓋。
        """
        profiles = raw.get("profiles", {})
        if profile not in profiles:
            raise KeyError(f"keymap.json 中沒有模式：{profile}")

        bindings: list[KeyBinding] = []
        for section in ("global", profile):
            if section not in profiles:
                continue
            for action, entry in profiles[section].items():
                bindings.append(
                    KeyBinding(
                        action=action,
                        keys=tuple(normalise_key(k) for k in entry["keys"]),
                        label=entry["label"],
                        category=entry.get("category", section),
                        source=entry.get("source", ""),
                    )
                )
        return cls(profile=profile, bindings=tuple(bindings))

    @classmethod
    def load(cls, path: str | Path, profile: str) -> Keymap:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data, profile)


#: 說明畫面中的分類名稱。
_CATEGORY_NAMES = {
    "driving": "駕駛操作",
    "query": "狀態查詢",
    "system": "系統",
    "global": "系統",
}
