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

#: 整份文件展開後允許的列數／儲存格數上限（跨所有工作表累計）。
#:
#: ``_MAX_REPEAT`` 只擋得住單一列或單一儲存格宣告的 repeat 值；大量各自
#: 不超過上限的 repeat 疊加起來，仍然可以展開成天文數字的輸出——這與
#: content.xml 的大小或巢狀深度無關，純粹是「展開後」的量爆炸。真正的
#: 時刻表檔案最多不到 200 列、幾千格，這裡給了數百倍餘裕，同時要能擋下
#: 刻意堆疊 repeat 造成的病態展開。
_MAX_TOTAL_ROWS = 50_000
_MAX_TOTAL_CELLS = 2_000_000


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


class _ExpansionBudget:
    """跨整份文件累計展開後的列數與儲存格數，超過預算立刻中止。

    ``_MAX_REPEAT`` 各自檢查單一儲存格或單一列的 repeat 屬性，但一份文件
    可以塞進大量「各自都不超過 512」的 repeat 宣告，疊加起來仍然遠遠超過
    合理範圍——先前的實作就是在這裡漏掉了累計上限，讓一個 326 KB、巢狀
    深度只有 5 層的檔案展開成超過一百萬列、五億多格。

    因此每一次要展開新的重複之前都先呼叫這裡檢查，超過預算就立刻中止，
    不讓中間清單先被建出來；不是等一整列或一整個表格展開完才事後計算。
    """

    __slots__ = ("cells", "path", "rows")

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows = 0
        self.cells = 0

    def add_cells(self, count: int) -> None:
        self.cells += count
        if self.cells > _MAX_TOTAL_CELLS:
            raise OdsReadError(
                f"{self.path.name}：展開後的儲存格總數超過上限"
                f"（{_MAX_TOTAL_CELLS:,}），拒絕讀取"
                "（可能是毀損或惡意的檔案）"
            )

    def add_rows(self, count: int) -> None:
        self.rows += count
        if self.rows > _MAX_TOTAL_ROWS:
            raise OdsReadError(
                f"{self.path.name}：展開後的列數超過上限"
                f"（{_MAX_TOTAL_ROWS:,}），拒絕讀取"
                "（可能是毀損或惡意的檔案）"
            )


def _read_row(row: ET.Element, budget: _ExpansionBudget) -> list[str]:
    cells: list[str] = []
    for cell in row:
        if cell.tag not in _CELL_TAGS:
            continue
        text = _cell_text(cell)
        repeat = _repeat_count(cell, "number-columns-repeated")
        # 同一列裡可以有大量各自獨立宣告 repeat 的儲存格；逐格檢查預算，
        # 而不是等整列展開完才計算，才擋得住「一列塞滿病態儲存格」本身
        # 就先把記憶體榨乾的情形。
        budget.add_cells(repeat)
        cells.extend([text] * repeat)
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
            ``content.xml`` 不是合法的 XML，``content.xml`` 解壓後大小
            ／巢狀深度超過上限，或展開後的列數／儲存格總數超過上限。
            臺鐵發布的檔案偶爾會因下載中斷或編輯器另存而毀損，這裡把
            底層例外統一包成一種帶檔名的錯誤，讓匯入指令能給出可行動的
            訊息，而不是原始 traceback；大小、深度與展開後總量上限則是
            防止一個刻意或不慎做出的病態檔案把匯入行程的記憶體或 CPU
            榨乾——三者分別擋下不同的放大手法：位元組數放大（zip
            bomb）、巢狀深度放大，以及大量各自合法的 repeat 屬性疊加
            出的展開量放大。
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

    # 跨整份文件（可能有多張工作表）累計，不是每張表各自歸零；否則把
    # 病態展開拆成多個表格就能繞過單一表格的預算。
    budget = _ExpansionBudget(path)

    sheets: list[Sheet] = []
    for table in root.iter(f"{{{_TABLE}}}table"):
        name = table.get(f"{{{_TABLE}}}name") or ""
        rows: list[tuple[str, ...]] = []
        for row in table.iter(f"{{{_TABLE}}}table-row"):
            cells = tuple(_read_row(row, budget))
            row_repeat = _repeat_count(row, "number-rows-repeated")
            # _read_row 已經把「這一列本身」的儲存格數計進預算一次；
            # 若整列還要再重複 row_repeat 次，展開後真正占用的儲存格數是
            # len(cells) * row_repeat，因此這裡只需要補計多出來的
            # (row_repeat - 1) 份，避免重複計算第一份。
            if row_repeat > 1:
                budget.add_cells(len(cells) * (row_repeat - 1))
            budget.add_rows(row_repeat)
            rows.extend([cells] * row_repeat)
        while rows and not rows[-1]:
            rows.pop()
        sheets.append(Sheet(name=name, rows=tuple(rows)))
    return sheets
