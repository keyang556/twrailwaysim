"""車站與月台資料模型（規格 §6.1、§6.2）。

依規格 §22 與 §27，凡是無法從公開官方資料確認的欄位（月台數、股道配置、
月台長度）一律為 ``None`` 並標記 ``verification_status = "unknown"``，
不得自行推測。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["Platform", "Station", "VerificationStatus"]

#: 允許的資料驗證狀態。``test_data`` 表示為第一版明確標示的測試資料（§27）。
VerificationStatus = str

_VALID_VERIFICATION = frozenset({"official", "test_data", "unknown"})


@dataclass(frozen=True)
class Platform:
    """月台（規格 §6.2）。"""

    id: str
    station_id: str
    track_id: str
    length_m: float | None = None
    direction: str | None = None
    verification_status: VerificationStatus = "unknown"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Platform:
        return cls(
            id=raw["id"],
            station_id=raw["station_id"],
            track_id=raw["track_id"],
            length_m=raw.get("length_m"),
            direction=raw.get("direction"),
            verification_status=raw.get("verification_status", "unknown"),
        )


@dataclass(frozen=True)
class Station:
    """車站（規格 §6.1）。

    Attributes:
        stop_rules: 車種代碼 -> 是否辦理客運停靠。此表用來表達「成功站僅停靠
            區間車」這類規則（§10.1、chat.md 成功站資料修正）。實際某一班次
            停或不停，仍以該班次的停靠表為準（§9.1）。
        is_operational_node: 是否同時是運轉上的節點（分歧、聯絡線接續等）。
    """

    id: str
    name_zh_tw: str
    line_ids: tuple[str, ...]
    stop_rules: dict[str, bool] = field(default_factory=dict)
    platforms: tuple[Platform, ...] = ()
    is_operational_node: bool = False
    platform_count: int | None = None
    verification_status: VerificationStatus = "unknown"
    source: str | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        if self.verification_status not in _VALID_VERIFICATION:
            raise ValueError(
                f"車站 {self.id} 的 verification_status 無效：{self.verification_status}"
            )

    def allows_train_type(self, train_type: str) -> bool:
        """該車種是否可在本站辦理客運停靠。

        未列於 ``stop_rules`` 的車種視為不可停靠，避免「經過即停靠」的錯誤
        推論（§9.1、§25.7）。
        """
        return bool(self.stop_rules.get(train_type, False))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Station:
        return cls(
            id=raw["id"],
            name_zh_tw=raw["name_zh_tw"],
            line_ids=tuple(raw.get("line_ids", ())),
            stop_rules=dict(raw.get("stop_rules", {})),
            platforms=tuple(Platform.from_dict(p) for p in raw.get("platforms", ())),
            is_operational_node=bool(raw.get("is_operational_node", False)),
            platform_count=raw.get("platform_count"),
            verification_status=raw.get("verification_status", "unknown"),
            source=raw.get("source"),
            notes=raw.get("notes"),
        )
