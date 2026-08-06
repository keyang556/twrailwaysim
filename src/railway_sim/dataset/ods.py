"""最小可用的 ODS（OpenDocument 試算表）讀取器。

臺鐵公布的時刻表是 ``.ods``。本模組只用標準函式庫（``zipfile`` ＋
``xml.etree``）把它讀成字串表格，因此匯入流程不需要 pandas 或 odfpy，
與專案「執行期零相依」的方針一致（``pyproject.toml`` 的 ``dependencies``
為空）。

只實作匯入時真正需要的功能：

- ``table:number-columns-repeated`` 與 ``table:number-rows-repeated`` 展開。
- ``table:covered-table-cell``（被合併儲存格覆蓋的位置）也占一格。
  時刻表的表頭大量使用縱向合併，不算進去就會整列錯位。
- 儲存格文字取 ``text:p`` 的全部內容，多個 ``text:p`` 以換行接起來。

不實作樣式、公式、圖表等與匯入無關的部分。
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

__all__ = ["OdsReadError", "Sheet", "read_ods"]

_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"

_CELL_TAGS = (f"{{{_TABLE}}}table-cell", f"{{{_TABLE}}}covered-table-cell")

#: 重複次數上限。ODS 會用一個 repeat 很大的空儲存格表示「這列剩下都是空的」，
#: 照著展開會產生數萬個空格，因此超過上限就視為結尾填充，只保留一格。
_MAX_REPEAT = 512


class OdsReadError(ValueError):
    """讀取 ``.ods`` 檔案失敗：檔案毀損，或格式不符 OpenDocument 試算表。

    訊息一律帶有來源檔名，讓匯入指令可以直接告訴使用者是哪個檔案需要
    重新取得，而不是讓 ``zipfile``／``xml.etree`` 的原始例外一路往外跑。
    """


@dataclass(frozen=True)
class Sheet:
    """一張工作表。"""

    name: str
    rows: tuple[tuple[str, ...], ...]

    def cell(self, row: int, col: int) -> str:
        """取一格文字；超出範圍回傳空字串。"""
        if 0 <= row < len(self.rows):
            line = self.rows[row]
            if 0 <= col < len(line):
                return line[col]
        return ""

    @property
    def width(self) -> int:
        return max((len(r) for r in self.rows), default=0)


def _cell_text(cell: ET.Element) -> str:
    paragraphs = ["".join(p.itertext()) for p in cell.findall(f"{{{_TEXT}}}p")]
    return "\n".join(paragraphs).strip()


def _repeat_count(element: ET.Element, attribute: str) -> int:
    raw = element.get(f"{{{_TABLE}}}{attribute}")
    if not raw:
        return 1
    try:
        count = int(raw)
    except ValueError:
        return 1
    if count < 1:
        return 1
    return 1 if count > _MAX_REPEAT else count


def _read_row(row: ET.Element) -> list[str]:
    cells: list[str] = []
    for cell in row:
        if cell.tag not in _CELL_TAGS:
            continue
        text = _cell_text(cell)
        cells.extend([text] * _repeat_count(cell, "number-columns-repeated"))
    while cells and not cells[-1]:
        cells.pop()
    return cells


def read_ods(path: str | Path) -> list[Sheet]:
    """讀取 ``path``，回傳所有工作表。

    每列尾端的空儲存格會被去除，因此不同列的長度不一定相同；取值請用
    :meth:`Sheet.cell`，不要直接索引。

    Raises:
        OdsReadError: 檔案不是有效的 zip、缺少 ``content.xml``，或
            ``content.xml`` 不是合法的 XML。臺鐵發布的檔案偶爾會因下載
            中斷或編輯器另存而毀損，這裡把三種底層例外統一包成一種帶
            檔名的錯誤，讓匯入指令能給出可行動的訊息，而不是原始
            traceback。
    """
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as archive:
            content = archive.read("content.xml")
    except zipfile.BadZipFile as exc:
        raise OdsReadError(
            f"{path.name}：不是有效的 ODS（zip）檔案，可能已毀損"
        ) from exc
    except KeyError as exc:
        raise OdsReadError(
            f"{path.name}：缺少 content.xml，不是有效的 ODS 檔案"
        ) from exc

    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise OdsReadError(f"{path.name}：content.xml 不是合法的 XML（{exc}）") from exc

    sheets: list[Sheet] = []
    for table in root.iter(f"{{{_TABLE}}}table"):
        name = table.get(f"{{{_TABLE}}}name") or ""
        rows: list[tuple[str, ...]] = []
        for row in table.iter(f"{{{_TABLE}}}table-row"):
            cells = tuple(_read_row(row))
            rows.extend([cells] * _repeat_count(row, "number-rows-repeated"))
        while rows and not rows[-1]:
            rows.pop()
        sheets.append(Sheet(name=name, rows=tuple(rows)))
    return sheets
