"""捷運資料集與維基百科匯入的驗證。

這一份守的是「資料本身對不對」：車站順序、代碼、站距，以及機場捷運的
停靠規則。運轉與廣播行為在 ``test_mrt_broadcast.py`` 與 ``test_ato.py``。
"""

from __future__ import annotations

import pytest

from railway_sim.data_loader import load_game_data
from railway_sim.dataset.mrt_wiki import parse_station_table
from railway_sim.systems import SYSTEMS, system_data_dir


class TestSystems:
    """兩套資料共用同一組模型（:mod:`railway_sim.systems`）。"""

    def test_臺鐵資料在_data_目錄底下(self, tmp_path):
        assert system_data_dir(tmp_path, "tra") == tmp_path

    def test_捷運資料在_data_mrt_底下(self, tmp_path):
        assert system_data_dir(tmp_path, "mrt") == tmp_path / "mrt"

    def test_未知系統會擲出例外(self, tmp_path):
        with pytest.raises(KeyError):
            system_data_dir(tmp_path, "hsr")

    def test_只有捷運有_ato(self):
        assert not SYSTEMS["tra"].supports_ato
        assert SYSTEMS["mrt"].supports_ato

    def test_鍵位表兩邊共用(self, game_data, mrt_data):
        """捷運目錄沒有自己的 keymap.json，會退回共用的那一份。"""
        assert mrt_data.keymap_raw == game_data.keymap_raw


class TestMrtDataset:
    def test_資料驗證通過(self, mrt_data):
        assert mrt_data.issues == []
        assert mrt_data.system.id == "mrt"

    def test_十條線都載入(self, mrt_data):
        assert set(mrt_data.lines) == {
            "wenhu",
            "tamsui_xinyi",
            "xinbeitou",
            "songshan_xindian",
            "xiaobitan",
            "zhonghe_xinlu",
            "bannan",
            "circular",
            "sanying",
            "taoyuan_airport",
        }

    def test_車站代碼即維基百科編號(self, mrt_data):
        assert mrt_data.stations["R22"].name_zh_tw == "北投"
        assert mrt_data.stations["R22A"].name_zh_tw == "新北投"
        assert mrt_data.stations["A14a"].name_zh_tw == "機場旅館"
        assert mrt_data.stations["LB12"].name_zh_tw == "鶯桃福德"
        assert mrt_data.stations["Y07"].name_zh_tw == "大坪林"

    def test_未通車的車站不列入(self, mrt_data):
        """條目列出但尚未通車的車站不該出現在可駕駛的資料裡。"""
        for code in ("R01", "A14", "A23", "LB14", "Y01", "Y21"):
            assert code not in mrt_data.stations

    def test_站距取自條目累計里程(self, mrt_data):
        """淡水信義線象山至淡水＝30.38−1.42 公里。"""
        route = mrt_data.routes["R_R1001"]
        assert route.length_m == pytest.approx(28960.0)

    def test_未通車車站不算進站距(self, mrt_data):
        """A13 與 A14a 之間夾著還沒通車的 A14，長度應為累計相減的 1.336 公里。"""
        link = mrt_data.network.link("STA_A13", "STA_A14a")
        assert link is not None
        assert link.length_m == pytest.approx(1336.0)

    def test_分歧站標記為運轉節點(self, mrt_data):
        for code in ("R22", "G03", "O12"):
            assert mrt_data.stations[code].is_operational_node
        assert not mrt_data.stations["R23"].is_operational_node

    def test_中和新蘆線兩個分支都連得上(self, mrt_data):
        """蘆洲往南勢角要經大橋頭轉入主線，長度為兩段相加。"""
        route = mrt_data.routes["R_O1003"]
        assert route.station_ids[0] == "O54"
        assert route.station_ids[-1] == "O01"
        assert "O12" in route.station_ids
        assert route.length_m == pytest.approx(6010.0 + 12400.0)

    def test_每條線的速限取自條目(self, mrt_data):
        assert mrt_data.network.link("STA_R22", "STA_R23").max_speed_kmh == 80.0
        assert mrt_data.network.link("STA_BR01", "STA_BR02").max_speed_kmh == 70.0
        assert mrt_data.network.link("STA_A1", "STA_A2").max_speed_kmh == 100.0
        # 新北投支線是全網最慢的一段（條目：最高速度 25km/h）。
        assert mrt_data.network.link("STA_R22", "STA_R22A").max_speed_kmh == 25.0


class TestAirportStopPattern:
    """機場捷運是唯一有兩種車種的線，停靠規則才有意義。"""

    def test_直達車只停五站(self, mrt_data):
        service = mrt_data.services["A1003"]
        assert service.train_type == "express"
        assert list(service.stop_station_ids) == ["A1", "A3", "A8", "A12", "A13"]

    def test_環北直達車多停高鐵桃園與環北(self, mrt_data):
        service = mrt_data.services["A1005"]
        assert list(service.stop_station_ids) == [
            "A1", "A3", "A8", "A12", "A13", "A18", "A21",
        ]

    def test_普通車各站皆停(self, mrt_data):
        service = mrt_data.services["A1001"]
        assert service.train_type == "commuter"
        assert len(service.stop_station_ids) == 22
        assert service.pass_station_ids == ()

    def test_直達車不停的站不辦理直達車停靠(self, mrt_data):
        """停靠規則由營運模式反推，因此泰山不接受直達車。"""
        assert not mrt_data.stations["A5"].allows_train_type("express")
        assert mrt_data.stations["A5"].allows_train_type("commuter")
        assert mrt_data.stations["A8"].allows_train_type("express")

    def test_台北捷運各線都是各站停車(self, mrt_data):
        for number in ("R1001", "BL1001", "BR1001", "LB1001", "Y1001"):
            assert mrt_data.services[number].pass_station_ids == ()


