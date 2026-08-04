"""測試共用夾具。"""

from __future__ import annotations

import pytest

from railway_sim.accessibility.announcer import Announcer
from railway_sim.data_loader import GameData, load_game_data
from railway_sim.roles.driver import DriverSession
from railway_sim.simulation.train import Train, TrainType


class ManualClock:
    """可手動推進的時鐘，讓播報去重邏輯在測試中完全決定性。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(scope="session")
def game_data() -> GameData:
    """載入 data 目錄的正式資料。"""
    return load_game_data()


@pytest.fixture
def announcer() -> Announcer:
    """測試用播報器：使用手動時鐘，並保留 sink 為 None（只記錄歷史）。"""
    return Announcer(clock=ManualClock(), dedupe_seconds=0.0)


@pytest.fixture
def train_spec() -> TrainType:
    """測試用車種：整數化的性能值，方便手算驗證。"""
    return TrainType(
        id="TEST",
        name_zh_tw="測試車種",
        max_speed_kmh=100.0,
        length_m=100.0,
        max_traction_ms2=1.0,
        max_service_brake_ms2=1.0,
        emergency_brake_ms2=2.0,
        power_notches=5,
        brake_notches=7,
        power_corner_speed_kmh=1000.0,  # 測試中不啟用定功率遞減
        resistance_ms2=(0.0, 0.0, 0.0),  # 測試中不計阻力
    )


@pytest.fixture
def train() -> Train:
    return Train(id="T_TEST", train_type="TEST", length_m=100.0)


def make_session(
    game_data: GameData,
    train_number: str,
    announcer: Announcer | None = None,
) -> DriverSession:
    """建立一個司機員工作階段。"""
    return DriverSession(
        data=game_data,
        service=game_data.service(train_number),
        announcer=announcer or Announcer(clock=ManualClock(), dedupe_seconds=0.0),
    )


def run_seconds(session: DriverSession, seconds: float) -> None:
    """以固定步長推進指定秒數。"""
    ticks = round(seconds / session.clock.tick_s)
    for _ in range(ticks):
        session.tick(session.clock.tick_s)


def drive_to(
    session: DriverSession,
    target_m: float,
    *,
    respect_stops: bool = False,
    max_seconds: float = 2400.0,
) -> None:
    """把列車開到指定里程。

    這是測試用的簡易自動駕駛，不是遊戲功能：每個步長比較目前速度與 ATP
    給的目標速度，超過就加制軔、低於就加電門。

    Args:
        respect_stops: ``True`` 時使用 ATP 的建議速度（含營業停車點），列車
            會在停車站停下；``False`` 時只遵守區間速限與號誌，用於製造
            應停未停的情境。
    """
    tick = session.clock.tick_s
    spec = session.spec
    train = session.train
    elapsed = 0.0

    while train.position_m < target_m and elapsed < max_seconds:
        state = session.last_state or session._evaluate_only()
        limit = state.advisory_kmh if respect_stops else state.permitted_kmh

        if not train.emergency_brake:
            if train.current_speed_kmh > limit - 2.0:
                train.power_notch = 0
                train.brake_notch = min(spec.brake_notches, train.brake_notch + 1)
            elif train.current_speed_kmh < limit - 8.0 and limit > 15.0:
                # 目標速度已低於 15 公里時不再加電門，讓列車確實停下來而
                # 不是在停車點附近反覆加減速。
                train.brake_notch = 0
                train.power_notch = min(spec.power_notches, train.power_notch + 1)

        session.tick(tick)
        elapsed += tick


@pytest.fixture
def local_session(game_data: GameData, announcer: Announcer) -> DriverSession:
    """區間車 2701 次：各站停車，含成功站。"""
    return make_session(game_data, "2701", announcer)


@pytest.fixture
def express_session(game_data: GameData, announcer: Announcer) -> DriverSession:
    """自強號 121 次：通過成功站。"""
    return make_session(game_data, "121", announcer)
