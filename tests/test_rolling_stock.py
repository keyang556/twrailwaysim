"""區間車與區間快的共通運用（``trains.json`` 的 ``rolling_stock_pools``）。

驗證的是三件使用者明確要求的事：

1. 花東線只有 EMU500，沙崙線與六家線只有 EMU500 與 EMU600。
2. 其餘路線五型混跑，且各路段的比重不同（縱貫線北段與區間快偏 EMU900，
   其他路段偏 EMU800 與 EMU700）。
3. 同一個車次今天與明天可能是不同車型——但**同一天之內一定是同一型**，
   否則選單顯示的和實際開到的會不一樣。
"""

from __future__ import annotations

import collections
import json
import os
import pathlib
import subprocess
import sys
import textwrap
from datetime import date, timedelta

import pytest
from conftest import REFERENCE_DAY, make_session

import railway_sim
from railway_sim import app
from railway_sim.accessibility.announcer import Announcer
from railway_sim.data_loader import GameData
from railway_sim.roles.driver import DriverSession
from railway_sim.timetable.service import Service

#: 五型通勤電聯車。
POOL_TYPES = ("EMU500", "EMU600", "EMU700", "EMU800", "EMU900")

#: 統計權重時取樣的天數。一年足以讓 1% 的車型也出現好幾次。
SAMPLE_DAYS = 365


def days(count: int = SAMPLE_DAYS) -> list[date]:
    return [REFERENCE_DAY + timedelta(days=n) for n in range(count)]


def services_under(data: GameData, rule_id: str) -> list[Service]:
    """適用某一條運用規則、且真的會被改派的班次。"""
    found = []
    for service in data.services.values():
        pools = data.rolling_stock_pools
        if service.rolling_stock_id not in pools.managed_rolling_stock_ids:
            continue
        route = data.routes.get(service.route_id)
        rule = pools.rule_for(
            service.train_type, route.line_ids if route is not None else ()
        )
        if rule is not None and rule.id == rule_id:
            found.append(service)
    return found


def draws(data: GameData, service: Service) -> collections.Counter[str]:
    """一年之中這個車次抽到各車型的次數。"""
    return collections.Counter(
        data.rolling_stock_id_for(service, day=day) for day in days()
    )


class TestPoolData:
    """資料檔本身。"""

    def test_all_five_commuter_types_exist(self, game_data: GameData) -> None:
        missing = [t for t in POOL_TYPES if t not in game_data.train_types]
        assert not missing, f"trains.json 缺少車輛型式：{missing}"

    def test_they_are_all_registered_for_local_services(
        self, game_data: GameData
    ) -> None:
        """五型都要登記成區間車與區間快的擔當車，否則資料自相矛盾。"""
        raw = json.loads(
            (game_data.data_dir / "trains.json").read_text(encoding="utf-8")
        )
        classes = {
            entry["id"]: set(entry.get("service_class_ids", ()))
            for entry in raw["train_types"]
        }
        for type_id in POOL_TYPES:
            assert {"local", "local_express"} <= classes[type_id], type_id

    def test_the_pool_is_exactly_the_five_commuter_types(
        self, game_data: GameData
    ) -> None:
        assert game_data.rolling_stock_pools.managed_rolling_stock_ids == frozenset(
            POOL_TYPES
        )

    def test_every_rule_can_actually_draw_something(self, game_data: GameData) -> None:
        for rule in game_data.rolling_stock_pools.rules:
            assert game_data.rolling_stock_pools.weights(rule), rule.id

    def test_the_data_passes_validation(self, game_data: GameData) -> None:
        """規則指到不存在的車型或車種時，載入就要報出來（不是開車才爆）。"""
        assert not [i for i in game_data.issues if "運用" in i]


class TestRestrictedLines:
    """使用者指定「只有某幾型」的路線。"""

    def test_hualien_taitung_line_is_emu500_only(self, game_data: GameData) -> None:
        """花東線（臺東線）的區間車與區間快只有 EMU500。"""
        services = services_under(game_data, "taitung_local")
        assert services, "找不到任何花東線的區間車"
        for service in services:
            assert set(draws(game_data, service)) == {"EMU500"}

    @pytest.mark.parametrize("rule_id", ["shalun_local", "liujia_local"])
    def test_shalun_and_liujia_use_only_emu500_and_emu600(
        self, game_data: GameData, rule_id: str
    ) -> None:
        """沙崙線與六家線只有 EMU500 與 EMU600。"""
        services = services_under(game_data, rule_id)
        assert services, f"找不到任何適用 {rule_id} 的班次"
        drawn: set[str] = set()
        for service in services:
            drawn |= set(draws(game_data, service))
        assert drawn == {"EMU500", "EMU600"}

    def test_neiwan_diesel_services_are_never_reassigned(
        self, game_data: GameData
    ) -> None:
        """內灣線與六家線共用 line_id，但柴客班次不在運用池裡，不可被改派。

        六家線的規則以線別比對，內灣線（竹中至內灣）未電氣化、車輛型式是
        DR1000，若不小心把它也納入池裡，遊戲會派一列電聯車跑沒有電車線的區間。
        """
        diesel = [
            s
            for s in game_data.services.values()
            if s.rolling_stock_id == "DR1000"
        ]
        assert diesel
        for service in diesel:
            assert set(draws(game_data, service)) == {"DR1000"}

    def test_reserved_seat_services_are_never_reassigned(
        self, game_data: GameData
    ) -> None:
        """對號列車的車輛型式由時刻表決定，不參加共通運用。"""
        for service in game_data.services.values():
            if service.train_type in ("local", "local_express"):
                continue
            assert set(draws(game_data, service)) == {service.rolling_stock_id}


