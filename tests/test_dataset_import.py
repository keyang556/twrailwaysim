"""時刻表匯入工具測試（``railway_sim.dataset``）。

匯入程式的正確性沒辦法只靠「跑得動」來確認，因此這裡用手寫的最小 ODS
檔驗證解析規則本身，再用實際產出的 ``data/`` 檢查匯入結果的完整性。
"""

from __future__ import annotations

import json
import shutil
import zipfile
from itertools import pairwise
from pathlib import Path

import pytest

from railway_sim.data_loader import (
    IMPORT_STAGING_PREFIX,
    GameData,
    default_data_dir,
    heal_interrupted_import,
    load_game_data,
)
from railway_sim.dataset.build import BuildResult, rolling_stock_for, write_dataset
from railway_sim.dataset.ods import OdsReadError, read_ods
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


class TestCorruptOdsFiles:
    """毀損的來源檔案必須報出帶檔名的錯誤，不能讓底層例外原樣往外跑。

    臺鐵發布的檔案偶爾會因下載中斷或編輯器另存而毀損；使用者需要知道
    「哪一個檔案」壞了，才能重新取得，而不是看到一段 zipfile／xml.etree
    的原始 traceback。
    """

    def test_not_a_zip_file(self, tmp_path: Path) -> None:
        path = tmp_path / "corrupt.ods"
        path.write_bytes(b"not a zip file at all")
        with pytest.raises(OdsReadError, match="corrupt.ods"):
            read_ods(path)

    def test_missing_content_xml(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.ods"
        with zipfile.ZipFile(path, "w"):
            pass
        with pytest.raises(OdsReadError, match="empty.ods"):
            read_ods(path)

    def test_invalid_xml_in_content(self, tmp_path: Path) -> None:
        path = tmp_path / "badxml.ods"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("content.xml", "<not-closed>")
        with pytest.raises(OdsReadError, match="badxml.ods"):
            read_ods(path)

    def test_ods_read_error_is_a_value_error(self, tmp_path: Path) -> None:
        """CLI 依此把讀取失敗歸類為可預期的匯入失敗，而不是未知例外。"""
        path = tmp_path / "corrupt.ods"
        path.write_bytes(b"garbage")
        with pytest.raises(ValueError):
            read_ods(path)


class TestOdsResourceBounds:
    """``content.xml`` 沒有大小或深度上限，一個刻意或不慎做出的病態檔案
    就能把匯入行程的記憶體或 CPU 榨乾（zip bomb／XML 巢狀炸彈）。

    真正的臺鐵時刻表 content.xml 最大約 475 KB、巢狀深度個位數，這裡的
    上限給了數十倍餘裕，同時要能明確擋下病態情形。
    """

    def test_oversized_content_xml_is_rejected(self, tmp_path: Path) -> None:
        """解壓後大小超過上限時拒絕讀取。"""
        path = tmp_path / "oversized.ods"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("content.xml", b"<a>" + b" " * (9 * 1024 * 1024) + b"</a>")
        with pytest.raises(OdsReadError, match="oversized.ods"):
            read_ods(path)

    def test_high_compression_ratio_zip_bomb_is_rejected(self, tmp_path: Path) -> None:
        """重現實際回報的情境：16,459 位元組壓縮，解壓後 16,777,237 位元組。

        ``zipfile`` 寫入的成員一律會如實記錄解壓後大小，因此上面那個測試
        與這裡都會先在 :func:`~railway_sim.dataset.ods._read_bounded_member`
        的宣告值快速檢查被擋下；真正無法繞過的防線是邊解壓邊累計實際
        位元組數的串流迴圈——宣告值理論上可以被惡意偽造，但邊讀邊算的
        實際位元組數不行。這裡用高壓縮比（大量重複空白）重現實際回報的
        情境，確認整條防線（含快速檢查與串流檢查）行為一致。
        """
        path = tmp_path / "bomb.ods"
        with zipfile.ZipFile(
            path, "w", zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            archive.writestr(
                "content.xml", b"<a>" + b" " * (16 * 1024 * 1024) + b"</a>"
            )
        # 壓縮比極高，來源檔案本身仍然很小。
        assert path.stat().st_size < 20_000
        with pytest.raises(OdsReadError, match="bomb.ods"):
            read_ods(path)

    def test_deeply_nested_xml_is_rejected(self, tmp_path: Path) -> None:
        """重現實際回報的情境：20,000 層巢狀的 XML 仍然解析成功。"""
        path = tmp_path / "deepnest.ods"
        deep = b"<a>" * 20_000 + b"</a>" * 20_000
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("content.xml", deep)
        with pytest.raises(OdsReadError, match="deepnest.ods"):
            read_ods(path)

    def test_deep_nesting_aborts_quickly_without_building_the_whole_tree(
        self, tmp_path: Path
    ) -> None:
        """深度超標時應該邊解析邊中止，不是先把病態巢狀整棵樹建完才發現。

        巢狀層數（一萬層）遠超過深度上限，但總位元組數刻意留在大小上限
        之內，確保觸發的是深度檢查本身，而不是先被大小上限擋下。如果
        實作退化成先呼叫 ``ET.fromstring`` 把整棵樹建完才事後檢查深度，
        這個測試會因為時間拉長而逾時；只有真正邊解析邊中止才能瞬間通過。
        """
        path = tmp_path / "extreme_deepnest.ods"
        deep = b"<a>" * 10_000 + b"</a>" * 10_000
        assert len(deep) < 8 * 1024 * 1024, "payload 必須留在大小上限之內"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("content.xml", deep)
        with pytest.raises(OdsReadError, match="巢狀深度超過上限"):
            read_ods(path)


_TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"


def _repeated_content_xml(
    row_elements: int, row_repeat: int, cell_repeat: int
) -> bytes:
    """組出 ``row_elements`` 個列元素，各自宣告列與儲存格的 repeat 屬性。

    每個 repeat 值本身都在 ``_MAX_REPEAT``（512）之內，因此 :func:`_read_row`
    與單一列的檢查都不會單獨擋下任何一個屬性；只有跨整份文件累計才會
    暴露出乘出來的展開量。
    """
    row_xml = (
        f'<table:table-row xmlns:table="{_TABLE_NS}" '
        f'table:number-rows-repeated="{row_repeat}">'
        f'<table:table-cell xmlns:table="{_TABLE_NS}" '
        f'table:number-columns-repeated="{cell_repeat}"/>'
        "</table:table-row>"
    )
    return (
        "<office:document-content "
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        f'xmlns:table="{_TABLE_NS}">'
        "<office:body><office:spreadsheet>"
        '<table:table table:name="s">' + row_xml * row_elements + "</table:table>"
        "</office:spreadsheet></office:body></office:document-content>"
    ).encode("utf-8")


class TestCumulativeExpansionBudget:
    """單一列或單一儲存格的 repeat 值各自合法，疊加起來仍可能展開成
    天文數字的輸出——``_MAX_REPEAT`` 只擋得住「單一宣告」，擋不住
    「大量各自合法的宣告疊加」，因此需要跨整份文件累計的預算。

    重現實際回報的情境：326 KB、巢狀深度 5 的檔案能展開成
    1,048,576 列、512 欄（536,870,912 個可存取儲存格）。
    """

    def test_many_row_level_repeats_are_rejected(self, tmp_path: Path) -> None:
        """許多各自 512 的列重複疊加，遠超過累計列數上限。"""
        path = tmp_path / "row_expansion.ods"
        # 2048 個列元素 × row_repeat=512 = 1,048,576 列，與實際回報的情境
        # 規模一致；cell_repeat 同樣維持在單一上限之內。
        content = _repeated_content_xml(
            row_elements=2048, row_repeat=512, cell_repeat=512
        )
        assert len(content) < 8 * 1024 * 1024, "payload 必須留在大小上限之內"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("content.xml", content)
        with pytest.raises(OdsReadError, match="列數超過上限"):
            read_ods(path)

    def test_expansion_is_rejected_before_building_the_oversized_list(
        self, tmp_path: Path
    ) -> None:
        """必須在超限當下就中止，不能先把展開後的巨大清單建出來。

        如果實作退化成「展開完才檢查總數」，這裡會因為要配置數億個
        字串參照而耗費大量時間與記憶體；只有邊展開邊檢查才能瞬間中止。
        """
        path = tmp_path / "row_expansion_large.ods"
        content = _repeated_content_xml(
            row_elements=2048, row_repeat=512, cell_repeat=512
        )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("content.xml", content)
        with pytest.raises(OdsReadError):
            read_ods(path)

    def test_many_distinct_cells_within_a_single_row_are_rejected(
        self, tmp_path: Path
    ) -> None:
        """單一列裡塞進大量各自獨立宣告 repeat 的儲存格，不靠列層級重複
        也能展開成天文數字，必須在 ``_read_row`` 自己的迴圈裡就擋下。
        """
        path = tmp_path / "single_row_expansion.ods"
        cells_xml = "".join(
            f'<table:table-cell xmlns:table="{_TABLE_NS}" '
            'table:number-columns-repeated="512"/>'
            for _ in range(6000)
        )
        content = (
            "<office:document-content "
            'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            f'xmlns:table="{_TABLE_NS}">'
            "<office:body><office:spreadsheet>"
            f'<table:table table:name="s">'
            f'<table:table-row xmlns:table="{_TABLE_NS}">{cells_xml}</table:table-row>'
            "</table:table></office:spreadsheet></office:body></office:document-content>"
        ).encode()
        assert len(content) < 8 * 1024 * 1024, "payload 必須留在大小上限之內"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("content.xml", content)
        with pytest.raises(OdsReadError, match="儲存格總數超過上限"):
            read_ods(path)

    def test_budget_accumulates_across_multiple_tables(self, tmp_path: Path) -> None:
        """預算跨整份文件（可能有多張工作表）累計，不是每張表各自歸零。

        否則只要把同樣的展開量拆成好幾張表格，就能繞過單一表格的預算。

        儲存格內容刻意給非空文字：內容全空的儲存格會被既有的「去除列尾
        空白儲存格」邏輯裁掉，裁掉後實際占用的記憶體確實很小，不構成
        威脅；真正需要擋下的是內容不會被裁掉、確實會被完整展開的情形。
        """
        table_xml = (
            f'<table:table-row xmlns:table="{_TABLE_NS}" '
            'table:number-rows-repeated="512">'
            f'<table:table-cell xmlns:table="{_TABLE_NS}" '
            f'xmlns:text="{_TEXT_NS}" table:number-columns-repeated="512">'
            "<text:p>x</text:p></table:table-cell>"
            "</table:table-row>"
        )
        # 每張表格單獨看都在預算之內（512 列 x 512 格＝262,144 格，
        # 低於 2,000,000 的上限），但 20 張加起來就會超過。
        tables = "".join(
            f'<table:table table:name="s{i}">{table_xml}</table:table>'
            for i in range(20)
        )
        content = (
            "<office:document-content "
            'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            f'xmlns:table="{_TABLE_NS}">'
            f"<office:body><office:spreadsheet>{tables}</office:spreadsheet></office:body>"
            "</office:document-content>"
        ).encode()
        path = tmp_path / "multi_table_expansion.ods"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("content.xml", content)
        with pytest.raises(OdsReadError, match="儲存格總數超過上限"):
            read_ods(path)

    def test_real_files_stay_well_within_the_budget(self, tmp_path: Path) -> None:
        """真正的時刻表檔案最多不到 200 列、幾千格——上限本身沒有訂得
        太緊，不會誤傷正常檔案（用既有的排版 A 測試樣本間接驗證）。
        """
        table = (
            '<table:table table:name="s">'
            + _row(_cell("車") + _cell("次") + _cell("站\n名\n間") + _cell("二"))
            + _row(
                _cell(covered=True)
                + _cell(covered=True)
                + _cell(covered=True)
                + _cell("水")
            )
            + _row(
                _cell("區間車") + _cell("2705") + _cell("二水－車埕") + _cell("08:00")
            )
            + "</table:table>"
        )
        sheets = read_ods(_write_ods(tmp_path / "small.ods", table))
        assert sheets


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
            + _row(
                _cell("竹 南")
                + _cell(repeat=3)
                + _cell("08:38")
                + _cell("07:54")
                + _cell("-")
            )
            # 山線／海線並排列：左欄海線、右欄山線
            + _row(
                _cell("後 龍")
                + _cell(repeat=3)
                + _cell("08:49")
                + _cell("08:07")
                + _cell("")
            )
            + _row(
                _cell("彰 化")
                + _cell(repeat=3)
                + _cell("09:43")
                + _cell("09:17")
                + _cell("-")
            )
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
            + _row(
                _cell("高雄") + _cell(repeat=3) + _cell("08:26") + _cell("") + _cell("")
            )
            + _row(
                _cell("Kaohsiung")
                + _cell(repeat=3)
                + _cell("08:29")
                + _cell("")
                + _cell("")
            )
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

    def test_both_station_names_are_offered_as_candidates(self, tmp_path: Path) -> None:
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


class TestRollingStockAssignment:
    """車種 → 車輛型式的分派（``dataset.build.rolling_stock_for``）。"""

    def test_tze_chiang_uses_more_than_one_vehicle_type(
        self, game_data: GameData
    ) -> None:
        """自強號是車種不是車輛型式，不能全部掛成 EMU3000。"""
        used = {
            s.rolling_stock_id
            for s in game_data.services.values()
            if s.train_type == "tze_chiang"
        }
        assert {"EMU3000", "TEMU1000", "TEMU2000", "PP"} <= used

    def test_class_name_decides_the_vehicle_type(self) -> None:
        assert rolling_stock_for("自強3000", set()) == "EMU3000"
        assert rolling_stock_for("普悠瑪", set()) == "TEMU2000"
        assert rolling_stock_for("太魯閣", set()) == "TEMU1000"
        assert rolling_stock_for("自強", set()) == "PP"
        assert rolling_stock_for("莒光", set()) == "CK"
        assert rolling_stock_for("區間車", set()) == "EMU900"

    def test_non_electrified_branch_forces_diesel(self) -> None:
        """碰到非電氣化支線就是柴油客車，即使大半行程在電氣化幹線上。"""
        assert rolling_stock_for("區間快", {"彰化", "二水", "集集", "車埕"}) == "DR1000"
        assert rolling_stock_for("區間車", {"新竹", "竹中", "上員", "內灣"}) == "DR1000"
        assert rolling_stock_for("區間車", {"八堵", "瑞芳", "十分", "菁桐"}) == "DR1000"

    def test_liujia_line_keeps_electric_stock(self) -> None:
        """六家線已電氣化，新竹至六家的區間車不是柴油客車。"""
        assert rolling_stock_for("區間車", {"新竹", "北新竹", "竹中", "六家"}) == "EMU900"

    def test_trunk_stations_of_a_branch_do_not_trigger_diesel(self) -> None:
        """二水、三貂嶺、瑞芳在幹線上，只停這些站的幹線列車不受影響。"""
        assert rolling_stock_for("區間車", {"彰化", "二水", "斗六"}) == "EMU900"
        assert rolling_stock_for("自強", {"瑞芳", "三貂嶺", "雙溪"}) == "PP"

    @pytest.mark.parametrize(
        ("branch_station", "expected_line"),
        [("集集", "jiji"), ("內灣", "neiwan"), ("菁桐", "pingxi"), ("八斗子", "pingxi")],
    )
    def test_branch_services_in_shipped_data_are_diesel(
        self, game_data: GameData, branch_station: str, expected_line: str
    ) -> None:
        station = next(
            s for s in game_data.stations.values() if s.name_zh_tw == branch_station
        )
        assert expected_line in station.line_ids
        services = [
            s
            for s in game_data.services.values()
            if station.id in s.stop_station_ids or station.id in s.pass_station_ids
        ]
        assert services, f"時刻表裡沒有停靠{branch_station}的班次"
        assert all(s.rolling_stock_id == "DR1000" for s in services)

    def test_liujia_services_in_shipped_data_are_electric(
        self, game_data: GameData
    ) -> None:
        liujia = next(s for s in game_data.stations.values() if s.name_zh_tw == "六家")
        services = [
            s for s in game_data.services.values() if liujia.id in s.stop_station_ids
        ]
        assert services
        assert all(s.rolling_stock_id == "EMU900" for s in services)

    def test_liujia_is_a_stub_off_zhuzhong(self, game_data: GameData) -> None:
        """六家由竹中分歧出去，內灣線列車不會經過六家。"""
        ids = {s.name_zh_tw: s.id for s in game_data.stations.values()}
        liujia = f"STA_{ids['六家']}"
        zhuzhong = f"STA_{ids['竹中']}"
        shangyuan = f"STA_{ids['上員']}"

        assert game_data.network.link(zhuzhong, liujia) is not None
        assert game_data.network.link(zhuzhong, shangyuan) is not None
        assert game_data.network.link(liujia, shangyuan) is None

        for route in game_data.routes.values():
            if liujia in route.node_ids and shangyuan in route.node_ids:
                pytest.fail(f"{route.id} 同時經過六家與上員")

    def test_rolling_stock_for_sees_path_only_stations(self) -> None:
        """車輛型式判斷必須看完整行經路徑，不能只看時刻表印出時刻的站。

        對號列車時刻表只印主要車站，實際尋路可能補入時刻表沒印出來的
        中間站；呼叫端（``build_dataset``）必須把這些站一併納入判斷，
        單純傳入 ``served_names`` 少了它們就會漏判非電氣化支線。
        """
        served_names = {"樹林", "臺北"}  # 時刻表印出時刻的站，不含支線
        path_only = {"海科館", "八斗子"}  # 只在完整路徑上出現
        assert rolling_stock_for("自強3000", served_names) == "EMU3000"
        assert rolling_stock_for("自強3000", served_names | path_only) == "DR1000"

    def test_train_number_override_beats_class_and_branch(self) -> None:
        """車次覆寫優先於車種與支線判斷。"""
        assert rolling_stock_for("自強", set(), train_number="209") == "DR3100"
        assert (
            rolling_stock_for(
                "自強", {"海科館", "八斗子"}, train_number="209"
            )
            == "DR3100"
        )

    def test_dr3100_override_applied_to_shipped_data(self, game_data: GameData) -> None:
        """人工確認由 DR3100 擔當的車次（見 trains.json 的 DR3100 note）。"""
        for number in ("209", "221", "238", "246"):
            assert game_data.services[number].rolling_stock_id == "DR3100"


class TestRuifangSijiaotingTopology:
    """瑞芳－四腳亭是宜蘭線本線相鄰站，不應繞經深澳線分歧。

    這是先前版本（此 PR 之前，即 6692c61）就存在的路網缺陷：圖裡完全沒有
    瑞芳與四腳亭的直接連線，唯一連通路徑是深澳線分歧（瑞芳→海科館→八斗子
    →四腳亭）。連只有兩站、時刻表明確排點 6 分鐘直達的區間車（例如 4017
    次）都被迫繞經深澳線——證明問題出在路網本身缺一條邊，不是任何特定車次
    的判斷邏輯。這連帶讓 157 個東部幹線班次（太魯閣、普悠瑪、自強3000 等）
    的路徑經過深澳線車站，若車輛型式判斷改用完整路徑（如上一組測試），會把
    它們全部誤判成 DR1000——但這些真的是電聯車，不會跑深澳線。修法是補上
    瑞芳－四腳亭的直接連線（topology_overrides.json 的 add_station_links），
    而不是接受繞路後將錯就錯地改車輛型式。
    """

    def test_direct_link_exists(self, game_data: GameData) -> None:
        ids = {s.name_zh_tw: s.id for s in game_data.stations.values()}
        ruifang = f"STA_{ids['瑞芳']}"
        sijiaoting = f"STA_{ids['四腳亭']}"
        assert game_data.network.link(ruifang, sijiaoting) is not None
        assert game_data.network.link(sijiaoting, ruifang) is not None

    def test_no_route_detours_through_the_shenao_branch(
        self, game_data: GameData
    ) -> None:
        """深澳線車站只能出現在真的以深澳線為端點的路線上。"""
        ids = {s.name_zh_tw: s.id for s in game_data.stations.values()}
        haikeguan = f"STA_{ids['海科館']}"
        badouzi = f"STA_{ids['八斗子']}"
        branch = {haikeguan, badouzi}

        for route in game_data.routes.values():
            touched = branch & set(route.node_ids)
            if not touched:
                continue
            endpoints = {route.node_ids[0], route.node_ids[-1]}
            assert endpoints & branch, (
                f"{route.id} 經過深澳線車站但兩端都不在深澳線上，"
                "應該是繞路而非真的行駛深澳線"
            )

    def test_local_train_with_a_scheduled_direct_run_does_not_detour(
        self, game_data: GameData
    ) -> None:
        """4017 次時刻表明確排點瑞芳直達四腳亭 6 分鐘，路徑不應繞經深澳線。"""
        service = game_data.services["4017"]
        route = game_data.routes[service.route_id]
        ids = {s.name_zh_tw: s.id for s in game_data.stations.values()}
        haikeguan = f"STA_{ids['海科館']}"
        badouzi = f"STA_{ids['八斗子']}"
        assert haikeguan not in route.node_ids
        assert badouzi not in route.node_ids

    def test_no_shipped_service_is_misclassified_by_a_path_detour(
        self, game_data: GameData
    ) -> None:
        """迴歸測試：完整路徑經過非電氣化支線車站的班次都必須是柴油客車。

        直接對照 ``dataset.build.NON_ELECTRIFIED_BRANCH_STATIONS``：任何
        班次只要路線的完整節點序列包含清單中的車站，車輛型式就必須是
        ``BRANCH_ROLLING_STOCK``；不符合的話代表要嘛路網又繞路了，要嘛
        車輛型式判斷又只看了 served_names。
        """
        from railway_sim.dataset.build import (
            BRANCH_ROLLING_STOCK,
            NON_ELECTRIFIED_BRANCH_STATIONS,
        )

        offenders = []
        for service in game_data.services.values():
            route = game_data.routes.get(service.route_id)
            if route is None:
                continue
            path_names = {
                game_data.stations[sid].name_zh_tw
                for sid in route.station_ids
                if sid in game_data.stations
            }
            if (
                path_names & NON_ELECTRIFIED_BRANCH_STATIONS
                and service.rolling_stock_id != BRANCH_ROLLING_STOCK
            ):
                offenders.append((service.train_number, service.rolling_stock_id))
        assert not offenders, offenders


def _minimal_build_result() -> BuildResult:
    """一個保證能通過 ``load_game_data`` 驗證的最小資料集。

    完全空白的站、路線、班次不會觸發 :func:`load_game_data` 裡任何一項
    交叉參照檢查，因此可以拿來測試「寫入成功」路徑本身，不需要真的跑
    一次完整匯入。
    """
    return BuildResult(
        stations={"meta": {"description": "test"}, "stations": []},
        routes={
            "meta": {"description": "test"},
            "lines": {},
            "region_rules": {},
            "nodes": [],
            "links": [],
            "routes": [],
        },
        timetables={"meta": {"description": "test"}, "services": []},
    )


class TestWriteDatasetIsAtomic:
    """``write_dataset`` 不得讓一次失敗的匯入毀掉正式資料。

    先前的實作依序直接覆寫 ``data/`` 底下的 JSON，驗證卻是寫完之後才做；
    驗證失敗時正式目錄已經是壞資料。這裡驗證新流程：先在暫存目錄驗證
    完整資料集，只有通過才提交，任何一步失敗都完全不動正式目錄。
    """

    @pytest.fixture
    def isolated_data_dir(self, tmp_path: Path) -> Path:
        """正式 ``data/`` 目錄的獨立副本，測試可以安全地寫壞它。"""
        target = tmp_path / "data"
        shutil.copytree(default_data_dir(), target)
        return target

    def test_failed_validation_leaves_existing_files_untouched(
        self, isolated_data_dir: Path
    ) -> None:
        """重現實際回報的情境：stations 被清空，routes 仍參照舊車站。"""
        before = {
            name: (isolated_data_dir / name).read_text(encoding="utf-8")
            for name in ("stations.json", "routes.json", "timetables.json")
        }
        original_stations = json.loads(before["stations.json"])

        broken = BuildResult(
            stations={"meta": original_stations["meta"], "stations": []},
            routes=json.loads(before["routes.json"]),
            timetables=json.loads(before["timetables.json"]),
        )

        with pytest.raises(ValueError, match="未通過驗證"):
            write_dataset(broken, isolated_data_dir)

        for name, original_text in before.items():
            assert (isolated_data_dir / name).read_text(encoding="utf-8") == (
                original_text
            ), f"{name} 在驗證失敗後不應被改動"

    def test_failed_validation_leaves_no_staging_or_backup_litter(
        self, isolated_data_dir: Path
    ) -> None:
        original_stations = json.loads(
            (isolated_data_dir / "stations.json").read_text(encoding="utf-8")
        )
        broken = BuildResult(
            stations={"meta": original_stations["meta"], "stations": []},
            routes=json.loads(
                (isolated_data_dir / "routes.json").read_text(encoding="utf-8")
            ),
            timetables=json.loads(
                (isolated_data_dir / "timetables.json").read_text(encoding="utf-8")
            ),
        )

        with pytest.raises(ValueError):
            write_dataset(broken, isolated_data_dir)

        leftovers = [
            p.name
            for p in isolated_data_dir.iterdir()
            if p.name.startswith(IMPORT_STAGING_PREFIX) or p.name.endswith(".bak")
        ]
        assert leftovers == []

    def test_missing_trains_json_is_reported_before_any_write(
        self, isolated_data_dir: Path
    ) -> None:
        """trains.json／keymap.json 不受匯入改動，但驗證仍需要它們存在。"""
        (isolated_data_dir / "trains.json").unlink()
        before_stations = (isolated_data_dir / "stations.json").read_text(
            encoding="utf-8"
        )

        with pytest.raises(FileNotFoundError, match="trains.json"):
            write_dataset(_minimal_build_result(), isolated_data_dir)

        assert (isolated_data_dir / "stations.json").read_text(
            encoding="utf-8"
        ) == before_stations

    def test_successful_write_replaces_all_three_files(
        self, isolated_data_dir: Path
    ) -> None:
        written = write_dataset(_minimal_build_result(), isolated_data_dir)
        assert {p.name for p in written} == {
            "stations.json",
            "routes.json",
            "timetables.json",
        }

        stored = json.loads(
            (isolated_data_dir / "stations.json").read_text(encoding="utf-8")
        )
        assert stored["stations"] == []

    def test_successful_write_leaves_no_staging_or_backup_litter(
        self, isolated_data_dir: Path
    ) -> None:
        write_dataset(_minimal_build_result(), isolated_data_dir)

        leftovers = [
            p.name
            for p in isolated_data_dir.iterdir()
            if p.name.startswith(IMPORT_STAGING_PREFIX) or p.name.endswith(".bak")
        ]
        assert leftovers == []


class TestSelfHealsAfterInterruption:
    """行程在逐檔取代中途被強制中止（kill -9、斷電）時的復原。

    ``write_dataset`` 內部的 ``try/except`` 只能攔到 Python 例外，攔不到
    行程被整個砍掉——如果中止點剛好落在「第一個檔案已經取代成功、第二
    個還沒開始」的縫隙之間，正式目錄理論上會停在一半新一半舊的狀態。

    這裡不模擬「怎麼砍掉行程」（那沒辦法用 pytest 可靠重現），而是直接
    在磁碟上重建那個縫隙會留下的殘留樣子：一個沒被清掉的暫存目錄，裡面
    有一個檔案的備份、正式目錄裡那個檔案已經是新內容，其餘檔案沒有備份
    （代表它們的取代根本還沒開始）。驗證下一次讀取或匯入都能自動修復。
    """

    @pytest.fixture
    def isolated_data_dir(self, tmp_path: Path) -> Path:
        target = tmp_path / "data"
        shutil.copytree(default_data_dir(), target)
        return target

    def _simulate_kill_after_first_replace(self, data_dir: Path) -> dict[str, str]:
        """重現「第一個檔案取代成功、行程隨即被砍掉」會留下的殘留狀態。"""
        before = {
            name: (data_dir / name).read_text(encoding="utf-8")
            for name in ("stations.json", "routes.json", "timetables.json")
        }
        staging = data_dir / f"{IMPORT_STAGING_PREFIX}deadbeef"
        staging.mkdir()
        # stations.json 已經被取代成「新」內容，備份留在暫存目錄裡；
        # routes.json／timetables.json 都還沒動，因此沒有備份——這正是
        # write_dataset 逐檔取代迴圈跑到一半被砍掉會留下的樣子。
        shutil.copyfile(data_dir / "stations.json", staging / "stations.json.bak")
        (data_dir / "stations.json").write_text(
            '{"meta": {}, "stations": []}', encoding="utf-8"
        )
        return before

    def test_load_game_data_heals_before_reading(self, isolated_data_dir: Path) -> None:
        """對應「下一次遊戲啟動無法載入資料」的回報情境。"""
        before = self._simulate_kill_after_first_replace(isolated_data_dir)

        data = load_game_data(isolated_data_dir)

        assert data.issues == []
        assert (isolated_data_dir / "stations.json").read_text(
            encoding="utf-8"
        ) == before["stations.json"]
        assert list(isolated_data_dir.glob(f"{IMPORT_STAGING_PREFIX}*")) == []

    def test_write_dataset_heals_a_prior_interruption_before_importing(
        self, isolated_data_dir: Path
    ) -> None:
        """對應「下一次重新執行匯入」的情境：先自我修復，再開始新的匯入。"""
        self._simulate_kill_after_first_replace(isolated_data_dir)

        written = write_dataset(_minimal_build_result(), isolated_data_dir)

        assert {p.name for p in written} == {
            "stations.json",
            "routes.json",
            "timetables.json",
        }
        assert list(isolated_data_dir.glob(f"{IMPORT_STAGING_PREFIX}*")) == []

    def test_heal_is_idempotent_with_no_staging_directory(
        self, isolated_data_dir: Path
    ) -> None:
        """沒有殘留的正常情形（絕大多數呼叫）必須是無害的無操作。"""
        before = (isolated_data_dir / "stations.json").read_text(encoding="utf-8")

        heal_interrupted_import(isolated_data_dir)

        assert (isolated_data_dir / "stations.json").read_text(
            encoding="utf-8"
        ) == before

    def test_heal_tolerates_a_missing_directory(self, tmp_path: Path) -> None:
        heal_interrupted_import(tmp_path / "does-not-exist")


class TestBackupCreationIsCrashSafe:
    """建立備份的過程本身也可能被中止，不能讓半成品污染到 ``.bak``。

    ``heal_interrupted_import`` 把任何名為 ``<檔名>.bak`` 的檔案都當成
    「取代前的完整備份」直接信任並還原。如果備份是直接寫成這個檔名，
    寫到一半被中止（斷電、kill -9）就會留下一個檔名看起來完整、內容
    其實截斷的檔案；下次修復流程會把這份殘缺內容當成正確備份，覆寫回
    正式檔案，反而毀掉原本完好的資料。``write_dataset`` 因此改成先寫到
    修復流程認不得的暫存檔名，完整寫完才原子發布成 ``.bak``。
    """

    @pytest.fixture
    def isolated_data_dir(self, tmp_path: Path) -> Path:
        target = tmp_path / "data"
        shutil.copytree(default_data_dir(), target)
        return target

    def test_a_truncated_dot_bak_dot_tmp_is_never_trusted(
        self, isolated_data_dir: Path
    ) -> None:
        """重現「備份複製到一半被中止」會留下的樣子：stations.json 已經
        取代完成（有完整備份），routes.json 的備份還在複製中——這種
        殘留必須用不會被復原流程辨識的暫存檔名，模擬出來的截斷內容
        才不會被誤認成完整備份。
        """
        before_stations = (isolated_data_dir / "stations.json").read_text(
            encoding="utf-8"
        )
        before_routes = (isolated_data_dir / "routes.json").read_text(encoding="utf-8")

        staging = isolated_data_dir / f"{IMPORT_STAGING_PREFIX}deadbeef"
        staging.mkdir()
        shutil.copyfile(
            isolated_data_dir / "stations.json", staging / "stations.json.bak"
        )
        (isolated_data_dir / "stations.json").write_text(
            '{"meta": {}, "stations": []}', encoding="utf-8"
        )
        # routes.json 的備份複製到一半就被中止：內容截斷，且檔名不是
        # write_dataset 實際會產生的「.bak」，而是複製完成前的暫存名稱。
        (staging / "routes.json.bak.tmp").write_bytes(
            before_routes.encode("utf-8")[:100]
        )
        # routes.json 本身從未被動過——它自己的取代步驟根本還沒開始。

        data = load_game_data(isolated_data_dir)

        assert data.issues == []
        assert (isolated_data_dir / "stations.json").read_text(
            encoding="utf-8"
        ) == before_stations
        assert (isolated_data_dir / "routes.json").read_text(
            encoding="utf-8"
        ) == before_routes, "截斷的暫存備份不得覆寫仍然完好的正式檔案"
        assert list(isolated_data_dir.glob(f"{IMPORT_STAGING_PREFIX}*")) == []

    def test_write_dataset_never_writes_directly_to_a_dot_bak_name(
        self, isolated_data_dir: Path
    ) -> None:
        """成功匯入之後，過程中不應該出現任何殘留的 ``.bak.tmp``。

        這不直接證明「寫入 .bak 本身是原子的」，但確認正常流程走完後
        沒有半成品殘留，且最終資料仍然正確——搭配上一個測試（直接偽造
        半成品、驗證修復流程不會信任它）一起構成完整證據鏈。
        """
        write_dataset(_minimal_build_result(), isolated_data_dir)

        leftovers = [
            p.name
            for p in isolated_data_dir.iterdir()
            if p.name.startswith(IMPORT_STAGING_PREFIX) or ".bak" in p.name
        ]
        assert leftovers == []


class TestCliHandlesCorruptSource:
    """CLI 對毀損來源檔案要輸出一致的「匯入失敗」訊息，不能讓例外原樣往外跑。

    對應三種毀損情形：不是 zip、缺少 content.xml、content.xml 不是合法
    XML。這三種在來源檔案損毀（下載中斷、編輯器另存）時都可能發生。
    """

    @pytest.fixture
    def isolated_data_dir(self, tmp_path: Path) -> Path:
        target = tmp_path / "data"
        shutil.copytree(default_data_dir(), target)
        return target

    def _run(
        self, source_dir: Path, data_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> tuple[int, str]:
        from railway_sim.dataset.__main__ import main

        code = main(["--source", str(source_dir), "--out", str(data_dir)])
        return code, capsys.readouterr().err

    def test_not_a_zip_file(
        self,
        tmp_path: Path,
        isolated_data_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "corrupt.ods").write_bytes(b"not a zip file at all")

        code, err = self._run(source_dir, isolated_data_dir, capsys)
        assert code == 2
        assert "匯入失敗" in err
        assert "corrupt.ods" in err

    def test_missing_content_xml(
        self,
        tmp_path: Path,
        isolated_data_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        with zipfile.ZipFile(source_dir / "empty.ods", "w"):
            pass

        code, err = self._run(source_dir, isolated_data_dir, capsys)
        assert code == 2
        assert "匯入失敗" in err
        assert "empty.ods" in err

    def test_invalid_xml(
        self,
        tmp_path: Path,
        isolated_data_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        with zipfile.ZipFile(source_dir / "badxml.ods", "w") as archive:
            archive.writestr("content.xml", "<not-closed>")

        code, err = self._run(source_dir, isolated_data_dir, capsys)
        assert code == 2
        assert "匯入失敗" in err
        assert "badxml.ods" in err

    def test_corrupt_source_does_not_touch_existing_data(
        self,
        tmp_path: Path,
        isolated_data_dir: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """讀取階段就失敗，連暫存驗證都還沒開始，正式資料自然不受影響。"""
        before = (isolated_data_dir / "stations.json").read_text(encoding="utf-8")
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "corrupt.ods").write_bytes(b"garbage")

        self._run(source_dir, isolated_data_dir, capsys)

        after = (isolated_data_dir / "stations.json").read_text(encoding="utf-8")
        assert after == before
