"""中文站名與內部車站代碼的對照（``data/station_registry.json``）。

匯入時刻表時，來源檔案只給中文站名；本模組負責把它對應到遊戲資料使用的
車站代碼。臺鐵新增或改名車站時，只要在對照表加一筆就能重新匯入，程式碼
不必更動。

``id`` 是本專案內部的識別碼，不是臺鐵的官方車站代碼；``name_en_status``
記錄英文站名是否由來源檔案本身證實（``official``）或僅由漢語拼音推導
（``derived``），符合規格 §22、§27「不得把未經證實的資料當成官方資料」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

__all__ = ["StationEntry", "StationRegistry", "UnknownStationError"]


class UnknownStationError(LookupError):
    """來源檔案出現對照表裡沒有的站名。"""


@dataclass(frozen=True)
class StationEntry:
    """對照表中的一個車站。"""

    name_zh_tw: str
    id: str
    name_en: str | None = None
    name_en_status: str = "derived"

    @property
    def official_name_en(self) -> str | None:
        """僅在來源檔案證實時回傳英文站名，否則為 ``None``。"""
        return self.name_en if self.name_en_status == "official" else None


@dataclass(frozen=True)
class StationRegistry:
    """整份對照表。"""

    entries: tuple[StationEntry, ...]

    def __post_init__(self) -> None:
        seen_ids: dict[str, str] = {}
        seen_names: set[str] = set()
        for entry in self.entries:
            if entry.id in seen_ids:
                raise ValueError(
                    f"車站代碼重複：{entry.id}"
                    f"（{seen_ids[entry.id]} 與 {entry.name_zh_tw}）"
                )
            if entry.name_zh_tw in seen_names:
                raise ValueError(f"站名重複：{entry.name_zh_tw}")
            seen_ids[entry.id] = entry.name_zh_tw
            seen_names.add(entry.name_zh_tw)

    def entry_for(self, name_zh_tw: str) -> StationEntry:
        for entry in self.entries:
            if entry.name_zh_tw == name_zh_tw:
                return entry
        raise UnknownStationError(
            f"對照表中沒有站名「{name_zh_tw}」；"
            "請在 data/station_registry.json 補一筆後重新匯入。"
        )

    def id_for(self, name_zh_tw: str) -> str:
        return self.entry_for(name_zh_tw).id

    def contains(self, name_zh_tw: str) -> bool:
        return any(e.name_zh_tw == name_zh_tw for e in self.entries)

    @classmethod
    def from_dict(cls, raw: dict) -> StationRegistry:
        return cls(
            entries=tuple(
                StationEntry(
                    name_zh_tw=item["name_zh_tw"],
                    id=item["id"],
                    name_en=item.get("name_en"),
                    name_en_status=item.get("name_en_status", "derived"),
                )
                for item in raw.get("stations", ())
            )
        )

    @classmethod
    def load(cls, path: str | Path) -> StationRegistry:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
