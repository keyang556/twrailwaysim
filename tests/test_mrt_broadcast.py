"""捷運車上廣播的播放規則（規格 §20.2）。

每一項都對應使用者指定的一句規則，並且**驗證實際播出的音檔**而不只是文字：
播錯版本（該播終點卻播了正常版）在文字上看起來幾乎一樣，只有檔名分得出來。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from railway_sim.accessibility.announcer import Announcer
from railway_sim.audio.broadcast import RunState
from railway_sim.audio.mrt_broadcast import MrtBroadcastRules
from railway_sim.roles.driver import DriverSession
from tests.conftest import ManualClock


class RecordingPlayer:
    """記下播了哪些音檔的假播放後端。"""

    def __init__(self) -> None:
        self.clips: list[str] = []

    def play(self, path: Path, *, interrupt: bool = False) -> bool:
        self.clips.append(path.name)
        return True

    def close(self) -> None:  # pragma: no cover - 介面完整性
        pass


def drive(mrt_data, number: str) -> tuple[DriverSession, RecordingPlayer]:
    """用自動駕駛把一個營運模式從頭跑到尾，回傳播出的音檔。

    用 ATO 開而不是自己寫控制迴圈：廣播時機取決於「什麼時候停妥、什麼時候
    起步」，用遊戲本身的自動駕駛跑出來的時序才是玩家真正會聽到的。
    """
    announcer = Announcer(clock=ManualClock(), dedupe_seconds=0.0)
    player = RecordingPlayer()
    session = DriverSession(
        data=mrt_data,
        service=mrt_data.service(number),
        announcer=announcer,
        player=player,  # type: ignore[arg-type]
    )
    session.toggle_ato()
    for _ in range(400_000):
        if session.finished:
            break
        session.tick(0.1)
        if (
            session.stopped_at() is not None
            and session.train.is_stopped
            and not session.ato.departure_authorised
        ):
            session.ato_depart()
    assert session.finished, f"{number} 沒有跑完"
    return session, player


class TestDestinationAnnouncement:
    """往○○的廣播（以終點站代碼命名的音檔）。"""

    def test_台北捷運在發車前播往哪裡(self, mrt_data):
        _, player = drive(mrt_data, "R1002")  # 象山－淡水，往淡水
        assert "R28.destination.ogg" in player.clips
        # 第一則就是目的地廣播：發車之前、列車還沒啟動時播。
        assert player.clips[0] == "R28.destination.ogg"

    def test_每一站發車前都會再播一次(self, mrt_data):
        _, player = drive(mrt_data, "R1002")
        assert player.clips.count("R28.destination.ogg") == 26

    def test_文湖線在發車後播(self, mrt_data):
        """條目與使用者指定：文湖線的往○○是發車之後才播。"""
        _, player = drive(mrt_data, "BR1001")
        assert "BR24.destination.ogg" in player.clips
        # 第一則到站廣播之前一定先有一則目的地廣播（離開動物園之後）。
        assert player.clips.index("BR24.destination.ogg") < player.clips.index(
            "BR02.arrive.ogg"
        )

    def test_到了終點就不再播開往哪裡(self, mrt_data):
        _, player = drive(mrt_data, "R1002")
        assert player.clips[-1] == "R28.arrive.ogg" or player.clips[-1].startswith("R28.")
        assert player.clips[-1] != "R28.destination.ogg"

    def test_支線用支線的目的地廣播(self, mrt_data):
        _, player = drive(mrt_data, "R1052")  # 北投－新北投
        assert player.clips[0] == "R22A.destination.ogg"

    def test_三鶯線與機捷沒有目的地廣播(self, mrt_data):
        for number in ("LB1001", "A1001"):
            _, player = drive(mrt_data, number)
            assert not [c for c in player.clips if ".destination." in c]


class TestArrivalVariants:
    """同一站的不同版本：終點、方向、車種。"""

    def test_大安為終點時播終點版(self, mrt_data):
        _, player = drive(mrt_data, "R1005")  # 北投－大安，往大安
        assert "R05.terminus.ogg" in player.clips
        assert "R05.arrive.ogg" not in player.clips

    def test_大安非終點時播正常版(self, mrt_data):
        _, player = drive(mrt_data, "R1001")  # 淡水－象山，途經大安
        assert "R05.arrive.ogg" in player.clips
        assert "R05.terminus.ogg" not in player.clips

    def test_往北投的列車在奇岩播特別版(self, mrt_data):
        _, player = drive(mrt_data, "R1004")  # 象山－北投
        assert "R21.arrive.terminus_next.ogg" in player.clips
        assert "R21.arrive.ogg" not in player.clips
        assert "R22.terminus.ogg" in player.clips

    def test_經過奇岩不停北投的列車播正常版(self, mrt_data):
        _, player = drive(mrt_data, "R1002")  # 象山－淡水，經奇岩、北投
        assert "R21.arrive.ogg" in player.clips
        assert "R22.arrive.ogg" in player.clips
        assert "R21.arrive.terminus_next.ogg" not in player.clips

    def test_從新北投來的列車在北投播支線版(self, mrt_data):
        _, player = drive(mrt_data, "R1051")  # 新北投－北投
        assert "R22.terminus.from_branch.ogg" in player.clips
        assert "R22.terminus.ogg" not in player.clips

    def test_從小碧潭來的列車在七張播支線版(self, mrt_data):
        _, player = drive(mrt_data, "G1051")  # 小碧潭－七張
        assert "G03.terminus.from_branch.ogg" in player.clips

    def test_台電大樓為終點時播終點版(self, mrt_data):
        _, player = drive(mrt_data, "G1003")  # 松山－台電大樓
        assert "G08.terminus.ogg" in player.clips
        assert "G08.arrive.ogg" not in player.clips

    def test_板南線兩個區間終點站(self, mrt_data):
        _, player = drive(mrt_data, "BL1004")  # 南港展覽館－亞東醫院
        assert "BL05.terminus.ogg" in player.clips
        _, player = drive(mrt_data, "BL1005")  # 頂埔－昆陽
        assert "BL21.terminus.ogg" in player.clips

    def test_昆陽非終點時播正常版(self, mrt_data):
        _, player = drive(mrt_data, "BL1001")  # 頂埔－南港展覽館
        assert "BL21.arrive.ogg" in player.clips
        assert "BL21.terminus.ogg" not in player.clips


class TestNextStationAnnouncement:
    """離站後的下一站廣播：三鶯線每一站都有，機捷只有幾站。"""

    def test_三鶯線每一站都有下一站廣播(self, mrt_data):
        session, player = drive(mrt_data, "LB1001")
        stops = [p.station_id for p in session.stations if p.must_stop][1:]
        for code in stops:
            assert f"{code}.next.ogg" in player.clips
            assert f"{code}.arrive.ogg" in player.clips

    def test_三鶯線先播下一站再播到站(self, mrt_data):
        _, player = drive(mrt_data, "LB1001")
        assert player.clips.index("LB02.next.ogg") < player.clips.index("LB02.arrive.ogg")

    def test_台北捷運沒有下一站廣播(self, mrt_data):
        _, player = drive(mrt_data, "R1001")
        assert not [c for c in player.clips if ".next." in c]

    def test_機捷只有指定的幾站有下一站廣播(self, mrt_data):
        _, player = drive(mrt_data, "A1001")  # 普通車，台北車站－老街溪
        assert [c for c in player.clips if ".next." in c] == [
            "A2.next.ogg",
            "A3.next.ogg",
            "A18.next.ogg",
        ]


class TestAirportExpress:
    """機場捷運的直達車與普通車廣播不同。"""

    def test_直達車終點機場第二航廈播專用版(self, mrt_data):
        _, player = drive(mrt_data, "A1003")
        assert "A13.next.express.ogg" in player.clips
        assert "A13.terminus.express.ogg" in player.clips
        assert "A13.arrive.ogg" not in player.clips

    def test_普通車在機場第二航廈只播一般到站(self, mrt_data):
        _, player = drive(mrt_data, "A1001")
        assert "A13.arrive.ogg" in player.clips
        assert not [c for c in player.clips if "express" in c]

    def test_環北直達車經過機場第二航廈播一般到站(self, mrt_data):
        """13-1／13-2 只屬於終點為機場第二航廈的直達車。"""
        _, player = drive(mrt_data, "A1005")
        assert "A13.arrive.ogg" in player.clips
        assert not [c for c in player.clips if "express" in c]

    def test_環北為終點時播終點版(self, mrt_data):
        _, player = drive(mrt_data, "A1005")
        assert "A21.terminus.ogg" in player.clips

    def test_環北非終點時播正常版(self, mrt_data):
        _, player = drive(mrt_data, "A1001")  # 普通車續往老街溪
        assert "A21.arrive.ogg" in player.clips
        assert "A21.terminus.ogg" not in player.clips


class TestNotice:
    """宣導廣播：指定區間、指定方向。"""

    def test_淡水信義線在竹圍到紅樹林之間播(self, mrt_data):
        _, player = drive(mrt_data, "R1002")  # 往淡水
        assert "NOTICE.notice.ogg" in player.clips
        # 在竹圍到站之後、紅樹林到站之前——中間還會夾一則發車前的目的地廣播。
        index = player.clips.index("NOTICE.notice.ogg")
        assert player.clips.index("R26.arrive.ogg") < index
        assert index < player.clips.index("R27.arrive.ogg")

    def test_反方向不播(self, mrt_data):
        _, player = drive(mrt_data, "R1001")  # 往象山
        assert "NOTICE.notice.ogg" not in player.clips

    def test_往台電大樓過了中正紀念堂才播(self, mrt_data):
        _, player = drive(mrt_data, "G1003")
        index = player.clips.index("NOTICE.notice.ogg")
        assert player.clips.index("G10.arrive.ogg") < index
        assert index < player.clips.index("G09.arrive.ogg")

    def test_往新店的不播(self, mrt_data):
        """同樣經過中正紀念堂往南，但終點不是台電大樓。"""
        _, player = drive(mrt_data, "G1001")
        assert "NOTICE.notice.ogg" not in player.clips

    def test_每一趟只播一次(self, mrt_data):
        _, player = drive(mrt_data, "R1002")
        assert player.clips.count("NOTICE.notice.ogg") == 1


class TestRuleFile:
    """規則檔本身。壞掉的規則檔不該讓遊戲開不起來。"""

    def test_讀不到規則檔只是沒有規則(self, tmp_path):
        rules = MrtBroadcastRules.load(tmp_path / "不存在.json")
        assert rules.lines == {}

    def test_格式壞掉也不會擲出例外(self, tmp_path):
        path = tmp_path / "broadcast_rules.json"
        path.write_text("{ 這不是 JSON", encoding="utf-8")
        assert MrtBroadcastRules.load(path).lines == {}

    def test_沒登記的線別只有到站廣播(self, mrt_data):
        rules = mrt_data.broadcast_rules.for_line("沒有這條線", "taipei_metro")
        state = RunState(
            speed_kmh=50.0,
            at_station_id=None,
            next_stop_id="X01",
            next_stop_name="測試",
            distance_to_next_stop_m=100.0,
            previous_stop_id="X00",
            origin_id="X00",
            terminus_id="X09",
        )
        assert rules.next_variant(state, "X01") is None
        assert rules.arrival_variant(state, "X01") == ""
        assert rules.notice_for(state) is None

    def test_臺鐵沒有捷運規則(self, game_data):
        assert game_data.broadcast_rules.lines == {}


class TestTraUnaffected:
    """臺鐵的廣播行為不因這次改動而變。"""

    def test_臺鐵仍然播下一站與到站(self, game_data):
        from tests.conftest import make_session

        session = make_session(game_data, "2115")
        assert session.broadcast.__class__.__name__ == "BroadcastSystem"
        assert session.broadcast.arrival_distance_m == pytest.approx(1500.0)
