"""鍵位設定（規格 §7）。

規格要求：

- 預設鍵位由 ``data/keymap.json`` 載入，方便日後修改（§7）。
- 所有快捷鍵必須可查詢（§2.1）→ :meth:`Keymap.help_lines`。
- 快捷鍵不得互相衝突（§2.1、§7.1）→ :meth:`Keymap.conflicts`，並由
  ``tests/test_keymap.py`` 強制為空。

與 OpenBVE 一致的鍵位
---------------------

依使用者指示「相同的功能，快捷鍵需和 OpenBVE 相同」，預設 profile
``driver`` 的鍵位直接取自 OpenBVE 的 ``assets/Controls/Default.controls``：
電門 ``Z``、減段 ``A``／``,``、制軔 ``.``、緊急制軔 ``/``、鳴笛 ``Enter``，
播報則沿用 OpenBVE 的無障礙指令 ``Ctrl+Shift+S／A／T``。

這取代了先前依 chat.md 訂下的 ``D`` 電門／``A`` 制軔配置；舊配置保留在
profile ``driver_legacy``，可用 ``--keymap-profile driver_legacy`` 選用。
每個鍵位的 ``source`` 欄位都記錄其依據。

修飾鍵
------

按鍵代碼支援 ``Ctrl+Shift+S`` 這種寫法，正規化後修飾鍵順序固定為
``CTRL+SHIFT+ALT``，因此 ``Shift+Ctrl+S`` 與 ``Ctrl+Shift+S`` 視為同一鍵。

**主控台介面的限制**：終端機只會收到控制字元，無法區分 ``Ctrl+S`` 與
``Ctrl+Shift+S``。主控台把控制字元一律視為 ``Ctrl+Shift+<字母>``；由於本
專案沒有使用單獨的 ``Ctrl+<字母>``，在本鍵位表內不會產生歧義。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["KeyBinding", "Keymap", "KeymapConflict", "display_key", "normalise_key"]

#: 特殊鍵的正規化名稱。
_SPECIAL_KEYS = {
    " ": "SPACE",
    "\x1b": "ESC",
    "\r": "ENTER",
    "\n": "ENTER",
    "\t": "TAB",
    ".": "PERIOD",
    ",": "COMMA",
    "/": "SLASH",
    ";": "SEMICOLON",
    "'": "QUOTE",
    "[": "BRACKETLEFT",
    "]": "BRACKETRIGHT",
    "-": "MINUS",
    "=": "EQUALS",
    "\\": "BACKSLASH",
}

#: 修飾鍵的正規化名稱與固定排列順序。
_MODIFIER_ALIASES = {
    "CTRL": "CTRL",
    "CONTROL": "CTRL",
    "SHIFT": "SHIFT",
    "ALT": "ALT",
}

#: 修飾鍵在代碼中的固定順序，確保 ``CTRL+SHIFT+S`` 與 ``SHIFT+CTRL+S`` 相同。
_MODIFIER_ORDER = ("CTRL", "SHIFT", "ALT")


def _normalise_base_key(raw: str) -> str:
    if raw in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[raw]
    upper = raw.strip().upper()
    return _SPECIAL_KEYS.get(upper, upper)


def normalise_key(raw: str) -> str:
    """把按鍵字串正規化成統一的代碼。

    支援修飾鍵，例如 ``"Ctrl+Shift+S"`` 與 ``"SHIFT+CTRL+s"`` 都會得到
    ``"CTRL+SHIFT+S"``。單一字元轉大寫，特殊字元轉為 ``SPACE``、``PERIOD``
    等名稱，其餘（``F1``、``UP``…）轉大寫後原樣保留。
    """
    if not raw:
        return ""
    if raw in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[raw]

    parts = [p for p in raw.strip().upper().split("+") if p]
    if not parts:
        # 「+」本身。
        return _normalise_base_key(raw)

    modifiers: set[str] = set()
    base_parts: list[str] = []
    for part in parts:
        alias = _MODIFIER_ALIASES.get(part)
        if alias:
            modifiers.add(alias)
        else:
            base_parts.append(part)

    base = _normalise_base_key("+".join(base_parts) if base_parts else "+")
    if not modifiers:
        return base
    ordered = [m for m in _MODIFIER_ORDER if m in modifiers]
    return "+".join([*ordered, base])


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
        return "、".join(display_key(k) for k in self.keys)


#: 按鍵在說明中的顯示名稱。
_DISPLAY_NAMES = {
    "SPACE": "空白鍵",
    "ESC": "Esc",
    "ENTER": "Enter",
    "PERIOD": "句號（.）",
    "COMMA": "逗號（,）",
    "SLASH": "斜線（/）",
    "SEMICOLON": "分號（;）",
    "MINUS": "減號（-）",
    "EQUALS": "等號（=）",
    "BRACKETLEFT": "左中括號（[）",
    "BRACKETRIGHT": "右中括號（]）",
    "BACKSLASH": "反斜線（\\）",
    "CTRL": "Ctrl",
    "SHIFT": "Shift",
    "ALT": "Alt",
}


def display_key(key: str) -> str:
    """把正規化的按鍵代碼轉成說明畫面用的文字。"""
    if "+" not in key:
        return _DISPLAY_NAMES.get(key, key)
    *modifiers, base = key.split("+")
    shown = [_DISPLAY_NAMES.get(m, m) for m in modifiers]
    shown.append(_DISPLAY_NAMES.get(base, base))
    return "＋".join(shown)


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
