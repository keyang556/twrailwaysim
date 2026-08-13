"""從維基百科條目讀出捷運各線的車站表。

來源是使用者提供的「另存新檔」條目（``<線名> - 維基百科，自由的百科全書.htm``），
每一條線的條目裡都有同一張「車站」表：

===== ============ ==================== ============================
編號  名稱         型式                 距離（單位：公里）
      中文  英文   站體  月台           與前一站站距  累計
===== ============ ==================== ============================

本模組只取**車站順序、車站代碼、中英文站名與里程**——這四項是模擬器需要的，
也是條目裡有明確出處的部分。月台型式、所在地、啟用日期一律不取：模擬器用不到，
取了反而要為每一個欄位負驗證責任（規格 §22）。

為什麼自己剖析 HTML
-------------------

專案不帶執行期相依套件，因此不用 BeautifulSoup。標準函式庫足以應付這張表，
剖析不出來的列會被跳過並記進 :attr:`StationTable.warnings`，不會讓匯入失敗。

為什麼一定要展開合併儲存格
--------------------------

這張表大量使用 ``rowspan``，而且**里程欄本身也會被合併**：三鶯線的
「臺北大學→鶯歌車站」中間有一個還沒命名的 LB07a，條目就把 2.980／9.858
這一組里程用 ``rowspan="2"`` 橫跨 LB07a 與 LB08 兩列，因此 LB08 那一列自己
**沒有**任何里程儲存格。若照 ``<td>`` 的出現順序讀，鶯歌車站就會整站沒有里程，
或更糟——讀到隔壁欄的數字。所以本模組先把表展開成完整的儲存格矩陣，再依
表頭找欄位，欄位位置與合併狀況都不再影響結果。

車站代碼有兩種寫法
------------------

台北捷運各線用車站編號圖示（``<img alt="BL01">``），三鶯線與機場捷運則直接
寫文字（``<td><b>LB02</b></td>``）。兩種都要認得，否則會有整條線抓不到代碼。
未啟用的車站兩種都沒有（條目不會給還沒編號的車站掛圖示），因此沒有代碼的列
一律略過——這正好是「這一站還沒通車」最可靠的訊號。
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["StationRow", "StationTable", "parse_station_table", "read_page"]

#: 車站表的辨識字串。條目裡只有這張表有這個欄位標題。
_TABLE_MARKER = "與前一站站距"

_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_CELL = re.compile(r"<(t[dh])\b([^>]*)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
#: 合併儲存格的屬性。引號可有可無、單雙引號皆可——HTML 允許三種寫法，
#: 只認其中一種會在來源換一種寫法時整欄讀成空的。
_SPAN_ATTR = re.compile(r"""\b(rowspan|colspan)\s*=\s*["']?(\d+)["']?""", re.IGNORECASE)
_SUP = re.compile(r"<sup\b.*?</sup>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_ENGLISH = re.compile(r'<span lang="en">(.*?)</span>', re.DOTALL)
_CODE_IMAGE = re.compile(r'alt="([A-Z]{1,3}\d{1,2}[A-Za-z]?)"')
_CODE_TEXT = re.compile(r"^[A-Z]{1,3}\d{1,2}[A-Za-z]?$")

#: 里程一律是帶小數點的公里數。
#:
#: 維基百科在排序用的隱藏欄位裡塞了一長串數字（例如啟用日期的排序鍵
#: ``000000002015-07-06-0000``），限定格式才不會把它們當成里程。
_DISTANCE = re.compile(r"^\d{1,3}\.\d{1,3}$")

#: 表頭欄位名稱 → 本模組使用的欄位代碼。
_COLUMNS = {
    "編號": "code",
    "中文": "name_zh_tw",
    "英文": "name_en",
    "與前一站站距": "gap_km",
    "累計": "cumulative_km",
}


@dataclass(frozen=True)
class StationRow:
    """車站表的一列。

    Attributes:
        code: 車站代碼（``R22``、``A14a``、``LB02``）。這是使用者指定用來
            辨識車站的識別碼，也是廣播音檔的檔名。
        gap_km: 條目所列的與前一站站距（公里）。起點站沒有這一欄。
        cumulative_km: 自本表起點起算的累計里程（公里）。
    """

    code: str
    name_zh_tw: str
    name_en: str
    gap_km: float | None
    cumulative_km: float | None


@dataclass
class StationTable:
    """一條線剖析出來的車站表。"""

    rows: list[StationRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def by_code(self) -> dict[str, StationRow]:
        return {row.code: row for row in self.rows}


def _clean(cell_html: str) -> str:
    text = _TAG.sub("", _SUP.sub("", cell_html))
    # 零寬空格會混進站名，讓「台北車站」與「台北車站」比不出相等。
    return html_module.unescape(text).replace("\u200b", "").strip()


def read_page(path: str | Path) -> str:
    """讀取條目檔。編碼一律 UTF-8（維基百科的另存新檔皆然）。"""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _find_table(page: str) -> str | None:
    marker = page.find(_TABLE_MARKER)
    if marker < 0:
        return None
    start = page.rfind("<table", 0, marker)
    end = page.find("</table>", marker)
    if start < 0 or end < 0:
        return None
    return page[start:end]


def _expand(table_html: str) -> list[list[str]]:
    """把表展開成矩陣，每格放該儲存格的原始 HTML。

    合併儲存格會被複製到它涵蓋的每一格，因此展開後每一列的欄位位置一致。
    """
    grid: list[list[str]] = []
    #: 欄位 -> (本列之後還要再延伸幾列, 內容)
    carried: dict[int, tuple[int, str]] = {}

    for match in _ROW.finditer(table_html):
        row: dict[int, str] = {}
        next_carried: dict[int, tuple[int, str]] = {}

        # 先讓上面幾列延伸下來的合併儲存格佔住它們的欄位，本列自己的
        # 儲存格才會落在正確的位置。
        for column, (remaining, content) in carried.items():
            row[column] = content
            if remaining > 1:
                next_carried[column] = (remaining - 1, content)

        column = 0
        for cell_match in _CELL.finditer(match.group(1)):
            while column in row:
                column += 1
            attributes = {
                name.lower(): int(value)
                for name, value in _SPAN_ATTR.findall(cell_match.group(2))
            }
            content = cell_match.group(3)
            colspan = max(1, attributes.get("colspan", 1))
            rowspan = max(1, attributes.get("rowspan", 1))
            for offset in range(colspan):
                row[column + offset] = content
                if rowspan > 1:
                    next_carried[column + offset] = (rowspan - 1, content)
            column += colspan

        carried = next_carried
        width = max(row) + 1 if row else 0
        grid.append([row.get(index, "") for index in range(width)])
    return grid


def _column_map(grid: list[list[str]]) -> dict[str, int]:
    """由表頭找出每個欄位在矩陣中的位置。

    表頭有兩列（「名稱」底下才是「中文／英文」），因此同一欄可能被兩列
    都寫到；後面的那一列比較細，直接覆蓋前面的即可。
    """
    columns: dict[str, int] = {}
    for row in grid[:3]:
        for index, cell in enumerate(row):
            label = _clean(cell)
            key = _COLUMNS.get(label)
            if key is not None:
                columns[key] = index
    return columns


def parse_station_table(page: str) -> StationTable:
    """剖析條目中的車站表。

    找不到表、或表裡沒有任何可用的列，都只會得到空的 :class:`StationTable`
    加上一則警告；呼叫端負責決定這是不是錯誤。
    """
    table_html = _find_table(page)
    if table_html is None:
        return StationTable(warnings=["條目中找不到車站表（沒有「與前一站站距」欄位）"])

    grid = _expand(table_html)
    columns = _column_map(grid)
    missing = [name for name in ("code", "name_zh_tw", "cumulative_km") if name not in columns]
    if missing:
        return StationTable(warnings=[f"車站表缺少必要欄位：{'、'.join(missing)}"])

    result = StationTable()
    for row in grid:
        if _ENGLISH.search("".join(row)) is None:
            # 表頭、路段分隔列（「中和新蘆線（大橋頭—蘆洲）」）與註腳列。
            continue

        code = _station_code(_cell(row, columns["code"]))
        name_zh_tw = _clean(_cell(row, columns["name_zh_tw"]))
        if not code:
            # 還沒編號的車站＝還沒通車，不是資料錯誤。
            result.warnings.append(f"略過沒有車站代碼的列：{name_zh_tw or '（無站名）'}")
            continue

        result.rows.append(
            StationRow(
                code=code,
                name_zh_tw=name_zh_tw,
                name_en=_clean(_cell(row, columns.get("name_en", -1))),
                gap_km=_distance(_cell(row, columns.get("gap_km", -1))),
                cumulative_km=_distance(_cell(row, columns["cumulative_km"])),
            )
        )

    if not result.rows:
        result.warnings.append("車站表裡沒有任何可用的車站列")
    return result


def _cell(row: list[str], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    return row[index]


def _station_code(cell_html: str) -> str:
    image = _CODE_IMAGE.search(cell_html)
    if image is not None:
        return image.group(1)
    text = _clean(cell_html)
    return text if _CODE_TEXT.match(text) else ""


def _distance(cell_html: str) -> float | None:
    text = _clean(cell_html)
    return float(text) if _DISTANCE.match(text) else None
