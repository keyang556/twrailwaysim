"""自動駕駛與 ATO（規格 §20.3）。

分工是這個功能的重點，因此測試也照分工分成三組：

- 有沒有 ATO（臺鐵沒有）
- 一般捷運線：電腦開車，駕駛負責開關門與發車
- 無人駕駛線：連車門與發車都不用駕駛動手
"""

from __future__ import annotations

import pytest

from railway_sim.accessibility.announcer import Announcer
from railway_sim.input.keymap import Keymap
from railway_sim.roles.driver import DriverSession
from railway_sim.simulation.ato import DEFAULT_DOOR_SIDE, AtoController
from railway_sim.simulation.train import Train
from tests.conftest import ManualClock, make_session


def make_mrt_session(mrt_data, number: str) -> DriverSession:
    """建立捷運工作階段。

    ``history`` 只在 ``flush()`` 之後才有內容，因此測試裡每一步都會 flush；
    上限也調高，一整趟車的播報才不會把前面的擠掉。
    """
    return DriverSession(
        data=mrt_data,
        service=mrt_data.service(number),
        announcer=Announcer(clock=ManualClock(), dedupe_seconds=0.0, history_limit=20_000),
    )


def run(session: DriverSession, *, depart: bool, limit: int = 400_000) -> int:
    """把工作階段跑到結束，回傳花了幾個步長。

    ``depart=True`` 時模擬駕駛：停妥就按發車鍵。``False`` 則完全不操作，
    用來驗證無人駕駛線真的不需要任何動作。
    """
    ticks = 0
    while not session.finished and ticks < limit:
        session.tick(0.1)
        session.announcer.flush()
        if (
            depart
            and session.ato.engaged
            and session.train.is_stopped
            and session.stopped_at() is not None
            and not session.ato.departure_authorised
        ):
            session.ato_depart()
        ticks += 1
    return ticks


class TestAvailability:
    def test_臺鐵沒有自動駕駛(self, game_data, announcer):
        session = make_session(game_data, "2115", announcer)
        assert not session.ato.available
        session.toggle_ato()
        announcer.flush()
        assert not session.ato.engaged
        assert "本系統沒有自動駕駛功能" in announcer.history[-1].text

    def test_捷運可以啟動(self, mrt_data):
        session = make_mrt_session(mrt_data, "R1001")
        session.toggle_ato()
        assert session.ato.engaged

    def test_再按一次解除(self, mrt_data):
        session = make_mrt_session(mrt_data, "R1001")
        session.toggle_ato()
        session.toggle_ato()
        assert not session.ato.engaged

    def test_無人駕駛線標記正確(self, mrt_data):
        for number in ("BR1001", "Y1001", "LB1001"):
            assert make_mrt_session(mrt_data, number).ato.driverless
        for number in ("R1001", "BL1001", "A1001"):
            assert not make_mrt_session(mrt_data, number).ato.driverless

    def test_快捷鍵為_alt_shift_u(self, game_data):
        keymap = Keymap.from_dict(game_data.keymap_raw, "driver")
        assert keymap.action_for("Alt+Shift+U") == "toggle_ato"
        assert keymap.conflicts() == []


