"""車上廣播（規格 §20.2「到站廣播」「車門聲」）。

本套件分成三層，彼此不互相依賴實作細節：

- :mod:`~railway_sim.audio.library`：掃描 ``data/audio/announcements``，
  把音檔索引成「車站＋種類」可查詢的資料。
- :mod:`~railway_sim.audio.player`：實際播放音檔。所有後端都是選用的，
  一個都沒有時整套仍可運作，只是不出聲。
- :mod:`~railway_sim.audio.broadcast`：決定「什麼時候該播哪一則」。

三層都遵守同一個原則（§20.1）：**音效不是唯一的資訊來源**。任何一則
廣播都會同時以文字送進 :class:`~railway_sim.accessibility.announcer.Announcer`，
因此沒有音檔、沒有播放後端、或車輛根本沒有廣播設備時，玩家得到的資訊
完全一樣，程式也不會出錯。
"""

from __future__ import annotations

from railway_sim.audio.broadcast import BroadcastSystem
from railway_sim.audio.library import (
    ANNOUNCEMENT_DIRNAME,
    BroadcastClip,
    BroadcastLibrary,
    ClipKind,
)
from railway_sim.audio.player import AudioPlayer, create_player

__all__ = [
    "ANNOUNCEMENT_DIRNAME",
    "AudioPlayer",
    "BroadcastClip",
    "BroadcastLibrary",
    "BroadcastSystem",
    "ClipKind",
    "create_player",
]