class TestWikiParser:
    """車站表的剖析。合併儲存格是這裡唯一真正難的地方。"""

    def _table(self, rows_html: str) -> str:
        return (
            "<table><tr><th>編號</th><th>名稱</th><th>距離</th></tr>"
            "<tr><th>編號</th><th>中文</th><th>英文</th>"
            "<th>與前一站站距</th><th>累計</th></tr>"
            f"{rows_html}</table>"
        )

    def test_讀出代碼站名與里程(self):
        table = self._table(
            '<tr><td><img alt="BL01"></td><td><a href="#">頂埔</a></td>'
            '<td><span lang="en">Dingpu</span></td><td>不適用</td>'
            "<td>0.00</td></tr>"
        )
        rows = parse_station_table(table).rows
        assert len(rows) == 1
        assert rows[0].code == "BL01"
        assert rows[0].name_zh_tw == "頂埔"
        assert rows[0].name_en == "Dingpu"
        assert rows[0].cumulative_km == 0.0

    def test_文字寫的車站代碼也認得(self):
        """三鶯線與機場捷運的編號欄是文字，不是圖示。"""
        table = self._table(
            "<tr><td><b>LB02</b></td><td><a href='#'>媽祖田</a></td>"
            '<td><span lang="en">Mazutian</span></td><td>1.005</td>'
            "<td>1.005</td></tr>"
        )
        rows = parse_station_table(table).rows
        assert rows[0].code == "LB02"

    def test_合併儲存格的里程會落在正確的車站(self):
        """三鶯線把 LB07a 與 LB08 的里程用 rowspan 併成一格。

        照 ``<td>`` 出現順序讀，鶯歌車站會整站沒有里程；展開成矩陣之後
        兩列都拿得到同一組里程，正是條目要表達的意思。
        """
        table = self._table(
            "<tr><td><i>LB07a</i></td><td colspan='2'>未釋出</td>"
            "<td rowspan='2'>2.980</td><td rowspan='2'>9.858</td></tr>"
            "<tr><td><b>LB08</b></td><td><a href='#'>鶯歌車站</a></td>"
            '<td><span lang="en">Yingge Station</span></td></tr>'
        )
        rows = parse_station_table(table).rows
        assert len(rows) == 1
        assert rows[0].code == "LB08"
        assert rows[0].cumulative_km == pytest.approx(9.858)

    def test_沒有代碼的車站會被略過並記錄(self):
        table = self._table(
            "<tr><td></td><td><a href='#'>廣慈/奉天宮</a></td>"
            '<td><span lang="en">Guangci</span></td><td>不適用</td>'
            "<td>0.00</td></tr>"
        )
        result = parse_station_table(table)
        assert result.rows == []
        assert any("沒有車站代碼" in w for w in result.warnings)

    def test_找不到車站表不會拋出例外(self):
        result = parse_station_table("<html><body>沒有表</body></html>")
        assert result.rows == []
        assert result.warnings


class TestSelection:
    """開場的系統與車次選擇（兩個介面共用同一份選項）。"""

    def test_系統選項有臺鐵與捷運(self):
        from railway_sim.app import system_choices

        keys = [choice.key for choice in system_choices()]
        assert keys == ["tra", "mrt"]

    def test_捷運不會列出臺鐵情境(self, mrt_data):
        from railway_sim.app import start_choices

        assert not [c for c in start_choices(mrt_data) if c.key.startswith("scenario:")]

    def test_臺鐵仍然列出情境(self, game_data):
        from railway_sim.app import start_choices

        assert [c for c in start_choices(game_data) if c.key.startswith("scenario:")]

    def test_捷運選項顯示營運模式名稱(self, mrt_data):
        from railway_sim.app import start_choices

        labels = [c.label for c in start_choices(mrt_data)]
        assert any("淡水信義線" in label and "往淡水" in label for label in labels)

    def test_可以用線名搜尋(self, mrt_data):
        from railway_sim.app import service_menu_lines

        assert len(service_menu_lines(mrt_data, "三鶯線")) == 2
        # 機場捷運的五種營運模式，其中兩種是單向的，因此是八個而不是十個。
        assert len(service_menu_lines(mrt_data, "機場捷運")) == 8

    def test_指定捷運情境會被擋下(self, capsys):
        """--scenario 是臺鐵專用；用在捷運上要說清楚，而不是丟出例外。"""
        from railway_sim.app import main

        assert main(["--system", "mrt", "--scenario", "local"]) == 2
        assert "--scenario 只有臺鐵有" in capsys.readouterr().err


class TestSystemIsolation:
    """兩套資料互不干擾。"""

    def test_臺鐵資料不含捷運車站(self, game_data):
        assert "R22" not in game_data.stations
        assert game_data.system.id == "tra"

    def test_捷運資料不含臺鐵車站(self, mrt_data):
        assert "TAIPEI" not in mrt_data.stations

    def test_捷運沒有臺鐵的路線規則違反(self):
        """成追線那一組規則只認臺鐵的車站代碼，因此不會誤傷捷運。"""
        data = load_game_data(system="mrt")
        assert data.issues == []
