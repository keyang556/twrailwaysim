"""車上廣播的播放規則測試（規格 §20）。

規則來自使用者說明：

- 「站名」＝下一站，**前往下個停靠站、列車啟動之後**播放。
- 「到站」在**到達停靠站之前**播放。
- 「終點」在到達終點站之前播放；該站有終點廣播就不播到站，沒有則回退到站。
- 不是區間車就不能照每站順序，只播**下一個停靠站**。
- DR1000 沒有廣播設備，不播。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from railway_sim.accessibility.announcer import Announcer
from railway_sim.audio.broadcast import (
    BROADCAST_DEPART_KMH,
    BroadcastSystem,
    RunState,
)
from railway_sim.audio.library import BroadcastLibrary
from railway_sim.data_loader import GameData
from railway_sim.roles.driver import DriverSession
from tests.conftest import ManualClock, drive_to


class RecordingPlayer:
    """記下播了哪些音檔，不真的出聲。"""

    def __init__(self) -> None:
        self.played: list[str] = []
        self.interrupts: list[bool] = []
        self.looping: str | None = None
        self.stops = 0

    def play(
        self, path: Path, *, interrupt: bool = False, loop: bool = False
    ) -> bool:
        if interrupt:
            self.stop()
        self.played.append(Path(path).name)
        self.interrupts.append(interrupt)
        if loop:
            self.looping = Path(path).name
        return True

    def stop(self) -> None:
        self.stops += 1
        self.looping = None

    def close(self) -> None:
        pass


@pytest.fixture
def clips(tmp_path: Path) -> Path:
    for line, names in (
        (
            "west_north",
            (
                "TAIPEI.next.ogg",
                "TAIPEI.arrive.ogg",
                "SHULIN.next.ogg",
                "SHULIN.arrive.ogg",
                "SHULIN.terminus.ogg",
                "WANHUA.next.ogg",
                "WANHUA.arrive.ogg",
            ),
        ),
        (
            # 不屬於任何車站的廣播：車門聲、開門側、提醒。
            "common",
            (
                "DOOR.open.ogg",
                "DOOR.close.ogg",
                "DOOR.open.emu900.ogg",
                "DOOR.close.emu900.ogg",
                "DOOR.side.left.ogg",
                "DOOR.side.right.ogg",
                "NOTICE.do_not_board.ogg",
                "NOTICE.unscheduled_stop.ogg",
            ),
        ),
    ):
        for name in names:
            path = tmp_path / line / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"clip")
    return tmp_path


def make_system(
    root: Path | None,
    *,
    enabled: bool = True,
    rolling_stock_id: str = "",
    boarding_notice: bool = False,
    door_sides: dict[str, str] | None = None,
) -> tuple[BroadcastSystem, Announcer, RecordingPlayer]:
    announcer = Announcer(clock=ManualClock(), dedupe_seconds=0.0)
    player = RecordingPlayer()
    system = BroadcastSystem(
        library=BroadcastLibrary.load(root) if root else BroadcastLibrary.empty(),
        announcer=announcer,
        player=player,  # type: ignore[arg-type]
        enabled=enabled,
        line_id="west_north",
        rolling_stock_id=rolling_stock_id,
        boarding_notice=boarding_notice,
        door_sides=door_sides or {},
    )
    return system, announcer, player


def texts(announcer: Announcer) -> list[str]:
    announcer.flush()
    return announcer.texts()


class TestClipSelection:
    """挑哪一個音檔。"""

    def test_next_stop_plays_the_next_clip(self, clips: Path) -> None:
        system, announcer, player = make_system(clips)
        system.announce_next_stop("TAIPEI", "臺北")
        assert player.played == ["TAIPEI.next.ogg"]
        assert texts(announcer) == ["車內廣播：下一站，臺北。"]

    def test_arrival_plays_the_arrive_clip(self, clips: Path) -> None:
        """到站廣播之後接一句開門側，讓看不見月台的旅客知道往哪邊走。"""
        system, announcer, player = make_system(clips)
        system.announce_arrival("TAIPEI", "臺北")
        assert player.played == ["TAIPEI.arrive.ogg", "DOOR.side.left.ogg"]
        assert texts(announcer) == [
            "車內廣播：臺北站快到了。",
            "車內廣播：左側開門。",
        ]

    def test_terminus_replaces_the_arrival_clip(self, clips: Path) -> None:
        """有終點廣播就不播原本的到站廣播。"""
        system, announcer, player = make_system(clips)
        system.announce_arrival("SHULIN", "樹林", is_terminus=True)
        assert player.played == ["SHULIN.terminus.ogg", "DOOR.side.left.ogg"]
        assert texts(announcer)[0] == "車內廣播：終點站樹林快到了。"

    def test_terminus_falls_back_to_the_arrival_clip(self, clips: Path) -> None:
        """該站還沒有終點廣播時，回退使用原本的到站廣播。"""
        system, announcer, player = make_system(clips)
        system.announce_arrival("TAIPEI", "臺北", is_terminus=True)
        assert player.played == ["TAIPEI.arrive.ogg", "DOOR.side.left.ogg"]
        # 文字仍要說明這是終點站，不能因為音檔回退就降級。
        assert texts(announcer)[0] == "車內廣播：終點站臺北快到了。"

    def test_a_new_broadcast_interrupts_the_previous_one(self, clips: Path) -> None:
        """廣播長達數十秒，疊著播兩則都聽不清楚。

        接在到站之後的開門側是例外：那正是要跟著聽到的一句，因此排進佇列
        而不是蓋掉剛剛那一則。
        """
        system, _, player = make_system(clips)
        system.announce_next_stop("TAIPEI", "臺北")
        system.announce_arrival("TAIPEI", "臺北")
        assert player.interrupts == [True, True, False]


class TestGracefulDegradation:
    """沒有廣播不能讓程式出錯（§20.1：文字永遠存在）。"""

    def test_station_without_a_clip_still_gets_text(self, clips: Path) -> None:
        system, announcer, player = make_system(clips)
        system.announce_next_stop("FENGMING", "鳳鳴")
        assert player.played == []
        assert texts(announcer) == ["車內廣播：下一站，鳳鳴。"]

    def test_no_audio_files_at_all(self) -> None:
        system, announcer, player = make_system(None)
        system.announce_arrival("TAIPEI", "臺北")
        assert player.played == []
        assert texts(announcer) == [
            "車內廣播：臺北站快到了。",
            "車內廣播：左側開門。",
        ]

    def test_no_player_at_all(self, clips: Path) -> None:
        announcer = Announcer(clock=ManualClock(), dedupe_seconds=0.0)
        system = BroadcastSystem(
            library=BroadcastLibrary.load(clips), announcer=announcer, player=None
        )
        system.announce_next_stop("TAIPEI", "臺北")
        assert texts(announcer) == ["車內廣播：下一站，臺北。"]


class TestTrainWithoutBroadcastEquipment:
    """DR1000 沒有這項設備，音檔與文字都不送出。"""

    def test_nothing_is_played_or_said(self, clips: Path) -> None:
        system, announcer, player = make_system(clips, enabled=False)
        system.announce_next_stop("TAIPEI", "臺北")
        system.announce_arrival("TAIPEI", "臺北", is_terminus=True)
        system.announce_doors("left", opening=True)
        assert player.played == []
        assert texts(announcer) == []


class TestDoorBroadcast:
    def test_door_broadcast_has_text_even_without_a_clip(self, clips: Path) -> None:
        system, announcer, _ = make_system(clips)
        system.announce_doors("left", opening=True)
        assert texts(announcer) == ["車內廣播：左側車門即將開啟。"]


class TestDoorSounds:
    """開關門聲每一型車不同，用車輛型式當版本挑（規格 §20.2「車門聲」）。"""

    def test_the_rolling_stock_version_is_used(self, clips: Path) -> None:
        system, _, player = make_system(clips, rolling_stock_id="EMU900")
        system.announce_doors("left", opening=True)
        assert player.played == ["DOOR.open.emu900.ogg"]

    def test_it_falls_back_to_the_generic_clip(self, clips: Path) -> None:
        """還沒錄到這一型的門聲時退回通用版，不是整個不播。"""
        system, _, player = make_system(clips, rolling_stock_id="EMU3000")
        system.announce_doors("left", opening=True)
        assert player.played == ["DOOR.open.ogg"]

    def test_closing_uses_the_close_clip(self, clips: Path) -> None:
        system, _, player = make_system(clips, rolling_stock_id="EMU900")
        system.announce_doors("left", opening=False)
        assert player.played == ["DOOR.close.emu900.ogg"]


class TestBoardingNotice:
    """全車對號列車開門中持續播放「請勿上車」。"""

    def test_it_loops_while_the_doors_are_open(self, clips: Path) -> None:
        system, announcer, player = make_system(
            clips, rolling_stock_id="EMU3000", boarding_notice=True
        )
        system.announce_doors("left", opening=True)
        assert player.looping == "NOTICE.do_not_board.ogg"
        assert any("請勿上車" in t for t in texts(announcer))

    def test_closing_stops_it_at_once(self, clips: Path) -> None:
        """關門動作一開始就停，不能等這一輪播完。"""
        system, _, player = make_system(
            clips, rolling_stock_id="EMU3000", boarding_notice=True
        )
        system.announce_doors("left", opening=True)
        system.announce_doors("left", opening=False)
        assert player.looping is None

    def test_one_side_closing_does_not_stop_it_while_the_other_is_open(
        self, clips: Path
    ) -> None:
        """兩側都開著時關掉一側，另一側還能上人，提醒不能消失。"""
        system, _, player = make_system(
            clips, rolling_stock_id="EMU3000", boarding_notice=True
        )
        system.announce_doors("left", opening=True)
        system.announce_doors("right", opening=True)
        system.announce_doors("left", opening=False)
        assert player.looping == "NOTICE.do_not_board.ogg"
        system.announce_doors("right", opening=False)
        assert player.looping is None

    def test_closing_stops_it_even_without_a_close_clip(
        self, tmp_path: Path
    ) -> None:
        """這一型車沒有關門聲時，循環一樣要停——不能靠關門聲順便蓋掉。"""
        notice = tmp_path / "common" / "NOTICE.do_not_board.ogg"
        notice.parent.mkdir(parents=True)
        notice.write_bytes(b"clip")
        system, _, player = make_system(
            tmp_path, rolling_stock_id="EMU3000", boarding_notice=True
        )
        system.announce_doors("left", opening=True)
        assert player.looping == "NOTICE.do_not_board.ogg"
        system.announce_doors("left", opening=False)
        assert player.looping is None

    def test_other_stock_never_plays_it(self, clips: Path) -> None:
        system, announcer, player = make_system(clips, rolling_stock_id="EMU900")
        system.announce_doors("left", opening=True)
        assert player.looping is None
        assert not any("請勿上車" in t for t in texts(announcer))

    def test_shipped_data_marks_only_the_reserved_stock(
        self, game_data: GameData
    ) -> None:
        marked = {
            spec.id
            for spec in game_data.train_types.values()
            if spec.boarding_notice
        }
        assert marked == {"TEMU1000", "TEMU2000", "EMU3000"}


class TestUnscheduledStop:
    def test_it_has_text_even_without_a_clip(self) -> None:
        system, announcer, player = make_system(None)
        system.announce_unscheduled_stop()
        assert player.played == []
        assert texts(announcer) == ["車內廣播：本列車臨時停車，請旅客稍候。"]

    def test_it_plays_the_notice_clip(self, clips: Path) -> None:
        system, _, player = make_system(clips)
        system.announce_unscheduled_stop()
        assert player.played == ["NOTICE.unscheduled_stop.ogg"]


class TestDoorSideSelection:
    """到站廣播之後提醒哪一側開門，預設左側、待避改右側。"""

    def test_default_is_left(self, clips: Path) -> None:
        system, announcer, _ = make_system(clips)
        system.announce_arrival("TAIPEI", "臺北")
        assert texts(announcer)[-1] == "車內廣播：左側開門。"

    def test_a_registered_station_uses_the_other_side(self, clips: Path) -> None:
        system, announcer, player = make_system(
            clips, door_sides={"TAIPEI": "right"}
        )
        system.announce_arrival("TAIPEI", "臺北")
        assert player.played[-1] == "DOOR.side.right.ogg"
        assert texts(announcer)[-1] == "車內廣播：右側開門。"


# ----------------------------------------------------------------------
# 接到實際運轉上
# ----------------------------------------------------------------------
def broadcast_texts(announcer: Announcer) -> list[str]:
    announcer.flush()
    return [t for t in announcer.texts() if t.startswith("車內廣播")]


def make_driver(
    game_data: GameData, train_number: str
) -> tuple[DriverSession, Announcer, RecordingPlayer]:
    announcer = Announcer(clock=ManualClock(), dedupe_seconds=0.0, history_limit=5000)
    player = RecordingPlayer()
    session = DriverSession(
        data=game_data,
        service=game_data.service(train_number),
        announcer=announcer,
        player=player,  # type: ignore[arg-type]
    )
    return session, announcer, player


class TestTimingDuringAService:
    """播放時機。"""

    def test_nothing_is_broadcast_before_the_train_starts(
        self, game_data: GameData
    ) -> None:
        """「下一站」是列車啟動之後才播的。"""
        session, announcer, _ = make_driver(game_data, "2115")
        for _ in range(20):
            session.tick(session.clock.tick_s)
        assert session.train.current_speed_kmh < BROADCAST_DEPART_KMH
        assert broadcast_texts(announcer) == []

    def test_next_stop_is_broadcast_once_moving(self, game_data: GameData) -> None:
        session, announcer, _ = make_driver(game_data, "2115")
        session.train.power_notch = 5
        while session.train.current_speed_kmh < BROADCAST_DEPART_KMH:
            session.tick(session.clock.tick_s)
        session.tick(session.clock.tick_s)

        upcoming = session.next_scheduled_stop()
        assert upcoming is not None
        assert broadcast_texts(announcer) == [f"車內廣播：下一站，{upcoming.name_zh_tw}。"]

    def test_each_stop_gets_next_then_arrive(self, game_data: GameData) -> None:
        session, announcer, _ = make_driver(game_data, "2115")
        drive_to(session, 12000.0, respect_stops=True, max_seconds=2400.0)

        # 開門側是接在到站之後的附帶提醒，這裡看的是站名的順序。
        spoken = [t for t in broadcast_texts(announcer) if "開門。" not in t]
        # 每一站都是「下一站」在前、「快到了」在後。
        assert spoken[0].startswith("車內廣播：下一站，")
        for first, second in zip(spoken[::2], spoken[1::2], strict=False):
            station = first.removeprefix("車內廣播：下一站，").removesuffix("。")
            assert second.startswith(f"車內廣播：{station}站快到了")

    def test_no_station_is_broadcast_twice(self, game_data: GameData) -> None:
        session, announcer, _ = make_driver(game_data, "2115")
        drive_to(session, 12000.0, respect_stops=True, max_seconds=2400.0)
        # 開門側每一站都會播一次，本來就會重複；這裡查的是站名廣播。
        spoken = [t for t in broadcast_texts(announcer) if "開門。" not in t]
        assert len(spoken) == len(set(spoken))


class TestAligningIsNotDeparting:
    """停短了往前推一點修正停車位置時，不可播「下一站」（issue #12）。

    列車在修正時確實在動，但還沒離站——旅客會在車門都還沒關的月台上聽到
    下一站的廣播。真正的界線是車頭有沒有通過本站的停車位置。
    """

    def _state(self, **overrides) -> RunState:
        fields = {
            "speed_kmh": 5.0,
            "at_station_id": None,
            "next_stop_id": "TAIPEI",
            "next_stop_name": "臺北",
            "distance_to_next_stop_m": 8000.0,
            "previous_stop_id": "WANHUA",
            "origin_id": "WANHUA",
            "terminus_id": "SHULIN",
        }
        return RunState(**{**fields, **overrides})

    def test_creeping_forward_before_the_mark_plays_nothing(self, clips: Path) -> None:
        system, announcer, player = make_system(clips)
        system.update(self._state(aligning_at_id="WANHUA"))
        assert player.played == []
        assert texts(announcer) == []

    def test_passing_the_mark_releases_the_broadcast(self, clips: Path) -> None:
        system, _, player = make_system(clips)
        system.update(self._state(aligning_at_id="WANHUA"))
        system.update(self._state(aligning_at_id=None))
        assert player.played == ["TAIPEI.next.ogg"]

    def test_driver_blocks_it_until_the_train_passes_the_mark(
        self, game_data: GameData
    ) -> None:
        """實際運轉：在栗林站前四公尺停妥，再往前推到停車位置。"""
        session, announcer, _ = make_driver(game_data, "2115")
        lilin = session.route.stop_for_station("LILIN")
        assert lilin is not None

        session.train.position_m = lilin.position_m - 4.0
        session.tick(0.1)
        assert session.aligning_at() is not None
        announcer.clear_history()

        # 往前推：速度足以觸發「已啟動」，但車頭還沒到停車位置。
        session.train.position_m = lilin.position_m - 1.0
        session.train.current_speed_kmh = BROADCAST_DEPART_KMH + 1.0
        session.tick(0.1)
        assert broadcast_texts(announcer) == []

        # 通過停車位置之後才播。
        session.train.position_m = lilin.position_m + 1.0
        session.tick(0.1)
        assert session.aligning_at() is None
        upcoming = session.next_scheduled_stop()
        assert upcoming is not None
        assert broadcast_texts(announcer) == [
            f"車內廣播：下一站，{upcoming.name_zh_tw}。"
        ]


class TestUnscheduledStopDuringAService:
    """站外臨時停車：停在月台以外的地方才播（issue 的「臨停」）。"""

    def test_stopping_away_from_a_platform_is_announced(
        self, game_data: GameData
    ) -> None:
        session, announcer, _ = make_driver(game_data, "2115")
        first, second = session.stations[0], session.stations[1]
        # 兩站正中間，離任何一座月台都夠遠。
        session.train.position_m = (first.position_m + second.position_m) / 2
        assert not session.at_platform()
        session.tick(0.1)
        assert any("臨時停車" in t for t in broadcast_texts(announcer))

    def test_stopping_at_a_platform_is_not(self, game_data: GameData) -> None:
        session, announcer, _ = make_driver(game_data, "2115")
        session.tick(0.1)
        assert session.at_platform()
        assert not any("臨時停車" in t for t in broadcast_texts(announcer))

    def test_it_is_not_repeated_while_the_train_stays_put(
        self, game_data: GameData
    ) -> None:
        session, announcer, _ = make_driver(game_data, "2115")
        first, second = session.stations[0], session.stations[1]
        session.train.position_m = (first.position_m + second.position_m) / 2
        for _ in range(50):
            session.tick(0.1)
        spoken = [t for t in broadcast_texts(announcer) if "臨時停車" in t]
        assert len(spoken) == 1


class TestStoppingPatternRules:
    """一律以下一個停靠站為準，不照路線上的車站順序。"""

    def test_express_service_skips_the_stations_it_passes(
        self, game_data: GameData
    ) -> None:
        """自強號 223 次通過許多站，廣播只能出現它實際停靠的站。"""
        session, announcer, _ = make_driver(game_data, "223")
        drive_to(session, session.route.length_m, respect_stops=True, max_seconds=9000.0)

        stopping = {p.name_zh_tw for p in session.stations if p.must_stop}
        passing = {p.name_zh_tw for p in session.stations if not p.must_stop}
        spoken = "".join(broadcast_texts(announcer))
        assert spoken  # 確實有播
        for name in passing:
            assert name not in spoken
        # 起點站不會被播（它是「已經在的那一站」），其餘停靠站都要出現。
        for name in stopping - {session.stations[0].name_zh_tw}:
            assert name in spoken

    def test_terminus_clip_is_used_when_it_exists(self, game_data: GameData) -> None:
        """區間快 2016 次終點臺中，臺中有終點廣播。"""
        session, announcer, player = make_driver(game_data, "2016")
        drive_to(session, session.route.length_m, respect_stops=True, max_seconds=9000.0)

        spoken = [t for t in broadcast_texts(announcer) if "開門。" not in t]
        assert spoken[-1] == "車內廣播：終點站臺中快到了。"
        assert "TAICHUNG.terminus.ogg" in player.played
        assert "TAICHUNG.arrive.ogg" not in player.played

    def test_terminus_without_a_clip_falls_back(self, game_data: GameData) -> None:
        """區間車 1701 次終點新竹，新竹目前沒有終點廣播。

        沒有終點版本的車站不是資料缺漏——臺鐵只在部分車站錄了終點廣播。
        這時要退回播它的到站廣播，文字仍然說明這是終點站。
        """
        session, announcer, player = make_driver(game_data, "1701")
        drive_to(session, session.route.length_m, respect_stops=True, max_seconds=9000.0)

        spoken = [t for t in broadcast_texts(announcer) if "開門。" not in t]
        assert spoken[-1] == "車內廣播：終點站新竹快到了。"
        assert "HSINCHU.terminus.ogg" not in player.played
        assert "HSINCHU.arrive.ogg" in player.played

    def test_direction_variant_follows_the_service(self, game_data: GameData) -> None:
        """區間車 1104 次開往基隆，八堵要播「往基隆」的版本。"""
        session, _, player = make_driver(game_data, "1104")
        drive_to(session, session.route.length_m, respect_stops=True, max_seconds=9000.0)
        assert "BADU.next.keelung.ogg" in player.played
        assert "BADU.next.hualien.ogg" not in player.played


class TestServiceWithoutEquipment:
    def test_dr1000_service_broadcasts_nothing(self, game_data: GameData) -> None:
        session, announcer, player = make_driver(game_data, "1801")
        assert session.spec.id == "DR1000"
        assert session.broadcast.enabled is False

        drive_to(session, 15000.0, respect_stops=True, max_seconds=3000.0)
        assert broadcast_texts(announcer) == []
        assert player.played == []

    def test_briefing_says_why_there_is_no_broadcast(self, game_data: GameData) -> None:
        session, _, _ = make_driver(game_data, "1801")
        assert "沒有車上廣播設備" in session.broadcast_status_text()