class TestWeights:
    """各路段的比重。次數是統計出來的，不是查表查出來的。"""

    def _tally(self, game_data: GameData, rule_id: str) -> collections.Counter[str]:
        services = services_under(game_data, rule_id)
        assert services, f"找不到任何適用 {rule_id} 的班次"
        tally: collections.Counter[str] = collections.Counter()
        for service in services[:40]:
            tally += draws(game_data, service)
        return tally

    def test_local_express_favours_emu900(self, game_data: GameData) -> None:
        """區間快最常派到 EMU900。"""
        tally = self._tally(game_data, "local_express_any")
        assert tally.most_common(1)[0][0] == "EMU900"

    def test_west_north_locals_favour_emu900(self, game_data: GameData) -> None:
        """經過縱貫線北段的區間車最常派到 EMU900。"""
        tally = self._tally(game_data, "local_west_north")
        assert tally.most_common(1)[0][0] == "EMU900"

    def test_other_locals_favour_emu800_and_emu700(self, game_data: GameData) -> None:
        """其他路段以 EMU800 與 EMU700 為主力，而且 EMU900 不是最多的。"""
        tally = self._tally(game_data, "local_other")
        top_two = {type_id for type_id, _ in tally.most_common(2)}
        assert top_two == {"EMU800", "EMU700"}

    def test_every_type_shows_up_somewhere_over_a_year(
        self, game_data: GameData
    ) -> None:
        """五型在一年之中都要真的出現過，權重設成 0 會被抓出來。"""
        tally: collections.Counter[str] = collections.Counter()
        for rule_id in ("local_express_any", "local_west_north", "local_other"):
            tally += self._tally(game_data, rule_id)
        assert set(tally) == set(POOL_TYPES)


class TestDeterminism:
    """同一天固定、隔天可能不同。"""

    def test_the_same_day_always_gives_the_same_type(
        self, game_data: GameData
    ) -> None:
        for service in list(game_data.services.values())[:50]:
            answers = {
                game_data.rolling_stock_id_for(service, day=REFERENCE_DAY)
                for _ in range(5)
            }
            assert len(answers) == 1

    def test_some_services_change_from_one_day_to_the_next(
        self, game_data: GameData
    ) -> None:
        """「今天這班和明天可能不同車型」——整份時刻表一定看得到變化。"""
        tomorrow = REFERENCE_DAY + timedelta(days=1)
        changed = [
            s.train_number
            for s in game_data.services.values()
            if game_data.rolling_stock_id_for(s, day=REFERENCE_DAY)
            != game_data.rolling_stock_id_for(s, day=tomorrow)
        ]
        assert changed

    def test_it_survives_a_restart(self, game_data: GameData) -> None:
        """換一個行程跑要得到同一個答案。

        Python 的 :func:`hash` 每次啟動都加鹽，用它抽籤會讓同一天重開遊戲就換
        一台車；本測試就是防止有人把 :mod:`hashlib` 換成 ``hash``。
        """
        service = next(
            s
            for s in game_data.services.values()
            if s.rolling_stock_id in game_data.rolling_stock_pools.managed_rolling_stock_ids
        )
        expected = game_data.rolling_stock_id_for(service, day=REFERENCE_DAY)
        script = textwrap.dedent(
            f"""
            from datetime import date
            from railway_sim.data_loader import load_game_data
            data = load_game_data({str(game_data.data_dir)!r})
            service = data.service({service.train_number!r})
            print(data.rolling_stock_id_for(service, day=date(
                {REFERENCE_DAY.year}, {REFERENCE_DAY.month}, {REFERENCE_DAY.day})))
            """
        )
        # 子行程不會繼承 pytest 幫忙加上的 sys.path，因此明說套件在哪裡。
        env = dict(os.environ)
        env["PYTHONPATH"] = str(pathlib.Path(railway_sim.__file__).parents[1])
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
        )
        # 不用 check=True：它拋出的例外看不到子行程的錯誤訊息，真的壞掉時
        # 只會得到一行 CalledProcessError，查不出原因。
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected


class TestSessionUsesTheDrawnType:
    """抽到的車必須真的開得到，而且選單顯示的就是它。"""

    def test_the_session_drives_the_drawn_type(self, game_data: GameData) -> None:
        session = make_session(game_data, "2115")
        assert session.rolling_stock_id == game_data.rolling_stock_id_for(
            game_data.service("2115")
        )
        assert session.spec.id == session.rolling_stock_id
        assert session.train.train_type == session.rolling_stock_id
        assert session.train.length_m == session.spec.length_m

    def test_an_explicit_type_wins(self, game_data: GameData) -> None:
        """指定車輛型式時不再抽籤，重播同一次運轉才有辦法重現。"""
        session = DriverSession(
            data=game_data,
            service=game_data.service("2115"),
            announcer=Announcer(),
            rolling_stock_id="EMU600",
        )
        assert session.spec.id == "EMU600"

    def test_the_menu_shows_what_the_session_will_drive(
        self, game_data: GameData
    ) -> None:
        """車次選單與工作階段必須一致（§25.5）。"""
        session = make_session(game_data, "2115")
        scenario = app.scenario_for_service(game_data, "2115")
        assert session.spec.name_zh_tw in scenario.description

    def test_the_service_list_shows_the_drawn_type(self, game_data: GameData) -> None:
        line = next(
            line
            for line in app.service_menu_lines(game_data)
            if line.split("\t")[0] == "2115"
        )
        assert make_session(game_data, "2115").rolling_stock_id in line
