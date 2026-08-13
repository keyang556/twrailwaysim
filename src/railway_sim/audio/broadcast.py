"""車上廣播（規格 §20.2「到站廣播」）。

播放時機
--------

``next``（下一站）
    列車自車站**啟動之後**播放，內容是下一個停靠站。

``arrive``（到站）
    到達停靠站**之前**播放。

``terminus``（終點）
    到達終點站之前播放。該站有終點廣播時就**不再播到站廣播**；沒有終點
    廣播的車站則退回播它的到站廣播。

一律以「下一個**停靠站**」為準，不照路線上的車站順序推進。自強號、區間快
會通過許多車站，若照順序播就會播出根本不停的站；以停靠站為準的規則對
區間車（站站停）與對號列車都成立，因此不需要為車種分開處理。

文字永遠存在
------------

規格 §20.1 明訂不可「只靠音效表達必要資訊」。因此每一則廣播都會同時送出
一行文字說明；沒有音檔、沒有播放後端時，玩家收到的資訊完全一樣。

文字是廣播內容的**摘要**，不是逐字稿：實際音檔含國語、臺語、客語與英語
四種語言，逐字稿無法由檔名得知，寫成摘要才不會虛構內容（§2.3）。

沒有廣播設備的車輛
------------------

DR1000 型柴油客車沒有車上廣播設備，因此這型車不播廣播，也不送出廣播
文字——沒有播出來的東西不應該假裝有。由 ``trains.json`` 的
``has_broadcast`` 決定，不是寫死車型代碼。

播放時機由廣播系統自己決定
--------------------------

:meth:`BroadcastSystem.update` 每個模擬步長收到一份 :class:`RunState`，自己
判斷該播什麼。運轉端只負責描述「現在的狀況」，不必知道任何一條線的廣播
規則——捷運的規則（往○○、宣導、終點變體）與臺鐵完全不同，全部收在
:mod:`railway_sim.audio.mrt_broadcast` 的子類別裡，運轉端一行都不用改。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from railway_sim.accessibility import messages as msg
from railway_sim.accessibility.announcer import Announcer, Priority
from railway_sim.audio.library import BroadcastLibrary
from railway_sim.audio.player import AudioPlayer

__all__ = ["BROADCAST_DEPART_KMH", "BroadcastSystem", "RunState"]

#: 視為「列車已啟動」的速度（公里／小時）。
#:
#: 「下一站」廣播是列車自車站啟動之後才播的，因此需要一個明確的啟動門檻；
#: 用大於零會在停妥判定的抖動下反覆觸發。
BROADCAST_DEPART_KMH = 3.0


@dataclass(frozen=True)
class RunState:
    """一個模擬步長裡與廣播有關的運轉狀況。

    只描述**事實**，不含任何「該播什麼」的判斷——那是廣播系統的責任。

    Attributes:
        at_station_id: 目前停妥在哪一個停靠站；行進中為 ``None``。
        next_stop_id: 前方第一個停靠站；全部跑完為 ``None``。
        previous_stop_id: 最近停靠過的車站，用來判斷「從哪裡來」與區間位置。
        origin_id: 本班次的起站。
        terminus_id: 本班次的終點站。
        service_class: 車種代碼（機捷的直達車與普通車廣播不同）。
    """

    speed_kmh: float
    at_station_id: str | None
    next_stop_id: str | None
    next_stop_name: str
    distance_to_next_stop_m: float
    previous_stop_id: str | None
    origin_id: str
    terminus_id: str
    service_class: str = ""

    @property
    def moving(self) -> bool:
        return self.speed_kmh >= BROADCAST_DEPART_KMH


@dataclass
class BroadcastSystem:
    """一列車的車上廣播。

    Attributes:
        library: 音檔索引。空的索引是合法狀態，只是不會有聲音。
        announcer: 文字播報出口。
        player: 播放後端；``None`` 表示這台機器放不出聲音（§20.1）。
        enabled: 本型車有沒有廣播設備。
        line_id: 目前路線，查詢音檔時優先使用同一條線的版本。
        called_station_ids: 本班次的停靠站，用來挑選分歧站的方向版本。
    """

    library: BroadcastLibrary
    announcer: Announcer
    player: AudioPlayer | None = None
    enabled: bool = True
    line_id: str = ""
    called_station_ids: tuple[str, ...] = ()

    played: list[str] = field(default_factory=list, init=False)
    """已播出的音檔索引鍵，供測試與診斷使用。"""

    #: 開始播放「到站廣播」的距離（公尺）。
    #:
    #: 比司機員的接近播報更早，因為到站廣播是完整的四語言錄音，終點站的版本
    #: 長達一分鐘；用八百公尺起播的話，時速一百公里只剩二十九秒，廣播會在
    #: 到站前被下一則蓋掉。捷運的站距短得多，因此子類別會改小這個值。
    arrival_distance_m: float = 1500.0

    _next_announced_for: str | None = field(default=None, init=False, repr=False)
    _arrival_announced: set[str] = field(default_factory=set, init=False, repr=False)

    # ------------------------------------------------------------------
    # 播放時機
    # ------------------------------------------------------------------
    def update(self, state: RunState) -> None:
        """依目前運轉狀況播放該播的廣播。

        一律以**下一個停靠站**為準，不照路線上的車站順序推進：自強號、區間快
        會通過許多車站，照順序播就會播出根本不停的站。

        「下一站」用「已播過的站」比對而不是「剛離站」這種瞬間事件，中途暫停
        或列車在站內前後移動都不會重播或漏播。
        """
        if not self.enabled or state.next_stop_id is None:
            return

        if self._next_announced_for != state.next_stop_id and state.moving:
            self._next_announced_for = state.next_stop_id
            self.announce_next_stop(state.next_stop_id, state.next_stop_name)

        if (
            state.next_stop_id not in self._arrival_announced
            and state.at_station_id is None
            and state.distance_to_next_stop_m <= self.arrival_distance_m
        ):
            self._arrival_announced.add(state.next_stop_id)
            self.announce_arrival(
                state.next_stop_id,
                state.next_stop_name,
                is_terminus=state.next_stop_id == state.terminus_id,
            )

    # ------------------------------------------------------------------
    def announce_next_stop(self, station_id: str, name_zh_tw: str) -> bool:
        """列車啟動後播報下一個停靠站。回傳是否播出（含文字）。"""
        return self._announce(
            station_id, "next", msg.broadcast_next_stop(name_zh_tw)
        )

    def announce_arrival(
        self, station_id: str, name_zh_tw: str, *, is_terminus: bool = False
    ) -> bool:
        """到達停靠站之前播報。

        ``is_terminus`` 為真且該站有終點廣播時播終點版本，否則播到站版本。
        """
        if is_terminus and self._find(station_id, "terminus") is not None:
            return self._announce(
                station_id, "terminus", msg.broadcast_terminus(name_zh_tw)
            )
        text = (
            msg.broadcast_terminus(name_zh_tw)
            if is_terminus
            else msg.broadcast_arriving(name_zh_tw)
        )
        return self._announce(station_id, "arrive", text)

    def announce_doors(self, side: str, *, opening: bool) -> bool:
        """車門開關廣播（§20.2「車門聲」）。

        音檔放在 ``common`` 資料夾（``DOOR.open``、``DOOR.close``）；目前
        來源資料尚未整理出這一組，因此通常只有文字。
        """
        if not self.enabled:
            return False
        kind = "open" if opening else "close"
        self._play(self._find("DOOR", kind))
        self.announcer.announce(
            msg.broadcast_doors(side, opening=opening), Priority.STATUS
        )
        return True

    # ------------------------------------------------------------------
    def _find(self, station_id: str, kind: str):
        return self.library.find_station_announcement(
            station_id,
            kind,
            line_id=self.line_id or None,
            called_station_ids=self.called_station_ids,
        )

    def _announce(self, station_id: str, kind: str, text: str) -> bool:
        if not self.enabled:
            return False
        self._play(self._find(station_id, kind))
        # 廣播是給旅客的資訊，優先級最低：它絕不可以蓋掉超速或冒進號誌
        # 這類安全訊息（§21.1）。
        self.announcer.announce(text, Priority.STATUS)
        return True

    def _play(self, clip) -> None:
        if clip is None or self.player is None:
            return
        # 廣播動輒數十秒，新的一則必須蓋掉還沒播完的舊的，否則「到站」會
        # 疊在「下一站」上面，兩則都聽不清楚。
        if self.player.play(clip.path, interrupt=True):
            self.played.append(clip.key)

    # ------------------------------------------------------------------
    @classmethod
    def disabled(cls, announcer: Announcer) -> BroadcastSystem:
        """建立一個什麼都不播的廣播系統（無廣播設備的車輛）。"""
        return cls(
            library=BroadcastLibrary.empty(), announcer=announcer, enabled=False
        )


def called_stations(stop_station_ids: Sequence[str]) -> tuple[str, ...]:
    """把停靠表整理成方向版本規則要用的形式。"""
    return tuple(stop_station_ids)
