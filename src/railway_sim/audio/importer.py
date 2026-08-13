"""把整理前的臺鐵廣播音檔匯入 ``data/audio/announcements``。

來源資料的檔名是給人看的，不是給程式看的：開頭有站序編號、站名偶有錯字
（「福州」實為浮洲、「經舞蹈站」實為精武到站）、分歧站還用括號標方向。
本模組負責把它翻譯成程式可以直接索引的形式：

``1縱貫北/14福州到站.ogg`` → ``west_north/FUZHOU.arrive.ogg``

翻譯規則全部放在 ``data/audio/source_map.json``，程式不寫死任何站名對應。
新增一條線、修一個錯字、改一個資料夾名稱都只要改那個 JSON，不用改程式。

對不上的檔案不會讓匯入失敗，只會列在報告的「未對應」裡（例如「4縱貫南」
目前檔名還是純編號，尚未整理完）。這與執行時的行為一致：**沒有廣播是
正常狀態，不是錯誤**。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from railway_sim.audio.library import AUDIO_EXTENSIONS, default_announcement_dir

__all__ = [
    "SOURCE_MAP_FILENAME",
    "ImportPlan",
    "PlannedCopy",
    "SourceMap",
    "apply_plan",
    "plan_import",
]

#: 對應規則的檔名，放在 ``data/audio/`` 底下。
SOURCE_MAP_FILENAME = "audio/source_map.json"

#: 匯入紀錄的檔名，寫在廣播資料夾裡，僅供追溯。
MANIFEST_FILENAME = "manifest.json"

#: 站序編號前綴，例如 ``14福州到站`` 的 ``14``。
_LEADING_NUMBER = re.compile(r"^\d+")

#: 方向標記，例如 ``八堵(往基隆)``。全形括號也接受。
_VARIANT_SUFFIX = re.compile(r"[（(]([^）)]+)[）)]\s*$")


def _normalise_station_name(name: str) -> str:
    """把站名正規化成可比對的形式。

    來源檔名寫「台北」「台中」，官方資料寫「臺北」「臺中」；兩者是同一個
    字的異體，統一成「臺」再比對，就不必為每一站建立別名。
    """
    return name.strip().replace("台", "臺")


@dataclass(frozen=True)
class SourceMap:
    """``source_map.json`` 的內容。

    Attributes:
        clip_map: ``<資料夾>/<檔名>`` → ``<車站代碼>.<種類>[.<版本>]`` 的**逐檔**
            對照，可加上 ``<線別>/`` 前綴把某一則放到別條線的資料夾。

            為什麼需要逐檔對照：臺鐵的來源檔名寫得出站名與種類（「14福州到站」），
            靠站名比對就夠；捷運的來源檔名只有各線自己的播放序號加站名
            （「19-1奇岩」），序號與種類的關係是每一條線各自約定的，而且同一個
            站名在不同線是不同車站。這種資料沒有規則可循，只能逐檔寫明——寫在
            資料檔裡，看得見也改得動，程式仍然不寫死任何站名。
    """

    line_ids: dict[str, str] = field(default_factory=dict)
    kind_suffixes: dict[str, str] = field(default_factory=dict)
    variant_names: dict[str, str] = field(default_factory=dict)
    station_aliases: dict[str, str] = field(default_factory=dict)
    filename_aliases: dict[str, str] = field(default_factory=dict)
    clip_map: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SourceMap:
        def table(key: str) -> dict[str, str]:
            value = raw.get(key) or {}
            if not isinstance(value, dict):
                return {}
            return {str(k): str(v) for k, v in value.items()}

        return cls(
            line_ids=table("line_ids"),
            kind_suffixes=table("kind_suffixes"),
            variant_names=table("variant_names"),
            station_aliases=table("station_aliases"),
            filename_aliases=table("filename_aliases"),
            clip_map=table("clip_map"),
        )

    @classmethod
    def load(cls, path: str | Path) -> SourceMap:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ------------------------------------------------------------------
    def line_id_for(self, source_dir: Path) -> str | None:
        """來源資料夾對應的路線代碼。"""
        return self.line_ids.get(source_dir.name)

    def clip_for(self, source_dir: Path, stem: str) -> tuple[str, str, str, str] | None:
        """逐檔對照的結果 ``(路線, 車站代碼, 種類, 版本)``；沒登記時回傳 ``None``。"""
        target = self.clip_map.get(f"{source_dir.name}/{stem}")
        if not target:
            return None
        line_id = ""
        if "/" in target:
            line_id, target = target.split("/", 1)
        parts = [p for p in target.split(".") if p]
        if len(parts) < 2:
            return None
        station_id, kind = parts[0], parts[1]
        variant = parts[2] if len(parts) > 2 else ""
        return line_id, station_id, kind, variant

    def parse_stem(self, stem: str) -> tuple[str, str, str] | None:
        """把來源檔名（不含副檔名）拆成 ``(站名, 種類, 版本)``。

        對不上時回傳 ``None``。
        """
        text = _LEADING_NUMBER.sub("", stem).strip()
        text = self.filename_aliases.get(text, text)
        if not text:
            return None

        variant = ""
        match = _VARIANT_SUFFIX.search(text)
        if match is not None:
            variant = self.variant_names.get(match.group(1).strip(), "")
            if not variant:
                return None
            text = text[: match.start()].strip()

        kind = "next"
        for suffix, name in self.kind_suffixes.items():
            if text.endswith(suffix) and len(text) > len(suffix):
                kind = name
                text = text[: -len(suffix)].strip()
                break

        if not text:
            return None
        return self.station_aliases.get(text, text), kind, variant


@dataclass(frozen=True)
class PlannedCopy:
    """一個「來源檔 → 目標檔」的搬運計畫。"""

    source: Path
    target: Path
    station_id: str
    station_name: str
    kind: str
    line_id: str
    variant: str = ""

    @property
    def renamed(self) -> bool:
        """站名是否經過修正（來源檔名有錯字）。"""
        return _normalise_station_name(self.source.stem) != self.source.stem


@dataclass
class ImportPlan:
    """一次匯入的完整計畫。"""

    announcement_dir: Path
    copies: list[PlannedCopy] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    """對不上的來源檔，格式為 ``資料夾/檔名：原因``。"""

    skipped_dirs: list[str] = field(default_factory=list)
    """沒有對應路線代碼、整個略過的來源資料夾。"""

    def report_lines(self) -> list[str]:
        """給人看的匯入摘要。"""
        lines = [f"目標資料夾：{self.announcement_dir}"]
        by_line: dict[str, int] = {}
        for copy in self.copies:
            by_line[copy.line_id] = by_line.get(copy.line_id, 0) + 1
        for line_id, count in sorted(by_line.items()):
            lines.append(f"  {line_id}：{count} 個音檔")
        lines.append(f"可匯入：{len(self.copies)} 個")
        if self.skipped_dirs:
            lines.append(f"略過的資料夾（source_map.json 沒有對應路線）：{len(self.skipped_dirs)}")
            lines.extend(f"  {name}" for name in self.skipped_dirs)
        if self.unresolved:
            lines.append(f"未對應：{len(self.unresolved)} 個（不影響其他檔案）")
            lines.extend(f"  {item}" for item in self.unresolved)
        return lines


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plan_import(
    sources: list[str | Path],
    data_dir: str | Path,
    station_names: dict[str, str],
    source_map: SourceMap,
) -> ImportPlan:
    """規劃匯入，但不動任何檔案。

    Args:
        sources: 來源資料夾。每個資料夾必須在 ``source_map.json`` 的
            ``line_ids`` 裡有對應的路線代碼，否則整個資料夾會被略過。
        data_dir: 遊戲資料目錄。
        station_names: ``車站代碼 → 中文站名``，用來把站名翻回代碼。
        source_map: 對應規則。
    """
    announcement_dir = default_announcement_dir(Path(data_dir))
    plan = ImportPlan(announcement_dir=announcement_dir)

    by_name: dict[str, str] = {
        _normalise_station_name(name): station_id
        for station_id, name in station_names.items()
    }

    for raw_source in sources:
        source_dir = Path(raw_source)
        line_id = source_map.line_id_for(source_dir)
        if line_id is None:
            plan.skipped_dirs.append(source_dir.name)
            continue
        if not source_dir.is_dir():
            plan.unresolved.append(f"{source_dir}：找不到來源資料夾")
            continue

        for path in sorted(source_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue

            # 逐檔對照優先：登記過的檔案不再猜站名，也不必符合任何檔名規則。
            mapped = source_map.clip_for(source_dir, path.stem)
            if mapped is not None:
                target_line, station_id, kind, variant = mapped
            else:
                parsed = source_map.parse_stem(path.stem)
                if parsed is None:
                    plan.unresolved.append(f"{source_dir.name}/{path.name}：看不懂的檔名")
                    continue

                station_name, kind, variant = parsed
                resolved = by_name.get(_normalise_station_name(station_name))
                if resolved is None:
                    plan.unresolved.append(
                        f"{source_dir.name}/{path.name}：查無此站「{station_name}」"
                    )
                    continue
                station_id, target_line = resolved, ""

            target_line = target_line or line_id
            stem = f"{station_id}.{kind}.{variant}" if variant else f"{station_id}.{kind}"
            plan.copies.append(
                PlannedCopy(
                    source=path,
                    target=announcement_dir / target_line / f"{stem}{path.suffix.lower()}",
                    station_id=station_id,
                    # 宣導這類不屬於任何車站的廣播查不到站名，用代碼本身即可。
                    station_name=station_names.get(station_id, station_id),
                    kind=kind,
                    line_id=target_line,
                    variant=variant,
                )
            )

    return plan


def apply_plan(plan: ImportPlan, *, write_manifest: bool = True) -> dict[str, int]:
    """執行匯入計畫，回傳 ``{"added": n, "updated": n, "unchanged": n}``。

    已存在且內容相同的檔案不會重寫，因此重跑匯入是安全且便宜的操作——
    廣播改版時只要把新檔放回來源資料夾再跑一次即可。
    """
    counts = {"added": 0, "updated": 0, "unchanged": 0}
    entries: list[dict[str, str]] = []

    for copy in plan.copies:
        copy.target.parent.mkdir(parents=True, exist_ok=True)
        digest = _sha256(copy.source)
        if not copy.target.exists():
            counts["added"] += 1
        elif _sha256(copy.target) == digest:
            counts["unchanged"] += 1
            entries.append(_manifest_entry(copy, digest))
            continue
        else:
            counts["updated"] += 1
        shutil.copy2(copy.source, copy.target)
        entries.append(_manifest_entry(copy, digest))

    if write_manifest:
        _write_manifest(plan, entries)
    return counts


def _read_manifest(announcement_dir: Path) -> dict[str, Any]:
    """讀取既有的匯入紀錄。讀不到就當作空的——紀錄本來就不是必要檔案。"""
    try:
        raw = json.loads((announcement_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _manifest_entry(copy: PlannedCopy, digest: str) -> dict[str, str]:
    return {
        "file": f"{copy.line_id}/{copy.target.name}",
        "station_id": copy.station_id,
        "station_name": copy.station_name,
        "kind": copy.kind,
        "variant": copy.variant,
        "source_name": copy.source.name,
        "source_dir": copy.source.parent.name,
        "sha256": digest,
    }


def _write_manifest(plan: ImportPlan, entries: list[dict[str, str]]) -> Path:
    """寫出匯入紀錄。

    執行時**不需要**這個檔案（索引一律由實際檔案掃描而來），它的用途是讓人
    對照「這個音檔是從哪個來源檔改名來的」，以及記錄哪些來源檔沒對上。

    紀錄會與既有內容**合併**，不是整份覆寫：廣播資料夾是共用的，一次匯入
    通常只處理其中幾條線（臺鐵一次一區、捷運一次一線）。直接覆寫會讓上一次
    匯入的紀錄憑空消失，資料夾裡明明有的音檔卻查不到出處。合併的鍵是目標
    檔名，因此重跑同一批來源只會更新自己那幾筆。
    """
    existing = _read_manifest(plan.announcement_dir)
    merged = {entry["file"]: entry for entry in existing.get("entries", ())}
    merged.update({entry["file"]: entry for entry in entries})

    # 未對應的來源檔以「來源資料夾」為單位取代：本次處理過的資料夾用新結果，
    # 沒碰到的資料夾保留上一次的紀錄。
    handled = {item.split("/", 1)[0] for item in plan.unresolved}
    handled.update(copy.source.parent.name for copy in plan.copies)
    unresolved = [
        item
        for item in existing.get("unresolved", ())
        if item.split("/", 1)[0] not in handled
    ]
    unresolved.extend(plan.unresolved)

    manifest = {
        "meta": {
            "description": (
                "由 railway_sim.audio.importer 產生的匯入紀錄，僅供追溯。"
                "遊戲執行時是掃描資料夾建立索引，不讀本檔，"
                "因此手動增刪音檔不需要同步修改這裡。"
            ),
            "count": len(merged),
        },
        "entries": sorted(merged.values(), key=lambda e: e["file"]),
        "unresolved": unresolved,
    }
    plan.announcement_dir.mkdir(parents=True, exist_ok=True)
    path = plan.announcement_dir / MANIFEST_FILENAME
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path
