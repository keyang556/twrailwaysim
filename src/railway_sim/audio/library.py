"""廣播音檔索引。

檔案配置
--------

::

    data/audio/announcements/
        variants.json              ← 方向版本的挑選規則（可手改）
        manifest.json              ← 匯入紀錄，僅供追溯，執行時不需要
        west_north/                ← 依路線分類，資料夾名稱即 line_id
            TAIPEI.next.ogg
            TAIPEI.arrive.ogg
            KEELUNG.terminus.ogg
            BADU.next.keelung.ogg
            BADU.next.hualien.ogg
        mountain/
            TAICHUNG.next.ogg
        common/                    ← 不屬於任何車站的廣播
            DOOR.open.ogg

檔名規則為 ``<車站代碼>.<種類>[.<版本>].<副檔名>``。

為什麼用「掃描資料夾」而不是「讀清單檔」
----------------------------------------

使用者的需求是**隨時可以更新**：廣播會改版、新站會通車，而且暫時沒有
廣播的車站不可以讓程式出錯。因此索引一律由實際存在的檔案建立：

- 丟一個新檔進去就會被索引，不需要同步維護任何清單。
- 檔案不存在只是「查不到」，回傳 ``None``，呼叫端照樣播文字。
- 檔名看不懂就跳過並記進 :attr:`BroadcastLibrary.warnings`，
  不會中斷載入。

因此本模組的任何一個函式都**不會**因為資料缺漏或損壞而拋出例外。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "ANNOUNCEMENT_DIRNAME",
    "AUDIO_EXTENSIONS",
    "COMMON_LINE_ID",
    "BroadcastClip",
    "BroadcastLibrary",
    "ClipKind",
    "VariantRules",
    "default_announcement_dir",
]

#: ``data`` 目錄底下存放廣播的資料夾。
ANNOUNCEMENT_DIRNAME = "audio/announcements"

#: 會被索引的副檔名。使用者可以換成任何一種，播放後端支援與否是另一回事。
AUDIO_EXTENSIONS = (".ogg", ".wav", ".mp3", ".m4a", ".flac", ".opus")

#: 不屬於任何路線的廣播（車門聲等）所在的資料夾。
COMMON_LINE_ID = "common"

#: 方向版本規則的檔名。
VARIANTS_FILENAME = "variants.json"

ClipKind = Literal["next", "arrive", "terminus"]
"""廣播種類。

``next``
    「下一站」廣播。列車自車站啟動之後播放，內容是下一個**停靠站**。
``arrive``
    「到站」廣播。到達停靠站之前播放。
``terminus``
    「終點站」廣播。到達終點站之前播放，取代該站的 ``arrive``。