class TestDriverAssisted:
    """一般捷運線：駕駛只負責開關門與發車。"""

    def test_開了自動駕駛不按發車就不會走(self, mrt_data):
        session = make_mrt_session(mrt_data, "R1051")
        session.toggle_ato()
        run(session, depart=False, limit=1200)
        assert session.train.is_stopped
        assert session.train.position_m == pytest.approx(0.0)
        assert not session.finished

    def test_停妥時會提醒駕駛按發車鍵(self, mrt_data):
        session = make_mrt_session(mrt_data, "R1051")
        session.toggle_ato()
        session.tick(0.1)
        session.announcer.flush()
        assert any(
            "請按發車鍵" in item.text for item in session.announcer.history
        )

    def test_按了發車就自己開到終點(self, mrt_data):
        session = make_mrt_session(mrt_data, "R1051")
        session.toggle_ato()
        run(session, depart=True)
        assert session.finished
        assert session.incidents.violation_count == 0
        assert all(p.served for p in session.stations if p.must_stop)

    def test_全程都停得準(self, mrt_data):
        """ATO 的停車誤差應該遠小於應停未停的容許範圍。"""
        session = make_mrt_session(mrt_data, "BL1003")
        session.toggle_ato()
        run(session, depart=True)
        offsets = [p.stop_offset_m for p in session.stations if p.served and p.stop_offset_m]
        assert offsets
        assert max(abs(offset) for offset in offsets) < 5.0

    def test_每一站都要重新按一次發車(self, mrt_data):
        """在前一站按過的發車不可以延續到下一站。"""
        session = make_mrt_session(mrt_data, "BL1003")
        session.toggle_ato()
        session.tick(0.1)
        session.ato_depart()
        assert session.ato.departure_authorised
        run(session, depart=False, limit=20_000)
        assert session.train.is_stopped
        assert not session.ato.departure_authorised
        # 只開了一站就停在第二站等發車。
        served = [p for p in session.stations if p.served]
        assert len(served) == 2

    def test_車門開著時不會發車(self, mrt_data):
        session = make_mrt_session(mrt_data, "R1051")
        session.toggle_ato()
        session.toggle_left_doors()
        session.ato_depart()
        assert not session.ato.departure_authorised

    def test_未啟動自動駕駛時發車鍵有回饋(self, mrt_data):
        session = make_mrt_session(mrt_data, "R1001")
        session.ato_depart()
        session.announcer.flush()
        assert "自動駕駛未啟動" in session.announcer.history[-1].text

    def test_電門鍵在自動駕駛下就是發車鍵(self, mrt_data):
        session = make_mrt_session(mrt_data, "R1051")
        session.toggle_ato()
        session.power_up()
        assert session.ato.departure_authorised


class TestDriverless:
    """文湖線、環狀線、三鶯線：完全不需要駕駛動作。"""

    @pytest.mark.parametrize("number", ["BR1001", "Y1001", "LB1001"])
    def test_不做任何操作也能跑完全程(self, mrt_data, number):
        session = make_mrt_session(mrt_data, number)
        session.toggle_ato()
        run(session, depart=False)
        assert session.finished
        assert session.incidents.violation_count == 0
        assert all(p.served for p in session.stations if p.must_stop)

    def test_到站自己開門離站前自己關門(self, mrt_data):
        session = make_mrt_session(mrt_data, "LB1001")
        session.toggle_ato()
        run(session, depart=False)
        texts = [item.text for item in session.announcer.history]
        assert sum(1 for t in texts if "車門開啟" in t) >= 11
        assert sum(1 for t in texts if "車門關閉" in t) >= 11
        # 跑完之後車門不該還開著（最後一站的門由停站計時關上）。
        assert not session.train.any_door_open

    def test_行進中車門一定是關的(self, mrt_data):
        session = make_mrt_session(mrt_data, "LB1001")
        session.toggle_ato()
        for _ in range(200_000):
            if session.finished:
                break
            session.tick(0.1)
            if session.train.current_speed_kmh > 1.0:
                assert not session.train.any_door_open
        assert session.finished


