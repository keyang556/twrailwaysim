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
from railway_sim.audio.broadcast import BROADCAST_DEPART_KMH, BroadcastSystem
from railway_sim.audio.library import BroadcastLibrary
from railway_sim.data_loader import GameData
from railway_sim.roles.driver import DriverSession
from tests.conftest import ManualClock, drive_to


class RecordingPlayer:
    """記下播了哪些音檔，不真的出聲。"""

    def __init__(self) -> None:
        self.played: list[str] = []
        self.interrupts: list[bool] = []

    def play(self, path: Path, *, interrupt: bool = False) -> bool:
        self.played.append(Path(path).name)
        self.interrupts.append(interrupt)
        return True

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def clips(tmp_path: Path) -> Path:
    for name in (
        "TAIPEI.next.ogg",
        "TAIPEI.arrive.ogg",
        "SHULIN.next.ogg",
        "SHULIN.arrive.ogg",
        "SHULIN.terminus.ogg",
        "WANHUA.next.ogg",
        "WANHUA.arrive.ogg",
    ):
        path = tmp_path / "west_north" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"clip")
    return tmp_path


def make_system(root: Path | None, *, enabled: bool = True) -> tuple[
    BroadcastSystem, Announcer, RecordingPlayer
]:
    announcer = Announcer(clock=ManualClock(), dedupe_seconds=0.0)
    player = RecordingPlayer()
    system = BroadcastSystem(
        library=BroadcastLibrary.load(root) if root else BroadcastLibrary.empty(),
        announcer=announcer,
        player=player,  # type: ignore[arg-type]
        enabled=enabled,
        line_id="west_north",
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
        system, announcer, player = make_system(clips)
        system.announce_arrival("TAIPEI", "臺北")
        assert player.played == ["TAIPEI.arrive.ogg"]
        assert texts(announcer) == ["車內廣播：臺北站快到了。"]

    def test_terminus_replaces_the_arrival_clip(self, clips: Path) -> None:
        """有終點廣播就不播原本的到站廣播。"""
        system, announcer, player = make_system(clips)
        system.announce_arrival("SHULIN", "樹林", is_terminus=True)
        assert player.played == ["SHULIN.terminus.ogg"]
        assert texts(announcer) == ["車內廣播：終點站樹林快到了。"]

    def test_terminus_falls_back_to_the_arrival_clip(self, clips: Path) -> None:
        """該站還沒有終點廣播時，回退使用原本的到站廣播。"""
        system, announcer, player = make_system(clips)
        system.announce_arrival("TAIPEI", "臺北", is_terminus=True)
        assert player.played == ["TAIPEI.arrive.ogg"]
        # 文字仍要說明這是終點站，不能因為音檔回退就降級。
        assert texts(announcer) == ["車內廣播：終點站臺北快到了。"]

    def test_a_new_broadcast_interrupts_the_previous_one(self, clips: Path) -> None:
        """廣播長達數十秒，疊著播兩則都聽不清楚。"""
        system, _, player = make_system(clips)
        system.announce_next_stop("TAIPEI", "臺北")
        system.announce_arrival("TAIPEI", "臺北")
        assert player.interrupts == [True, True]


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
        assert texts(announcer) == ["車內廣播：臺北站快到了。"]

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

        spoken = broadcast_texts(announcer)
        # 每一站都是「下一站」在前、「快到了」在後。
        assert spoken[0].startswith("車內廣播：下一站，")
        for first, second in zip(spoken[::2], spoken[1::2], strict=False):
            station = first.removeprefix("車內廣播：下一站，").removesuffix("。")
            assert second.startswith(f"車內廣播：{station}站快到了")

    def test_no_station_is_broadcast_twice(self, game_data: GameData) -> None:
        session, announcer, _ = make_driver(game_data, "2115")
        drive_to(session, 12000.0, respect_stops=True, max_seconds=2400.0)
        spoken = broadcast_texts(announcer)
        assert len(spoken) == len(set(spoken))


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

        assert broadcast_texts(announcer)[-1] == "車內廣播：終點站臺中快到了。"
        assert player.played[-1] == "TAICHUNG.terminus.ogg"
        assert "TAICHUNG.arrive.ogg" not in player.played

    def test_terminus_without_a_clip_falls_back(self, game_data: GameData) -> None:
        """區間車 2115 次終點彰化，彰化目前沒有終點廣播。"""
        session, announcer, player = make_driver(game_data, "2115")
        drive_to(session, session.route.length_m, respect_stops=True, max_seconds=9000.0)

        assert broadcast_texts(announcer)[-1] == "車內廣播：終點站彰化快到了。"
        assert player.played[-1] == "CHANGHUA.arrive.ogg"

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
