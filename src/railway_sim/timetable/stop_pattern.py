"""停靠判斷（規格 §9.1、chat.md「成功站資料修正」）。

判斷順序：

1. 班次停靠表明列為停靠 → ``stop``
2. 班次停靠表明列為通過 → ``pass``
3. 兩者皆未列出 → 依車站的車種停靠規則判斷；車種不可在該站辦理客運停靠
   時為 ``pass``，否則為 ``conditional``（依班次資料判斷，預設通過）

**絕不可**因為「列車經過這條路線」就判定停靠（§9.1）。
"""

from __future__ import annotations

from typing import Literal

from railway_sim.railway.station import Station
from railway_sim.timetable.service import Service

__all__ = ["STOP_KINDS", "StopKind", "resolve_stop_kind", "validate_service"]

StopKind = Literal["stop", "pass", "conditional"]

STOP_KINDS: tuple[StopKind, ...] = ("stop", "pass", "conditional")


def resolve_stop_kind(
    service: Service,
    station: Station,
    *,
    resolve_conditional: bool = True,
) -> StopKind:
    """判斷某班次在某站是停靠、通過，還是依班次判斷。

    Args:
        service: 班次。
        station: 車站。
        resolve_conditional: 為 ``True`` 時把 ``conditional`` 收斂為實際結果
            （未列於停靠表即為通過），供運轉邏輯使用；為 ``False`` 時保留
            ``conditional``，供資料檢查與播報使用。
    """
    if station.id in service.stop_station_ids:
        return "stop"
    if station.id in service.pass_station_ids:
        return "pass"
    if not station.allows_train_type(service.train_type):
        # 該車種不在本站辦理客運停靠，例如自強號之於成功站。
        return "pass"
    return "pass" if resolve_conditional else "conditional"


def validate_service(service: Service, stations: dict[str, Station]) -> list[str]:
    """檢查班次停靠表是否違反車站的車種停靠規則。

    這是規格 §25.7「不得把成功站設定成自強號停靠站」的自動化防線。

    Returns:
        錯誤訊息清單，空清單表示通過。
    """
    errors: list[str] = []

    overlap = set(service.stop_station_ids) & set(service.pass_station_ids)
    if overlap:
        errors.append(
            f"班次 {service.train_number} 同時把 {'、'.join(sorted(overlap))} "
            "列為停靠站與通過站"
        )

    for station_id in service.stop_station_ids:
        station = stations.get(station_id)
        if station is None:
            errors.append(f"班次 {service.train_number} 的停靠站 {station_id} 不存在")
            continue
        if not station.allows_train_type(service.train_type):
            errors.append(
                f"班次 {service.train_number}（{service.train_type}）不得停靠 "
                f"{station.name_zh_tw}站：該站不辦理此車種客運停靠"
            )

    for station_id in service.pass_station_ids:
        if station_id not in stations:
            errors.append(f"班次 {service.train_number} 的通過站 {station_id} 不存在")

    return errors