class TestControlLaw:
    """控制律本身。用最小的假列車測，不必跑完整條線。"""

    def test_緊急制軔時完全不介入(self, train_spec, train):
        ato = AtoController(spec=train_spec, available=True, engaged=True)
        train.emergency_brake = True
        train.brake_notch = 4
        decision = ato.decide(
            train, permitted_kmh=80.0, distance_to_stop_m=None, holding=False
        )
        assert decision.reason == "emergency"
        assert decision.power_notch == 0
        assert decision.brake_notch == 4

    def test_車門開著不加電門(self, train_spec, train):
        ato = AtoController(spec=train_spec, available=True, engaged=True)
        train.left_doors_open = True
        decision = ato.decide(
            train, permitted_kmh=80.0, distance_to_stop_m=None, holding=False
        )
        assert decision.power_notch == 0
        assert decision.braking

    def test_停妥待發時保持制軔(self, train_spec, train):
        ato = AtoController(spec=train_spec, available=True, engaged=True)
        decision = ato.decide(
            train, permitted_kmh=80.0, distance_to_stop_m=1000.0, holding=True
        )
        assert decision.power_notch == 0
        assert decision.braking

    def test_低於目標速度就加速(self, train_spec, train):
        ato = AtoController(spec=train_spec, available=True, engaged=True)
        train.current_speed_kmh = 20.0
        decision = ato.decide(
            train, permitted_kmh=80.0, distance_to_stop_m=5000.0, holding=False
        )
        assert decision.reason == "accelerate"
        assert decision.power_notch == train_spec.power_notches

    def test_接近停車點就減速(self, train_spec, train):
        ato = AtoController(spec=train_spec, available=True, engaged=True)
        train.current_speed_kmh = 72.0  # 20 m/s
        # 停在 200 公尺外需要 1.0 m/s²，正好是全常用制軔。
        decision = ato.decide(
            train, permitted_kmh=80.0, distance_to_stop_m=200.0, holding=False
        )
        assert decision.reason == "approach"
        assert decision.brake_notch == train_spec.brake_notches

    def test_距離很遠不必減速(self, train_spec, train):
        ato = AtoController(spec=train_spec, available=True, engaged=True)
        train.current_speed_kmh = 72.0
        decision = ato.decide(
            train, permitted_kmh=80.0, distance_to_stop_m=5000.0, holding=False
        )
        assert not decision.braking

    def test_換一站要重新取得發車授權(self, train_spec):
        ato = AtoController(spec=train_spec, available=True, engaged=True)
        assert ato.arrive_at("A")
        ato.authorise_departure()
        assert not ato.arrive_at("A")  # 同一站不重設
        assert ato.departure_authorised
        assert ato.arrive_at("B")
        assert not ato.departure_authorised

    def test_沒有_ato_的線啟動不了(self, train_spec):
        ato = AtoController(spec=train_spec, available=False)
        assert not ato.engage()
        assert not ato.engaged

    def test_預設開門側有明確依據(self):
        """月台方向不在來源資料裡，因此固定一側並在文件中說明（§27）。"""
        assert DEFAULT_DOOR_SIDE in {"left", "right"}


class TestManualStillWorks:
    """開了自動駕駛不代表失去手動控制。"""

    def test_解除後可以手動加電門(self, mrt_data):
        session = make_mrt_session(mrt_data, "R1051")
        session.toggle_ato()
        session.toggle_ato()
        session.power_up()
        assert session.train.power_notch == 1

    def test_緊急制軔仍然有效(self, mrt_data):
        session = make_mrt_session(mrt_data, "R1051")
        session.toggle_ato()
        session.ato_depart()
        for _ in range(300):
            session.tick(0.1)
        assert session.train.current_speed_kmh > 0
        session.emergency_brake()
        for _ in range(300):
            session.tick(0.1)
        assert session.train.is_stopped
        assert session.train.emergency_brake

    def test_臺鐵手動駕駛不受影響(self, game_data, announcer):
        session = make_session(game_data, "2115", announcer)
        session.power_up()
        assert session.train.power_notch == 1


def test_假列車不會被誤判為捷運(train_spec):
    """AtoController 預設是關的：沒有明確給 available 就不該能開。"""
    ato = AtoController(spec=train_spec)
    assert not ato.available
    assert not ato.engaged
    train = Train(id="T", train_type=train_spec.id)
    assert ato.decide(
        train, permitted_kmh=50.0, distance_to_stop_m=None, holding=False
    ).reason in {"accelerate", "coast", "trim"}
