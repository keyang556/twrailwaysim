"""可駕駛的鐵路系統（臺鐵／捷運）。

為什麼分成兩套資料而不是一個大資料集
------------------------------------

臺鐵與捷運共用同一組模型（車站、路網、路線、班次、車輛型式）與同一個
運轉核心，因此程式幾乎完全共用；但兩者的**資料**沒有任何交集，規則也不同：

- 臺鐵有山線海線與成追線的路線規則（規格 §10），捷運沒有。
- 臺鐵一個車次跑一條路線，捷運是「營運模式」，同一條線同時有全程車與區間車。
- 捷運有 ATO 與自動駕駛，臺鐵沒有（見 :mod:`railway_sim.simulation.ato`）。

把兩者塞進同一批 ``stations.json`` 只會讓每一次查詢都得先過濾系統，而且
臺鐵的路線規則驗證會被迫忽略一半的資料。改成「同樣的檔名、不同的目錄」
之後，載入器、驗證、匯入工具全部原封不動就能跑第二套資料。

目錄配置
--------

::

    data/                 ← 臺鐵（維持原位，安裝檔與既有工具的路徑不變）
        stations.json
        keymap.json       ← 鍵位兩邊共用，只有一份
        audio/announcements/
            west_north/   ← 依 line_id 分類，捷運各線也放這裡
            tamsui_xinyi/
        mrt/              ← 捷運
            stations.json
            line_spec.json

鍵位與廣播音檔刻意留在共用的位置：同一個玩家不會希望換一個系統就得重設
一次按鍵，而廣播本來就是以 ``line_id`` 分類，兩個系統的線別代碼不會相撞。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["DEFAULT_SYSTEM", "SYSTEMS", "RailSystem", "system_data_dir"]


@dataclass(frozen=True)
class RailSystem:
    """一個可駕駛的鐵路系統。

    Attributes:
        data_subdir: 資料目錄相對於 ``data/`` 的位置。空字串表示就在 ``data/``
            底下——臺鐵留在原位，安裝檔與既有工具的路徑才不會變。
        supports_ato: 有沒有 ATO 與自動駕駛。臺鐵沒有，因此臺鐵模式下連
            切換鍵都會回覆「本系統沒有這個功能」，而不是靜靜地沒反應。
    """

    id: str
    name_zh_tw: str
    data_subdir: str
    supports_ato: bool
    description: str = ""


SYSTEMS: dict[str, RailSystem] = {
    "tra": RailSystem(
        id="tra",
        name_zh_tw="臺鐵",
        data_subdir="",
        supports_ato=False,
        description="臺灣鐵路：西部幹線、宜蘭線、北迴線、臺東線、南迴線與各支線。",
    ),
    "mrt": RailSystem(
        id="mrt",
        name_zh_tw="捷運",
        data_subdir="mrt",
        supports_ato=True,
        description="臺北捷運七條線、新北捷運三鶯線與桃園機場捷運，含 ATO 與自動駕駛。",
    ),
}

#: 未指定時使用的系統。
DEFAULT_SYSTEM = "tra"


def system_data_dir(root: str | Path, system: str = DEFAULT_SYSTEM) -> Path:
    """某個系統的資料目錄。

    Raises:
        KeyError: 沒有這個系統代碼。
    """
    try:
        entry = SYSTEMS[system]
    except KeyError:
        raise KeyError(
            f"沒有這個鐵路系統：{system}（可用：{'、'.join(SYSTEMS)}）"
        ) from None
    root_path = Path(root)
    return root_path / entry.data_subdir if entry.data_subdir else root_path
