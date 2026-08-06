"""把臺鐵公布的時刻表 ``.ods`` 解析成中性的班次資料。

本模組只負責「讀懂臺鐵的排版」，不做任何遊戲端的判斷；車站編號、路線
建構、停靠規則推導都在 :mod:`railway_sim.dataset.build`。

臺鐵的檔案有兩種排版，同一個檔案裡也可能兩種都有，而且**一張工作表可以
含多個表格區塊**（例如「彰化→新竹」同一張表先山線、後海線）。

排版 A：一列一班車
------------------

車站是欄，班次是列。表頭「站　　名間」那一格之後就是車站欄；站名是縱書，
拆在連續兩到三個表頭列裡，同一欄由上往下接起來才是完整站名::

    車  區次        站名間     彰  成  新  烏 ...
                                   烏
                              化  功  日  日 ...
    區間車      3147  后里－潮州   -  05:21 ...

排版 B：一欄一班車
------------------

對號列車時刻表是轉置的：班次是欄，車站是列。表頭列依序為車種代碼
（``T.C.``／``C.K.``）、車種、車次、經由線別、始發站、``↓``、終點站，
接著每一列是一個車站（中文站名在最左欄，英文站名在右邊幾欄）。

兩種排版共用的儲存格慣例：

- ``HH:MM``：該站的時刻。
- ``-``：該站**通過**（有經過但不停）。
- 空白：該班次不行經該站。
- 車次後綴「追」：經由成追線（見各檔案末的「註：」）。
- 「山」／「海」欄：經由山線／海線。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from railway_sim.dataset.ods import Sheet, read_ods

__all__ = [
    "ParsedService",
    "ParsedStop",
    "SourceBlock",
    "normalise_name",
    "parse_file",
    "parse_sheet",
]

#: 表示「通過」的儲存格內容。
_PASS_MARKERS = frozenset({"-", "–", "—", "－", "↓", "|", "｜"})

#: ``HH:MM`` 時刻。臺鐵表中也出現過 ``H:MM``。
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

#: 車次：數字，可帶「追」等後綴。
_TRAIN_NUMBER_RE = re.compile(r"^(\d{1,4})([一-鿿]*)$")

#: 表頭中標示「站名間」的儲存格。
_STATION_HEADER_RE = re.compile(r"站.*名")

#: 排版 B 用來辨識班次欄的車種代碼列。
_CLASS_CODES = frozenset({"T.C.", "C.K.", "TC", "CK"})

#: 已知的車種名稱（出現在排版 A 的第一欄、排版 B 的車種列）。
KNOWN_TRAIN_CLASSES = (
    "自強3000",
    "普悠瑪",
    "太魯閣",
    "自強",
    "莒光",
    "復興",
    "區間快",
    "區間車",
    "普快車",
    "普快",
)

#: 縱書表頭無法單純由上往下接起來的例外。
#:
#: 「新城(太魯閣)」在表頭裡是一格內的雙欄縱書（``︵``／``新太``／``　魯``／
#: ``城閣``／``　 ︶``），照字面接會得到「︵新太魯城閣︶」。
NAME_ALIASES = {
    "︵新太魯城閣︶": "新城",
    "(新太魯城閣)": "新城",
    "新城太魯閣": "新城",
    "新城(太魯閣)": "新城",
    "新城（太魯閣）": "新城",
}


def normalise_name(raw: str) -> str:
    """正規化站名：去除空白、換行、全形空格、括號註記與並列的英文站名。"""
    if not raw:
        return ""
    text = unicodedata.normalize("NFKC", raw)
    text = re.sub(r"\s+", "", text)
    if text in NAME_ALIASES:
        return NAME_ALIASES[text]
    # 「新城(太魯閣)」這類括號註記不是站名的一部分。
    stripped = re.sub(r"[(（][^)）]*[)）]", "", text)
    text = stripped or text
    if text in NAME_ALIASES:
        return NAME_ALIASES[text]
    # 「新城Xincheng」：同一格裡中文站名後面接英文，英文不是站名的一部分。
    if re.search(r"[一-鿿]", text):
        text = re.sub(r"[A-Za-z][A-Za-z'\-]*$", "", text)
    return NAME_ALIASES.get(text, text)


def _is_latin_only(text: str) -> bool:
    """整格只有英文（羅馬拼音列），不是中文站名。"""
    return bool(text) and not re.search(r"[一-鿿]", text)


#: 站名只由中文字構成，長度二到五字（最長為「林榮新光」「長榮大學」）。
_STATION_NAME_RE = re.compile(r"^[一-鿿]{2,5}$")


def _looks_like_station_name(text: str) -> bool:
    """是否像站名。

    對號列車時刻表的左側欄位除了站名，還混著註解與宣導文字，因此不能把
    「班次欄左邊的中文」全部當成車站。
    """
    return bool(_STATION_NAME_RE.match(text))


def _normalise_title(raw: str) -> str:
    """區塊標題只壓縮空白，保留中英文全文（不套用站名的裁切規則）。"""
    return re.sub(r"[\s　]+", " ", unicodedata.normalize("NFKC", raw)).strip()


def _is_time(value: str) -> bool:
    match = _TIME_RE.match(value.strip())
    if not match:
        return False
    hour, minute = int(match.group(1)), int(match.group(2))
    return 0 <= hour <= 29 and 0 <= minute <= 59


def _is_pass(value: str) -> bool:
    return normalise_name(value) in _PASS_MARKERS


def _train_class(value: str) -> str:
    """把儲存格內容對應到車種名稱，認不出來回傳空字串。"""
    text = normalise_name(value)
    for name in KNOWN_TRAIN_CLASSES:
        if text == name:
            return name
    return ""


@dataclass(frozen=True)
class ParsedStop:
    """一個班次在一個車站的紀錄。"""

    station_name: str
    time_text: str
    """``HH:MM``；通過站為空字串。"""

    stops: bool

    alternatives: tuple[str, ...] = ()
    """同一列的其他候選站名。

    對號列車時刻表在竹南與彰化之間把山線與海線的站名並排成左右兩欄
    （左海線、右山線），同一列共用一個時刻欄；究竟是哪一站，要由該班次
    表頭的「山」／「海」標記決定。解析階段無從得知線別歸屬，因此把候選
    站名一起帶出來，交由 :mod:`railway_sim.dataset.build` 依線別解析。
    """

    @property
    def minutes(self) -> int | None:
        """自 00:00 起算的分鐘數；跨日（``24:xx``）會超過 1440。"""
        match = _TIME_RE.match(self.time_text)
        if not match:
            return None
        return int(match.group(1)) * 60 + int(match.group(2))


@dataclass(frozen=True)
class ParsedService:
    """一個班次。"""

    train_number: str
    """車次號碼，不含「追」等後綴。"""

    train_class: str
    """車種名稱，例如「區間車」「自強3000」。"""

    stops: tuple[ParsedStop, ...]
    origin_name: str = ""
    destination_name: str = ""
    via_note: str = ""
    """經由標記：「山」「海」「追」，沒有標示則為空字串。"""

    operating_mark: str = ""
    """行駛日期的符號（▲△⊕◆…），意義見來源檔案的「註：」。"""

    source_file: str = ""
    source_sheet: str = ""
    source_title: str = ""

    @property
    def calling_stops(self) -> tuple[ParsedStop, ...]:
        return tuple(s for s in self.stops if s.stops)

    @property
    def passes(self) -> tuple[ParsedStop, ...]:
        return tuple(s for s in self.stops if not s.stops)


@dataclass
class SourceBlock:
    """一張工作表裡的一個表格區塊。"""

    layout: str
    """``"row_per_train"`` 或 ``"column_per_train"``。"""

    title: str
    station_names: tuple[str, ...]
    """本區塊出現過的所有站名（不重複），供對照表檢查使用。"""

    sequences: tuple[tuple[str, ...], ...] = ()
    """依實際順序排列的站序，每條走廊一份。

    對號列車時刻表在竹南與彰化之間並排海線與山線兩欄，兩者各自是一條連續
    的站序；若把它們交錯成一份清單，相鄰關係會全部錯亂。"""
    services: list[ParsedService] = field(default_factory=list)
    legend: str = ""
    sheet_name: str = ""
    source_file: str = ""


# ----------------------------------------------------------------------
# 排版 A：一列一班車
# ----------------------------------------------------------------------
def _find_row_layout_headers(sheet: Sheet) -> list[tuple[int, int]]:
    """找出所有「站名間」表頭格，回傳 ``(列, 欄)``。"""
    found: list[tuple[int, int]] = []
    for index, row in enumerate(sheet.rows):
        for col, value in enumerate(row):
            text = normalise_name(value)
            # 同一列可能有左右並排的兩個區塊（例如集集線順行／逆行），
            # 因此整列都要掃完，不能找到一個就停。
            if _STATION_HEADER_RE.match(text) and "間" in text:
                found.append((index, col))
    return found


def _header_depth(sheet: Sheet, header_row: int, first_col: int) -> int:
    """站名縱書占用的表頭列數（含 ``header_row``）。"""
    last = header_row
    for index in range(header_row + 1, min(header_row + 4, len(sheet.rows))):
        row = sheet.rows[index]
        values = [normalise_name(v) for v in row[first_col:]]
        values = [v for v in values if v]
        if not values:
            continue
        # 出現時刻或車種就表示表頭結束、資料開始。
        if any(_is_time(v) or _train_class(v) for v in values):
            break
        last = index
    return last - header_row + 1


def _station_names_from_header(
    sheet: Sheet, header_row: int, depth: int, first_col: int, end_col: int
) -> list[str]:
    """把縱書表頭接成站名，索引與欄號一一對應（空字串代表非車站欄）。"""
    names: list[str] = []
    for col in range(first_col, end_col):
        parts = [
            normalise_name(sheet.cell(header_row + offset, col))
            for offset in range(depth)
        ]
        names.append(normalise_name("".join(parts)))
    return names


def _block_start_columns(sheet: Sheet) -> list[int]:
    """左右並排的區塊各自的起始欄，即出現車種名稱的欄。"""
    columns = set()
    for row in sheet.rows:
        for col, value in enumerate(row):
            if _train_class(value):
                columns.add(col)
    return sorted(columns)


def _parse_row_block(
    sheet: Sheet,
    header_row: int,
    start_col: int,
    first_col: int,
    end_row: int,
    end_col: int,
    source_file: str,
) -> SourceBlock:
    depth = _header_depth(sheet, header_row, first_col)
    names = _station_names_from_header(sheet, header_row, depth, first_col, end_col)
    title = ""
    if header_row:
        title = " ".join(
            _normalise_title(v)
            for v in sheet.rows[header_row - 1][start_col:end_col]
            if _normalise_title(v)
        )

    ordered = tuple(n for n in names if n)
    block = SourceBlock(
        layout="row_per_train",
        title=title,
        station_names=ordered,
        sequences=(ordered,),
        sheet_name=sheet.name,
        source_file=source_file,
    )

    for index in range(header_row + depth, end_row):
        row = sheet.rows[index]
        if not row:
            continue
        joined = normalise_name("".join(row))
        if joined.startswith("註"):
            block.legend = "".join(v for v in row if v.strip())
            continue

        train_class = _train_class(sheet.cell(index, start_col))
        if not train_class:
            continue

        # 車次是表頭欄裡最後一個純數字（有些檔案在車種與車次之間留空欄）。
        number = ""
        suffix = ""
        number_col = -1
        for col in range(start_col + 1, first_col):
            match = _TRAIN_NUMBER_RE.match(normalise_name(sheet.cell(index, col)))
            if match:
                number, suffix = match.group(1), match.group(2)
                number_col = col
        if not number:
            continue

        # 記號欄與經由欄都在車種與車次之間、或車次與站名欄之間。
        mark = ""
        via = suffix
        for col in range(start_col + 1, first_col):
            if col == number_col:
                continue
            text = normalise_name(sheet.cell(index, col))
            if not text or _TRAIN_NUMBER_RE.match(text):
                continue
            if text in ("山", "海", "追"):
                via = via or text
            elif len(text) == 1 and not text.isalnum():
                mark = text

        origin, destination = _split_endpoints(
            normalise_name(sheet.cell(index, first_col - 1))
        )

        stops = _row_stops(sheet, index, first_col, names)
        if not stops:
            continue

        block.services.append(
            ParsedService(
                train_number=number,
                train_class=train_class,
                stops=stops,
                origin_name=origin,
                destination_name=destination,
                via_note=via,
                operating_mark=mark,
                source_file=source_file,
                source_sheet=sheet.name,
                source_title=title,
            )
        )
    return block


def _row_stops(
    sheet: Sheet, index: int, first_col: int, names: list[str]
) -> tuple[ParsedStop, ...]:
    stops: list[ParsedStop] = []
    for offset, name in enumerate(names):
        if not name:
            continue
        value = normalise_name(sheet.cell(index, first_col + offset))
        if not value:
            continue
        if _is_time(value):
            stops.append(ParsedStop(name, value, stops=True))
        elif _is_pass(value):
            stops.append(ParsedStop(name, "", stops=False))
    return tuple(stops)


def _split_endpoints(text: str) -> tuple[str, str]:
    """把「彰化－車埕」拆成起訖站。"""
    for separator in ("－", "-", "–", "—", "~", "～"):
        if separator in text:
            left, _, right = text.partition(separator)
            return normalise_name(left), normalise_name(right)
    return "", ""


# ----------------------------------------------------------------------
# 排版 B：一欄一班車
# ----------------------------------------------------------------------
def _find_column_layout_header(sheet: Sheet) -> int | None:
    """找出車種代碼列（``T.C.``／``C.K.``）。"""
    for index, row in enumerate(sheet.rows[:12]):
        hits = sum(1 for value in row if normalise_name(value) in _CLASS_CODES)
        if hits >= 3:
            return index
    return None


def _parse_column_block(sheet: Sheet, code_row: int, source_file: str) -> SourceBlock:
    title = ""
    if code_row:
        title = " ".join(
            _normalise_title(v) for v in sheet.rows[code_row - 1] if _normalise_title(v)
        )

    class_row = code_row + 1
    number_row = code_row + 2
    via_row = code_row + 3

    # 班次欄：車種列有可辨識車種的欄。有些欄沒有 T.C./C.K.（區間車），
    # 所以以車種列為準，不是以代碼列。
    columns = [
        col
        for col in range(sheet.width)
        if _train_class(sheet.cell(class_row, col))
        and _TRAIN_NUMBER_RE.match(normalise_name(sheet.cell(number_row, col)))
    ]

    # 車站列：中文站名在班次欄左邊；竹南至彰化之間有並排的兩欄（海線、山線）。
    first_train_col = min(columns) if columns else 0
    station_rows: list[tuple[int, tuple[str, ...]]] = []
    legend = ""
    for index in range(number_row + 1, len(sheet.rows)):
        row = sheet.rows[index]
        if not row:
            continue
        if normalise_name(row[0]).startswith("註"):
            legend = "".join(v for v in row if v.strip())
            continue
        candidates: list[str] = []
        for col in range(first_train_col):
            name = normalise_name(sheet.cell(index, col))
            if not name or name in candidates:
                continue
            # 排除時刻、箭號、車種，以及「Taipei」這類羅馬拼音欄。
            if _train_class(name) or not _looks_like_station_name(name):
                continue
            candidates.append(name)
        if candidates:
            station_rows.append((index, tuple(candidates)))

    seen: list[str] = []
    for _, group in station_rows:
        for name in group:
            if name not in seen:
                seen.append(name)
    sequences = [tuple(group[0] for _, group in station_rows)]
    if any(len(group) > 1 for _, group in station_rows):
        sequences.append(
            tuple(group[1] if len(group) > 1 else group[0] for _, group in station_rows)
        )

    block = SourceBlock(
        layout="column_per_train",
        title=title,
        station_names=tuple(seen),
        sequences=tuple(sequences),
        legend=legend,
        sheet_name=sheet.name,
        source_file=source_file,
    )

    for col in columns:
        match = _TRAIN_NUMBER_RE.match(normalise_name(sheet.cell(number_row, col)))
        if not match:
            continue
        number, suffix = match.group(1), match.group(2)
        via = normalise_name(sheet.cell(via_row, col))
        if via not in ("山", "海", "追"):
            via = suffix

        stops: list[ParsedStop] = []
        for index, group in station_rows:
            value = normalise_name(sheet.cell(index, col))
            if not value:
                continue
            primary, alternatives = group[0], group[1:]
            if _is_time(value):
                stops.append(ParsedStop(primary, value, True, alternatives))
            elif _is_pass(value):
                stops.append(ParsedStop(primary, "", False, alternatives))
        if not stops:
            continue

        block.services.append(
            ParsedService(
                train_number=number,
                train_class=_train_class(sheet.cell(class_row, col)),
                stops=tuple(stops),
                origin_name=stops[0].station_name,
                destination_name=stops[-1].station_name,
                via_note=via,
                source_file=source_file,
                source_sheet=sheet.name,
                source_title=title,
            )
        )
    return block


# ----------------------------------------------------------------------
def parse_sheet(sheet: Sheet, source_file: str = "") -> list[SourceBlock]:
    """解析一張工作表，回傳其中所有表格區塊。"""
    if not sheet.rows:
        return []

    headers = _find_row_layout_headers(sheet)
    if headers:
        blocks: list[SourceBlock] = []
        starts = sorted(set(headers))
        block_columns = _block_start_columns(sheet)
        for position, (row, col) in enumerate(starts):
            # 下一個「站名間」表頭所在的列，就是本區塊資料的結尾。
            end_row = len(sheet.rows)
            for next_row, _ in starts[position + 1 :]:
                if next_row > row:
                    end_row = next_row - 1
                    break
            # 左右並排的區塊（例如集集線順行／逆行）以下一個區塊的車種欄為界，
            # 否則右邊區塊的欄位會被當成左邊區塊的車站。
            end_col = sheet.width
            for start in block_columns:
                if start > col:
                    end_col = start
                    break
            # 本區塊的車種欄：不超過表頭欄的最後一個區塊起始欄。
            start_col = 0
            for start in block_columns:
                if start <= col:
                    start_col = start
            blocks.append(
                _parse_row_block(
                    sheet, row, start_col, col + 1, end_row, end_col, source_file
                )
            )
        return [b for b in blocks if b.services]

    code_row = _find_column_layout_header(sheet)
    if code_row is not None:
        block = _parse_column_block(sheet, code_row, source_file)
        return [block] if block.services else []
    return []


def parse_file(path: str | Path) -> list[SourceBlock]:
    """解析一個 ``.ods`` 檔的所有工作表。"""
    name = Path(path).name
    blocks: list[SourceBlock] = []
    for sheet in read_ods(path):
        blocks.extend(parse_sheet(sheet, source_file=name))
    return blocks
