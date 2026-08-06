"""時刻表匯入工具測試（``railway_sim.dataset``）。

匯入程式的正確性沒辦法只靠「跑得動」來確認，因此這裡用手寫的最小 ODS
檔驗證解析規則本身，再用實際產出的 ``data/`` 檢查匯入結果的完整性。
"""

from __future__ import annotations

import json
import zipfile
from itertools import pairwise
from pathlib import Path

import pytest

from railway_sim.data_loader import GameData, default_data_dir
from railway_sim.dataset.ods import read_ods
from railway_sim.dataset.registry import StationRegistry, UnknownStationError
from railway_sim.dataset.tra_parser import normalise_name, parse_sheet

_CONTENT_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
    xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
    xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
    xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
  <office:body><office:spreadsheet>{tables}</office:spreadsheet></office:body>
</office:document-content>
"""


def _cell(value: str = "", *, repeat: int = 1, covered: bool = False) -> str:
    tag = "table:covered-table-cell" if covered else "table:table-cell"
    attributes = f' table:number-columns-repeated="{repeat}"' if repeat > 1 else ""
    if not value:
        return f"<{tag}{attributes}/>"
    return f"<{tag}{attributes}><text:p>{value}</text:p></{tag}>"


def _row(cells: str, *, repeat: int = 1) -> str:
    attributes = f' table:number-rows-repeated="{repeat}"' if repeat > 1 else ""
    return f"<table:table-row{attributes}>{cells}</table:table-row>"


def _write_ods(path: Path, tables: str) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("content.xml", _CONTENT_TEMPLATE.format(tables=tables))
    return path


class TestOdsReader:
    """只用標準函式庫讀 ODS，必須處理重複與合併儲存格。"""

    def test_reads_cells_and_sheet_name(self, tmp_path: Path) -> None:
        table = (
            '<table:table table:name="測試">'
            + _row(_cell("甲") + _cell("乙"))
            + "</table:table>"
        )
        sheet = read_ods(_write_ods(tmp_path / "a.ods", table))[0]
        assert sheet.name == "測試"
        assert sheet.rows[0] == ("甲", "乙")

    def test_expands_repeated_columns(self, tmp_path: Path) -> None:
        table = (
            '<table:table table:name="s">'
            + _row(_cell("x") + _cell("y", repeat=3) + _cell("z"))
            + "</table:table>"
        )
        sheet = read_ods(_write_ods(tmp_path / "b.ods", table))[0]
        assert sheet.rows[0] == ("x", "y", "y", "y", "z")

    def test_covered_cells_keep_columns_aligned(self, tmp_path: Path) -> None:
        """縱向合併留下的 covered-table-cell 必須占一格。

        時刻表表頭大量使用縱向合併；不計入的話整列會往左位移，站名與時刻
        就會對到錯誤的車站。
        """
        table = (
            '<table:table table:name="s">'
            + _row(_cell("車次") + _cell("站名"))
            + _row(_cell(covered=True) + _cell("二水"))
            + "</table:table>"
        )
        sheet = read_ods(_write_ods(tmp_path / "c.ods", table))[0]
        assert sheet.rows[1] == ("", "二水")

    def test_huge_repeat_is_treated_as_trailing_padding(self, tmp_path: Path) -> None:
        """ODS 以超大 repeat 表示「這列剩下都是空的」，不可照著展開。"""
        table = (
            '<table:table table:name="s">'
            + _row(_cell("x") + _cell(repeat=60000))
            + "</table:table>"
        )
        sheet = read_ods(_write_ods(tmp_path / "d.ods", table))[0]
        assert sheet.rows[0] == ("x",)


class TestNameNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("臺 北", "臺北"),
            ("蘇澳新", "蘇澳新"),
            ("新 城(太魯閣) Xincheng", "新城"),
            ("︵\n新太\n　魯\n城閣\n　 ︶", "新城"),
            ("高雄", "高雄"),
            ("", ""),
        ],
    )
    def test_normalise_name(self, raw: str, expected: str) -> None:
        assert normalise_name(raw) == expected


class TestRowLayoutParsing:
    """排版 A：車站是欄、班次是列，站名縱書拆在兩列。"""

    def _sheet(self, tmp_path: Path):
        table = (
            '<table:table table:name="集集線">'
            # 表頭：車種、車次、站名間，之後是縱書站名的第一個字
            + _row(
                _cell("車")
                + _cell("次")
                + _cell("站\n　名\n間")
                + _cell("二")
                + _cell("源")
                + _cell("車")
            )
            # 縱書站名的第二個字
            + _row(
                _cell(covered=True)
                + _cell(covered=True)
                + _cell(covered=True)
                + _cell("水")
                + _cell("泉")
                + _cell("埕")
            )
            # 資料列：一列一班車
            + _row(
                _cell("區間車")
                + _cell("2705")
                + _cell("二水－車埕")
                + _cell("08:00")
                + _cell("-")
                + _cell("08:49")
            )
            + "</table:table>"
        )
        return read_ods(_write_ods(tmp_path / "row.ods", table))[0]

    def test_station_names_are_joined_vertically(self, tmp_path: Path) -> None:
        block = parse_sheet(self._sheet(tmp_path))[0]
        assert block.layout == "row_per_train"
        assert block.station_names == ("二水", "源泉", "車埕")

    def test_times_and_passes_are_read(self, tmp_path: Path) -> None:
        service = parse_sheet(self._sheet(tmp_path))[0].services[0]
        assert service.train_number == "2705"
        assert service.train_class == "區間車"
        assert [s.station_name for s in service.calling_stops] == ["二水", "車埕"]
        assert [s.station_name for s in service.passes] == ["源泉"]
        assert service.calling_stops[0].time_text == "08:00"

    def test_endpoints_come_from_the_range_column(self, tmp_path: Path) -> None:
        service = parse_sheet(self._sheet(tmp_path))[0].services[0]
        assert service.origin_name == "二水"
        assert service.destination_name == "車埕"


class TestColumnLayoutParsing:
    """排版 B：班次是欄、車站是列，山線與海線站名並排。"""

    def _sheet(self, tmp_path: Path):
        table = (
            '<table:table table:name="西部幹線">'
            + _row(_cell(repeat=4) + _cell("T.C.") + _cell("T.C.") + _cell("T.C."))
            + _row(_cell(repeat=4) + _cell("自強") + _cell("自強") + _cell("自強"))
            + _row(_cell(repeat=4) + _cell("101") + _cell("103") + _cell("105"))
            + _row(_cell(repeat=4) + _cell("山") + _cell("海") + _cell(""))
            # 單一站名列
            + _row(_cell("竹 南") + _cell(repeat=3) + _cell("08:38") + _cell("07:54") + _cell("-"))
            # 山線／海線並排列：左欄海線、右欄山線
            + _row(
                _cell("後 龍")
                + _cell(repeat=3)
                + _cell("08:49")
                + _cell("08:07")
                + _cell("")
            )
            + _row(_cell("彰 化") + _cell(repeat=3) + _cell("09:43") + _cell("09:17") + _cell("-"))
            + "</table:table>"
        )
        return read_ods(_write_ods(tmp_path / "col.ods", table))[0]

    def test_services_are_read_per_column(self, tmp_path: Path) -> None:
        block = parse_sheet(self._sheet(tmp_path))[0]
        assert block.layout == "column_per_train"
        assert [s.train_number for s in block.services] == ["101", "103", "105"]

    def test_via_marker_is_captured(self, tmp_path: Path) -> None:
        block = parse_sheet(self._sheet(tmp_path))[0]
        by_number = {s.train_number: s for s in block.services}
        assert by_number["101"].via_note == "山"
        assert by_number["103"].via_note == "海"

    def test_latin_only_rows_are_not_stations(self, tmp_path: Path) -> None:
        """站名下方自成一列的羅馬拼音不是另一個車站。"""
        table = (
            '<table:table table:name="s">'
            + _row(_cell(repeat=4) + _cell("T.C.") + _cell("T.C.") + _cell("T.C."))
            + _row(_cell(repeat=4) + _cell("自強") + _cell("自強") + _cell("自強"))
            + _row(_cell(repeat=4) + _cell("101") + _cell("103") + _cell("105"))
            + _row(_cell(repeat=4) + _cell("山") + _cell("海") + _cell(""))
            + _row(_cell("高雄") + _cell(repeat=3) + _cell("08:26") + _cell("") + _cell(""))
            + _row(_cell("Kaohsiung") + _cell(repeat=3) + _cell("08:29") + _cell("") + _cell(""))
            + "</table:table>"
        )
        sheet = read_ods(_write_ods(tmp_path / "latin.ods", table))[0]
        block = parse_sheet(sheet)[0]
        assert "Kaohsiung" not in block.station_names


class TestParallelLineColumns:
    """竹南至彰化之間，對號時刻表把海線與山線站名並排成左右兩欄。

    兩欄共用同一個時刻欄，實際是哪一站由該班次的「山」／「海」標記決定。
    不處理的話，山線列車的時刻會被記到海線車站上。
    """

    def _sheet(self, tmp_path: Path):
        table = (
            '<table:table table:name="西部幹線">'
            + _row(_cell(repeat=4) + _cell("T.C.") + _cell("T.C.") + _cell("T.C."))
            + _row(_cell(repeat=4) + _cell("自強") + _cell("自強") + _cell("莒光"))
            + _row(_cell(repeat=4) + _cell("105") + _cell("103") + _cell("501"))
            + _row(_cell(repeat=4) + _cell("山") + _cell("海") + _cell("山"))
            + _row(
                _cell("竹 南")
                + _cell(repeat=3)
                + _cell("08:38")
                + _cell("07:54")
                + _cell("10:10")
            )
            # 左欄海線「後龍」、右欄山線「苗栗」，共用同一個時刻欄
            + _row(
                _cell("後 龍")
                + _cell()
                + _cell("苗 栗")
                + _cell()
                + _cell("08:49")
                + _cell("08:07")
                + _cell("10:25")
            )
            + _row(
                _cell("彰 化")
                + _cell(repeat=3)
                + _cell("09:43")
                + _cell("09:17")
                + _cell("11:20")
            )
            + "</table:table>"
        )
        return read_ods(_write_ods(tmp_path / "parallel.ods", table))[0]

    def test_both_station_names_are_offered_as_candidates(
        self, tmp_path: Path
    ) -> None:
        block = parse_sheet(self._sheet(tmp_path))[0]
        assert "後龍" in block.station_names
        assert "苗栗" in block.station_names

        service = next(s for s in block.services if s.train_number == "105")
        shared = next(s for s in service.stops if s.station_name == "後龍")
        assert shared.alternatives == ("苗栗",)

    def test_each_corridor_is_a_separate_sequence(self, tmp_path: Path) -> None:
        """站序必須分成兩條，否則相鄰關係會交錯錯亂。"""
        block = parse_sheet(self._sheet(tmp_path))[0]
        assert block.sequences == (
            ("竹南", "後龍", "彰化"),
            ("竹南", "苗栗", "彰化"),
        )

    def test_via_marker_selects_the_right_column(self, tmp_path: Path) -> None:
        from railway_sim.dataset.build import _resolve_parallel_names

        block = parse_sheet(self._sheet(tmp_path))[0]
        station_lines = {
            "竹南": {"west_north"},
            "後龍": {"coast"},
            "苗栗": {"mountain"},
            "彰化": {"mountain", "coast"},
        }
        assert _resolve_parallel_names([block], station_lines) == 0

        by_number = {s.train_number: s for s in block.services}
        mountain = [s.station_name for s in by_number["105"].stops]
        coast = [s.station_name for s in by_number["103"].stops]
        assert "苗栗" in mountain and "後龍" not in mountain
        assert "後龍" in coast and "苗栗" not in coast


class TestStationRegistry:
    def test_duplicate_ids_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="車站代碼重複"):
            StationRegistry.from_dict(
                {
                    "stations": [
                        {"name_zh_tw": "甲", "id": "X"},
                        {"name_zh_tw": "乙", "id": "X"},
                    ]
                }
            )

    def test_unknown_station_names_are_reported(self) -> None:
        registry = StationRegistry.from_dict(
            {"stations": [{"name_zh_tw": "臺中", "id": "TAICHUNG"}]}
        )
        with pytest.raises(UnknownStationError, match="station_registry"):
            registry.entry_for("不存在站")

    def test_only_source_confirmed_english_is_official(self) -> None:
        registry = StationRegistry.from_dict(
            {
                "stations": [
                    {
                        "name_zh_tw": "基隆",
                        "id": "KEELUNG",
                        "name_en": "Keelung",
                        "name_en_status": "official",
                    },
                    {
                        "name_zh_tw": "三坑",
                        "id": "SANKENG",
                        "name_en": "Sankeng",
                        "name_en_status": "derived",
                    },
                ]
            }
        )
        assert registry.entry_for("基隆").official_name_en == "Keelung"
        assert registry.entry_for("三坑").official_name_en is None


class TestShippedRegistry:
    """專案內的對照表本身必須完整可用。"""

    def test_registry_loads(self) -> None:
        registry = StationRegistry.load(default_data_dir() / "station_registry.json")
        assert len(registry.entries) > 200

    def test_every_shipped_station_is_in_the_registry(
        self, game_data: GameData
    ) -> None:
        registry = StationRegistry.load(default_data_dir() / "station_registry.json")
        for station in game_data.stations.values():
            assert registry.contains(station.name_zh_tw), station.name_zh_tw


class TestImportedDataProvenance:
    """匯入結果必須帶著來源資訊，才知道現在用的是哪一次改點的資料。"""

    def _meta(self, name: str) -> dict:
        path = default_data_dir() / name
        return json.loads(path.read_text(encoding="utf-8"))["meta"]

    @pytest.mark.parametrize(
        "name", ["stations.json", "routes.json", "timetables.json"]
    )
    def test_provenance_is_recorded(self, name: str) -> None:
        provenance = self._meta(name)["provenance"]
        assert provenance["effective_date"]
        assert provenance["sources"]
        for source in provenance["sources"]:
            assert source["file"].endswith(".ods")
            assert len(source["sha256"]) == 64

    def test_effective_date_matches_across_files(self) -> None:
        dates = {
            self._meta(name)["provenance"]["effective_date"]
            for name in ("stations.json", "routes.json", "timetables.json")
        }
        assert len(dates) == 1

    def test_estimated_lengths_are_marked_as_such(self) -> None:
        """區間長度是由排點時間推估的，不得標成官方資料（規格 §27）。"""
        routes = json.loads(
            (default_data_dir() / "routes.json").read_text(encoding="utf-8")
        )
        assert routes["links"]
        for link in routes["links"]:
            assert link["verification_status"] == "estimated_from_timetable"


class TestImportedDataShape:
    """匯入結果的基本完整性。"""

    def test_every_service_has_a_route(self, game_data: GameData) -> None:
        for service in game_data.services.values():
            assert service.route_id in game_data.routes

    def test_every_stop_is_on_its_route(self, game_data: GameData) -> None:
        for service in game_data.services.values():
            route = game_data.routes[service.route_id]
            for station_id in service.stop_station_ids:
                assert station_id in route.station_ids, service.train_number

    def test_stop_times_are_in_order(self, game_data: GameData) -> None:
        """停靠時刻必須沿路線遞增（跨日班次允許一次回捲）。"""
        for service in game_data.services.values():
            route = game_data.routes[service.route_id]
            times = [
                (route.stop_for_station(sid).position_m, service.departure_times[sid])
                for sid in service.stop_station_ids
                if sid in service.departure_times
                and route.stop_for_station(sid) is not None
            ]
            times.sort()
            minutes = [int(t[:2]) * 60 + int(t[3:]) for _, t in times]
            wraps = sum(1 for a, b in pairwise(minutes) if b < a)
            assert wraps <= 1, f"{service.train_number} 的時刻順序不合理"

    def test_services_cover_every_train_class(self, game_data: GameData) -> None:
        classes = {s.train_type for s in game_data.services.values()}
        assert {"local", "local_express", "tze_chiang", "chu_kuang"} <= classes

    def test_network_is_connected_enough_for_every_route(
        self, game_data: GameData
    ) -> None:
        for route in game_data.routes.values():
            ok, reason = game_data.network.is_traversable(list(route.node_ids))
            assert ok, f"{route.id}：{reason}"