"""

#: 目前有意義的種類。其他種類仍會被索引，只是不會有人查詢。
KNOWN_KINDS: tuple[str, ...] = ("next", "arrive", "terminus")


def default_announcement_dir(data_dir: Path) -> Path:
    """``data`` 目錄對應的廣播資料夾。"""
    return Path(data_dir) / ANNOUNCEMENT_DIRNAME


@dataclass(frozen=True)
class BroadcastClip:
    """一個已索引的廣播音檔。"""

    path: Path
    station_id: str
    kind: str
    line_id: str
    variant: str = ""

    @property
    def key(self) -> str:
        """索引鍵，與檔名（去掉副檔名與路線資料夾）相同。"""
        if self.variant:
            return f"{self.station_id}.{self.kind}.{self.variant}"
        return f"{self.station_id}.{self.kind}"


@dataclass(frozen=True)
class VariantRules:
    """方向版本的挑選規則。

    八堵、七堵這類分歧站的「下一站」廣播有兩個版本（往基隆／往花蓮），
    差別在於提醒旅客要不要在本站換車，因此**取決於本班列車開往哪裡**，
    不是取決於車站本身。規則放在資料檔而不是程式碼裡，日後多一個分歧站
    只要改 JSON。

    規則格式::

        {"BADU": [{"variant": "keelung", "when_service_calls_at": ["KEELUNG"]},
                  {"variant": "hualien", "when_service_calls_at": ["HUALIEN"]}]}

    依序比對，第一個「本班次會停靠其中任一站」的版本勝出。
    """

    rules: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = field(default_factory=dict)

    def select(self, station_id: str, called_station_ids: Iterable[str]) -> str | None:
        """挑選版本；沒有規則或沒有一條符合時回傳 ``None``。"""
        options = self.rules.get(station_id)
        if not options:
            return None
        called = set(called_station_ids)
        for variant, deciders in options:
            if called.intersection(deciders):
                return variant
        return None

    @classmethod
    def from_dict(cls, raw: Any) -> VariantRules:
        """由 JSON 建立規則。格式不對的項目直接忽略，不拋例外。"""
        rules: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {}
        if not isinstance(raw, dict):
            return cls()
        for station_id, options in (raw.get("rules") or {}).items():
            if not isinstance(options, list):
                continue
            parsed: list[tuple[str, tuple[str, ...]]] = []
            for option in options:
                if not isinstance(option, dict):
                    continue
                variant = str(option.get("variant", "")).strip().lower()
                deciders = option.get("when_service_calls_at") or ()
                if not variant or not isinstance(deciders, list):
                    continue
                parsed.append((variant, tuple(str(d) for d in deciders)))
            if parsed:
                rules[str(station_id)] = tuple(parsed)
        return cls(rules=rules)

    @classmethod
    def load(cls, path: Path) -> VariantRules:
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            # 規則檔不存在或壞掉都只代表「沒有方向版本」，不是致命錯誤。
            return cls()


def parse_clip_name(name: str) -> tuple[str, str, str] | None:
    """把檔名（不含副檔名）拆成 ``(車站代碼, 種類, 版本)``。

    看不懂的名稱回傳 ``None``，由呼叫端記錄警告後跳過。
    """
    parts = [p for p in name.split(".") if p]
    if len(parts) < 2:
        return None
    station_id = parts[0].strip().upper()
    kind = parts[1].strip().lower()
    variant = parts[2].strip().lower() if len(parts) > 2 else ""
    if not station_id or not kind:
        return None
    return station_id, kind, variant


@dataclass
class BroadcastLibrary:
    """廣播音檔索引。

    Attributes:
        root: 索引來源資料夾。不存在時索引為空，這是正常狀態。
        warnings: 掃描時看不懂或無法讀取的項目。僅供診斷，不影響執行。
    """

    root: Path | None = None
    clips: dict[str, BroadcastClip] = field(default_factory=dict)
    variants: VariantRules = field(default_factory=VariantRules)
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # 建立
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, root: str | Path | None) -> BroadcastLibrary:
        """掃描 *root* 建立索引。任何錯誤都只會變成警告，不會拋出例外。"""
        library = cls(root=Path(root) if root is not None else None)
        library.reload()
        return library

    @classmethod
    def empty(cls) -> BroadcastLibrary:
        """沒有任何廣播的索引。查詢一律回傳 ``None``。"""
        return cls()

    def reload(self) -> BroadcastLibrary:
        """重新掃描資料夾。

        廣播會改版、新站會通車，因此索引必須可以在不重新安裝的情況下更新：
        把新檔案放進資料夾再呼叫本方法（或重開一次工作階段）即可生效。
        """
        self.clips = {}
        self.warnings = []
        self.variants = VariantRules()

        root = self.root
        if root is None:
            return self
        if not root.is_dir():
            # 還沒匯入任何廣播是完全合法的狀態（§20.1：音效不是必要資訊）。
            self.warnings.append(f"找不到廣播資料夾：{root}")
            return self

        self.variants = VariantRules.load(root / VARIANTS_FILENAME)

        try:
            line_dirs = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError as exc:  # pragma: no cover - 權限或磁碟問題
            self.warnings.append(f"無法讀取廣播資料夾 {root}：{exc}")
            return self

        for line_dir in line_dirs:
            self._index_line(line_dir)
        return self

    def _index_line(self, line_dir: Path) -> None:
        line_id = line_dir.name
        try:
            entries = sorted(line_dir.iterdir())
        except OSError as exc:  # pragma: no cover - 權限或磁碟問題
            self.warnings.append(f"無法讀取 {line_dir}：{exc}")
            return

        for path in entries:
            if not path.is_file():
                continue
            if path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            parsed = parse_clip_name(path.stem)
            if parsed is None:
                self.warnings.append(f"看不懂的廣播檔名，已略過：{path.name}")
                continue
            station_id, kind, variant = parsed
            clip = BroadcastClip(
                path=path,
                station_id=station_id,
                kind=kind,
                line_id=line_id,
                variant=variant,
            )
            # 同一則廣播出現在多個路線資料夾（例如竹南同時屬於縱貫線與山線）
            # 時保留先出現的那一份；查詢時本來就會先找指定路線，
            # 因此這裡的順序只影響「兩邊都沒指定路線」的退路。
            self.clips.setdefault(f"{line_id}/{clip.key}", clip)

    # ------------------------------------------------------------------
    # 查詢
    # ------------------------------------------------------------------
    def find(
        self,
        station_id: str,
        kind: str,
        *,
        line_id: str | None = None,
        variant: str | None = None,
    ) -> BroadcastClip | None:
        """找出一則廣播；沒有就回傳 ``None``。

        查詢順序：指定路線 → :data:`COMMON_LINE_ID` → 其餘路線（依名稱）。
        同一個車站的廣播內容不會因為列車走哪條線而不同，因此跨路線退路是
        安全的；竹南這種同時屬於兩條線的車站只要有一份檔案就夠。

        Args:
            variant: 指定方向版本。指定的版本不存在時會退回沒有版本的檔案，
                避免因為多了一個版本反而整個播不出來。
        """
        station_id = station_id.strip().upper()
        kind = kind.strip().lower()
        if not station_id or not kind:
            return None

        keys = [f"{station_id}.{kind}.{variant.strip().lower()}"] if variant else []
        keys.append(f"{station_id}.{kind}")

        for key in keys:
            for line in self._line_search_order(line_id):
                clip = self.clips.get(f"{line}/{key}")
                if clip is not None:
                    return clip
        return None

    def find_station_announcement(
        self,
        station_id: str,
        kind: str,
        *,
        line_id: str | None = None,
        called_station_ids: Sequence[str] = (),
    ) -> BroadcastClip | None:
        """查詢車站廣播，並自動套用方向版本規則。"""
        variant = self.variants.select(station_id.strip().upper(), called_station_ids)
        return self.find(station_id, kind, line_id=line_id, variant=variant)

    def has(self, station_id: str, kind: str) -> bool:
        """該車站有沒有這一種廣播，**含方向版本**。

        與 :meth:`find` 不同：``find`` 拿不到適用的版本時寧可不播（播錯方向
        比不播更糟），但「這一站到底有沒有廣播」這個問題不該因為只有分歧站
        版本就回答「沒有」。診斷用途一律使用本方法。
        """
        station_id = station_id.strip().upper()
        kind = kind.strip().lower()
        return any(
            clip.station_id == station_id and clip.kind == kind
            for clip in self.clips.values()
        )

    def has_any(self, station_id: str) -> bool:
        """該車站有沒有**任何**一則廣播。

        用來回答「這一站到底錄了沒」。不能只看某一種：臺鐵每站都有「下一站」
        與「到站」，捷運台北的五條高運量線根本沒有「下一站」廣播，照那一種
        去數會把整條線判成「沒有廣播」。
        """
        station_id = station_id.strip().upper()
        return any(clip.station_id == station_id for clip in self.clips.values())

    def station_ids(self) -> set[str]:
        """索引中出現過的車站代碼。"""
        return {clip.station_id for clip in self.clips.values()}

    def _line_search_order(self, line_id: str | None) -> list[str]:
        lines: list[str] = []
        if line_id:
            lines.append(line_id)
        if COMMON_LINE_ID not in lines:
            lines.append(COMMON_LINE_ID)
        for line in sorted({clip.line_id for clip in self.clips.values()}):
            if line not in lines:
                lines.append(line)
        return lines

    # ------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.clips)

    def __bool__(self) -> bool:
        return bool(self.clips)
