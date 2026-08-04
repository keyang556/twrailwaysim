"""行車違規與異常事件紀錄（規格 §9.2、§12.3、§14.2）。

規格 §12.3 明確要求：站外等待屬**特殊事件**，不得當作日常高頻事件。因此
異常事件只能由外部明確觸發（例如劇本或調度介入），系統本身不會自行產生。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["INCIDENT_TYPES", "Incident", "IncidentLog", "Violation"]

#: 規格 §12.3 列出的特殊事件型別。
INCIDENT_TYPES: tuple[str, ...] = (
    "train_failure",  # 前方列車故障
    "signal_failure",  # 號誌故障
    "point_failure",  # 道岔故障
    "engineering_block",  # 施工封鎖
    "major_delay_conflict",  # 重大誤點造成衝突
    "temporary_restriction",  # 災害或臨時管制
)


@dataclass(frozen=True)
class Violation:
    """一件行車違規。"""

    kind: str
    """``missed_stop`` 應停未停、``overspeed`` 超速、``spad`` 冒進號誌。"""

    description: str
    at_position_m: float
    at_time_s: float
    station_id: str | None = None


@dataclass(frozen=True)
class Incident:
    """一件特殊事件（§12.3）。"""

    kind: str
    description: str
    at_time_s: float

    def __post_init__(self) -> None:
        if self.kind not in INCIDENT_TYPES:
            raise ValueError(f"未知的特殊事件型別：{self.kind}")


@dataclass
class IncidentLog:
    """違規與特殊事件的紀錄。"""

    violations: list[Violation] = field(default_factory=list)
    incidents: list[Incident] = field(default_factory=list)

    def record_violation(
        self,
        kind: str,
        description: str,
        *,
        at_position_m: float,
        at_time_s: float,
        station_id: str | None = None,
    ) -> Violation:
        violation = Violation(
            kind=kind,
            description=description,
            at_position_m=at_position_m,
            at_time_s=at_time_s,
            station_id=station_id,
        )
        self.violations.append(violation)
        return violation

    def record_incident(self, kind: str, description: str, *, at_time_s: float) -> Incident:
        incident = Incident(kind=kind, description=description, at_time_s=at_time_s)
        self.incidents.append(incident)
        return incident

    def count_of(self, kind: str) -> int:
        return sum(1 for v in self.violations if v.kind == kind)

    @property
    def violation_count(self) -> int:
        return len(self.violations)
