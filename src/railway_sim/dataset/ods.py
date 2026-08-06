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

import io
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

#: content.xml 解壓後允許的位元組數上限。臺鐵實際發布的時刻表中，最大的
#: content.xml 解壓後約 475 KB；這裡給了約 17 倍餘裕，同時足以擋下刻意
#: 製作的高壓縮比 zip bomb——``ZipInfo`` 裡宣告的大小可以被偽造，因此真正
#: 的防線是邊解壓邊累計實際位元組數（見 :func:`_read_bounded_member`），
#: 不是隨便相信宣告值。
_MAX_CONTENT_BYTES = 8 * 1024 * 1024

#: content.xml 的 XML 巢狀深度上限。真正的時刻表檔案巢狀深度只有個位數，
#: 這裡給了數十倍餘裕，同時擋下刻意或毀損造成的病態深度巢狀（見
#: :func:`_parse_bounded_xml`）。
_MAX_XML_DEPTH = 256


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


def _read_bounded_member(archive: zipfile.ZipFile, name: str, path: Path) -> bytes:
    """解壓 ``archive`` 裡的 ``name``，但不超過 :data:`_MAX_CONTENT_BYTES`。

    ``zipfile`` 的 ``read()`` 會先把整個成員解壓完才回傳，對一個刻意做出
    極端壓縮比的 zip（zip bomb）來說，這個動作本身就足以榨乾記憶體。
    這裡改用串流讀取、邊讀邊累計實際解壓出來的位元組數，一旦超過上限就
    立刻中止，不讓解壓動作把整個檔案的內容都攤開在記憶體裡。

    ``ZipInfo`` 裡宣告的大小只當快速篩選用，不能單獨信任——惡意 zip 可以
    偽造宣告值，因此真正擋下超量資料的是串流讀取迴圈裡的即時位元組計數。
    """
    info = archive.getinfo(name)
    if info.file_size > _MAX_CONTENT_BYTES or info.compress_size > _MAX_CONTENT_BYTES:
        raise OdsReadError(
            f"{path.name}：{name} 宣告大小超過上限"
            f"（{_MAX_CONTENT_BYTES // (1024 * 1024)} MiB），拒絕讀取"
            "（可能是毀損或惡意的檔案）"
        )

    chunks: list[bytes] = []
    total = 0
    with archive.open(name) as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_CONTENT_BYTES:
                raise OdsReadError(
                    f"{path.name}：{name} 解壓後超過大小上限"
                    f"（{_MAX_CONTENT_BYTES // (1024 * 1024)} MiB），拒絕讀取"
                    "（宣告大小與實際解壓結果不符，可能是惡意的檔案）"
                )
            chunks.append(chunk)
    return b"".join(chunks)


def _parse_bounded_xml(content: bytes, path: Path) -> ET.Element:
    """解析 ``content``，但對 XML 巢狀深度設上限，避免病態巢狀耗盡資源。

    直接呼叫 :func:`xml.etree.ElementTree.fromstring` 會先把整棵樹建好
    才回傳，深度失控時早就已經來不及中止；改用 :func:`~xml.etree.ElementTree.iterparse`
    以事件方式邊解析邊檢查深度，一超過上限就立刻中止，不必先把病態巢狀
    的樹整個建完。
    """
    depth = 0
    root: ET.Element | None = None
    try:
        for event, element in ET.iterparse(
            io.BytesIO(content), events=("start", "end")
        ):
            if event == "start":
                depth += 1
                if depth > _MAX_XML_DEPTH:
                    raise OdsReadError(
                        f"{path.name}：content.xml 巢狀深度超過上限"
                        f"（{_MAX_XML_DEPTH}），拒絕讀取"
                        "（可能是毀損或惡意的檔案）"
                    )
                if root is None:
                    root = element
            else:
                depth -= 1
    except ET.ParseError as exc:
        raise OdsReadError(f"{path.name}：content.xml 不是合法的 XML（{exc}）") from exc

    if root is None:
        raise OdsReadError(f"{path.name}：content.xml 沒有內容")
    return root


def read_ods(path: str | Path) -> list[Sheet]:
    """讀取 ``path``，回傳所有工作表。

    每列尾端的空儲存格會被去除，因此不同列的長度不一定相同；取值請用
    :meth:`Sheet.cell`，不要直接索引。

    Raises:
        OdsReadError: 檔案不是有效的 zip、缺少 ``content.xml``、
            ``content.xml`` 不是合法的 XML，或 ``content.xml`` 解壓後大小
            ／巢狀深度超過上限。臺鐵發布的檔案偶爾會因下載中斷或編輯器
            另存而毀損，這裡把底層例外統一包成一種帶檔名的錯誤，讓匯入
            指令能給出可行動的訊息，而不是原始 traceback；大小與深度
            上限則是防止一個刻意或不慎做出的病態檔案把匯入行程的記憶體
            或 CPU 榨乾。
    """
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as archive:
            content = _read_bounded_member(archive, "content.xml", path)
    except zipfile.BadZipFile as exc:
        raise OdsReadError(
            f"{path.name}：不是有效的 ODS（zip）檔案，可能已毀損"
        ) from exc
    except KeyError as exc:
        raise OdsReadError(
            f"{path.name}：缺少 content.xml，不是有效的 ODS 檔案"
        ) from exc

    root = _parse_bounded_xml(content, path)

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
