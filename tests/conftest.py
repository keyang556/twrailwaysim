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


def block_midpoint(session: DriverSession, block_id: str) -> float:
    """閉塞區間的中點里程。

    區間長度由時刻表推估而來，改點後會變動，因此測試一律由路線推算位置，
    不寫死公尺數。
    """
    block = session.blocks.block(block_id)
    return (block.start_m + block.end_m) / 2.0


def signal_position(session: DriverSession, block_id: str) -> float:
    """防護某個閉塞的號誌所在里程。"""
    signal = session.signals.signal(f"SIG_{block_id}")
    return signal.position_m


def slowest_segment(session: DriverSession) -> tuple[float, float]:
    """路線上速限最低的區間，回傳 ``(中點里程, 速限)``。

    超速測試需要「速限低於車輛最高速度」的區間才有超速的空間；山線的速限
    與 EMU900 的最高速度同為 110，因此必須挑速限較低的區間（例如成追線）。
    """
    segment = min(session.route.segments, key=lambda s: s.max_speed_kmh)
    return (segment.start_m + segment.end_m) / 2.0, segment.max_speed_kmh


#: 情境用車次。均取自臺鐵 115 年 7 月 1 日實施的實際時刻表。
LOCAL_SERVICE = "2115"
"""區間車 2115 次：豐原往彰化，各站停車（含成功站）。"""

EXPRESS_SERVICE = "2021"
"""區間快 2021 次：與 2115 次同路線，通過成功站。"""

TZE_CHIANG_SERVICE = "101"
"""自強號 101 次：臺中往潮州，通過成功站（規格 §25.7）。"""

CHENGZHUI_SERVICE = "2600"
"""區間車 2600 次：臺中往大甲，經成追線，不經彰化（規格 §10.4）。"""


@pytest.fixture
def local_session(game_data: GameData, announcer: Announcer) -> DriverSession:
    """區間車 2115 次：各站停車，含成功站。"""
    return make_session(game_data, LOCAL_SERVICE, announcer)


@pytest.fixture
def express_session(game_data: GameData, announcer: Announcer) -> DriverSession:
    """區間快 2021 次：通過成功站。"""
    return make_session(game_data, EXPRESS_SERVICE, announcer)


@pytest.fixture
def chengzhui_session(game_data: GameData, announcer: Announcer) -> DriverSession:
    """區間車 2600 次：臺中經成追線往大甲，含速限較低的成追線區間。"""
    return make_session(game_data, CHENGZHUI_SERVICE, announcer)
