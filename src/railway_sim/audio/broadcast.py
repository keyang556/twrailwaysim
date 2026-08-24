"""車上廣播（規格 §20.2「到站廣播」）。

播放時機
--------

``next``（下一站）
    列車自車站**離站之後**播放，內容是下一個停靠站。停短了往前推一點修正
    停車位置時列車也在動，但那不算離站；界線是車頭有沒有通過該站的停車
    位置（見 :attr:`RunState.departed`）。

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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from railway_sim.accessibility import messages as msg
from railway_sim.accessibility.announcer import Announcer, Priority
from railway_sim.audio.library import BroadcastLibrary
from railway_sim.audio.player import AudioPlayer
from railway_sim.timetable.door_side import DEFAULT_DOOR_SIDE

__all__ = [
    "BROADCAST_DEPART_KMH",
    "DOOR_CLIP_ID",
    "NOTICE_CLIP_ID",
    "BroadcastSystem",
    "RunState",
]

#: 視為「列車已啟動」的速度（公里／小時）。
#:
#: 「下一站」廣播是列車自車站啟動之後才播的，因此需要一個明確的啟動門檻；
#: 用大於零會在停妥判定的抖動下反覆觸發。
BROADCAST_DEPART_KMH = 3.0

#: 車門相關音檔的「車站代碼」。
#:
#: 索引以車站代碼為鍵（見 :mod:`railway_sim.audio.library`），車門聲不屬於
#: 任何一站，因此給它一個保留字。種類為 ``open``／``close``／``side``。
DOOR_CLIP_ID = "DOOR"

#: 不屬於任何車站的提醒廣播代碼（``NOTICE.do_not_board``、
#: ``NOTICE.unscheduled_stop``）。
NOTICE_CLIP_ID = "NOTICE"


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
        aligning_at_id: 還在對準停車位置的那一站；車頭通過該站的停車位置
            之後為 ``None``。
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
    aligning_at_id: str | None = None

    @property
    def moving(self) -> bool:
        return self.speed_kmh >= BROADCAST_DEPART_KMH

    @property
    def departed(self) -> bool:
        """列車是否真的離站了。

        停短了往前推一點修正停車位置時，列車一樣在動——但那不是離站，
        此時播「下一站」是錯的（issue #12）。真正的界線是車頭有沒有通過
        該站的停車位置，那正是 ``aligning_at_id`` 歸 ``None`` 的時候。
        """
        return self.moving and self.aligning_at_id is None


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
        rolling_stock_id: 車輛型式代碼。開關門聲每一型車不同，用它挑版本
            （``DOOR.open.emu900``）；沒有該型的版本就退回通用的
            ``DOOR.open``，不會因為多了一型反而整個播不出來。
        boarding_notice: 車門開啟中是否持續播放「請勿上車」（全車對號的
            車型才有，由 ``trains.json`` 的同名欄位決定）。
        door_sides: ``{車站代碼: "left"／"right"}``，本班次在各站的開門側。
            沒有登記的車站一律當成預設的左側（見 :data:`DEFAULT_DOOR_SIDE`）。
    """

    library: BroadcastLibrary
    announcer: Announcer
    player: AudioPlayer | None = None
    enabled: bool = True
    line_id: str = ""
    called_station_ids: tuple[str, ...] = ()
    rolling_stock_id: str = ""
    boarding_notice: bool = False
    door_sides: Mapping[str, str] = field(default_factory=dict)

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
    _boarding_notice_playing: bool = field(default=False, init=False, repr=False)
    _open_sides: set[str] = field(default_factory=set, init=False, repr=False)

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

        if self._next_announced_for != state.next_stop_id and state.departed:
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
        """到達停靠站之前播報，接著提醒開門側。

        ``is_terminus`` 為真且該站有終點廣播時播終點版本，否則播到站版本。

        開門側是**接在到站廣播之後**的一小則，因此排進佇列而不是蓋掉前一則
        （見 :meth:`announce_door_side`）。
        """
        if is_terminus and self._find(station_id, "terminus") is not None:
            spoken = self._announce(
                station_id, "terminus", msg.broadcast_terminus(name_zh_tw)
            )
        else:
            text = (
                msg.broadcast_terminus(name_zh_tw)
                if is_terminus
                else msg.broadcast_arriving(name_zh_tw)
            )
            spoken = self._announce(station_id, "arrive", text)
        if spoken:
            self.announce_door_side(self.door_side_at(station_id))
        return spoken

    def door_side_at(self, station_id: str) -> str:
        """本班次在某一站的開門側。沒有登記的車站一律是預設側。"""
        return self.door_sides.get(station_id, DEFAULT_DOOR_SIDE)

    def announce_door_side(self, side: str) -> bool:
        """提醒哪一側開門。接在到站廣播之後，不蓋掉它。"""
        if not self.enabled:
            return False
        self._play(self._find_door("side", variant=side), interrupt=False)
        self.announcer.announce(msg.broadcast_door_side(side), Priority.STATUS)
        return True

    def announce_doors(self, side: str, *, opening: bool) -> bool:
        """車門開關廣播（§20.2「車門聲」）。

        開關門聲**每一型車不一樣**，因此以車輛型式為版本查詢
        （``DOOR.open.emu900``）；該型沒有錄到就退回通用的 ``DOOR.open``。
        新增一型車只要把音檔放進 ``common`` 資料夾，程式不必改。

        全車對號的車型在開門中還要持續播放「請勿上車」，關門動作一開始就
        立即停止（見 :meth:`_update_boarding_notice`）。
        """
        if not self.enabled:
            return False
        if opening:
            self._open_sides.add(side)
        else:
            self._open_sides.discard(side)
        kind = "open" if opening else "close"
        # 關門聲要蓋掉還在循環的「請勿上車」，開門聲不必蓋掉任何東西。
        interrupted = not opening
        self._play(
            self._find_door(kind, variant=self.rolling_stock_id),
            interrupt=interrupted,
        )
        self.announcer.announce(
            msg.broadcast_doors(side, opening=opening), Priority.STATUS
        )
        self._update_boarding_notice(
            any_open=bool(self._open_sides), interrupted=interrupted
        )
        return True

    def _update_boarding_notice(self, *, any_open: bool, interrupted: bool) -> None:
        """車門開啟中持續播放「請勿上車」，兩側都關上才停止。

        提醒的對象是月台上**沒有買這班列車車票**的旅客，因此必須在整段開門
        時間裡一直播，播一次就停沒有意義；左右兩側各自獨立開關（§16.2），
        只關掉其中一側時另一側還能上人，因此要等 :attr:`_open_sides` 全空
        才算真的關門，不能因為單側關門的動作就停。

        麻煩的是**關門聲本身一定會蓋掉循環**（見上面的 ``interrupted``），
        不管另一側是不是還開著；因此單側關門、另一側仍開著時，播放器裡的
        循環其實已經被這一次關門聲打斷了，必須重新排入，不能只看
        :attr:`_boarding_notice_playing` 這個旗標就以為它還在播。
        """
        if not self.boarding_notice:
            return
        if not any_open:
            # 這一型車沒有關門聲時，上面那一步不會去動播放器，循環就會一直
            # 播下去；因此停止循環要自己明說，不能靠關門聲順便把它蓋掉。
            if self._boarding_notice_playing and self.player is not None:
                self.player.stop()
            self._boarding_notice_playing = False
            return
        if self._boarding_notice_playing and not interrupted:
            return
        clip = self._find_notice("do_not_board")
        self.announcer.announce(msg.broadcast_do_not_board(), Priority.STATUS)
        if clip is None or self.player is None:
            return
        if self.player.play(clip.path, loop=True):
            self._boarding_notice_playing = True
            self.played.append(clip.key)

    def announce_unscheduled_stop(self) -> bool:
        """臨時停車（不在月台的地方停下來）的廣播。

        號誌、前方列車或事故造成的站外停車，旅客只知道車忽然不動了；這一則
        就是告訴他們「這是臨時停車」。回到月台範圍內停車不算，那是正常到站。
        """
        if not self.enabled:
            return False
        self._play(self._find_notice("unscheduled_stop"))
        self.announcer.announce(msg.broadcast_unscheduled_stop(), Priority.STATUS)
        return True

    # ------------------------------------------------------------------
    def _find(self, station_id: str, kind: str):
        return self.library.find_station_announcement(
            station_id,
            kind,
            line_id=self.line_id or None,
            called_station_ids=self.called_station_ids,
        )

    def _find_door(self, kind: str, *, variant: str = ""):
        """車門相關音檔。版本查不到時退回沒有版本的通用檔。"""
        return self.library.find(
            DOOR_CLIP_ID, kind, line_id=self.line_id or None, variant=variant or None
        )

    def _find_notice(self, kind: str):
        return self.library.find(NOTICE_CLIP_ID, kind, line_id=self.line_id or None)

    def _announce(self, station_id: str, kind: str, text: str) -> bool:
        if not self.enabled:
            return False
        self._play(self._find(station_id, kind))
        # 廣播是給旅客的資訊，優先級最低：它絕不可以蓋掉超速或冒進號誌
        # 這類安全訊息（§21.1）。
        self.announcer.announce(text, Priority.STATUS)
        return True

    def _play(self, clip, *, interrupt: bool = True) -> None:
        if clip is None or self.player is None:
            return
        # 廣播動輒數十秒，新的一則必須蓋掉還沒播完的舊的，否則「到站」會
        # 疊在「下一站」上面，兩則都聽不清楚。接在後面的短句（開門側）例外，
        # 那正是要跟在到站廣播之後聽到的。
        if self.player.play(clip.path, interrupt=interrupt):
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
