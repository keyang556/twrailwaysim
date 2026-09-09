"""廣播音檔索引與匯入測試（規格 §20）。

重點在使用者提出的兩個要求：

1. **隨時可以更新**：把檔案放進資料夾就生效，不需要同步維護任何清單。
2. **暫時沒有廣播的不能讓程式 error**：缺資料夾、缺檔案、檔名看不懂，
   一律只是「查不到」，不是例外。
"""

from __future__ import annotations

import json
from pathlib import Path

from railway_sim.audio.importer import SourceMap, apply_plan, plan_import
from railway_sim.audio.library import BroadcastLibrary, VariantRules, parse_clip_name
from railway_sim.data_loader import GameData


def _write(path: Path, content: bytes = b"ogg") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class TestClipNames:
    """檔名就是索引：``<車站代碼>.<種類>[.<版本>]``。"""

    def test_station_and_kind(self) -> None:
        assert parse_clip_name("TAIPEI.next") == ("TAIPEI", "next", "")

    def test_variant(self) -> None:
        assert parse_clip_name("BADU.next.keelung") == ("BADU", "next", "keelung")

    def test_case_is_normalised(self) -> None:
        assert parse_clip_name("taipei.NEXT") == ("TAIPEI", "next", "")

    def test_unreadable_name_is_rejected_without_raising(self) -> None:
        assert parse_clip_name("132n") is None
        assert parse_clip_name("") is None


class TestMissingData:
    """沒有廣播是正常狀態，不是錯誤。"""

    def test_missing_directory_gives_an_empty_library(self, tmp_path: Path) -> None:
        library = BroadcastLibrary.load(tmp_path / "不存在")
        assert len(library) == 0
        assert library.warnings  # 有說明，但不是例外
        assert library.find("TAIPEI", "next") is None

    def test_no_root_at_all(self) -> None:
        assert BroadcastLibrary.empty().find("TAIPEI", "next") is None

    def test_unreadable_filenames_are_skipped_not_fatal(self, tmp_path: Path) -> None:
        _write(tmp_path / "west_north" / "TAIPEI.next.ogg")
        _write(tmp_path / "west_north" / "132n.ogg")
        _write(tmp_path / "west_north" / "readme.txt")

        library = BroadcastLibrary.load(tmp_path)
        assert library.find("TAIPEI", "next") is not None
        assert len(library) == 1
        assert any("132n" in w for w in library.warnings)

    def test_station_without_a_clip_just_returns_none(self, tmp_path: Path) -> None:
        _write(tmp_path / "west_north" / "TAIPEI.next.ogg")
        library = BroadcastLibrary.load(tmp_path)
        assert library.find("新開的站", "next") is None
        assert library.find("TAIPEI", "terminus") is None


class TestUpdating:
    """廣播會改版、新站會通車，重新掃描就要生效。"""

    def test_new_file_is_picked_up_on_reload(self, tmp_path: Path) -> None:
        library = BroadcastLibrary.load(tmp_path)
        assert library.find("FENGMING", "next") is None

        _write(tmp_path / "west_north" / "FENGMING.next.ogg")
        library.reload()
        assert library.find("FENGMING", "next") is not None

    def test_removed_file_disappears_on_reload(self, tmp_path: Path) -> None:
        clip = _write(tmp_path / "west_north" / "TAIPEI.next.ogg")
        library = BroadcastLibrary.load(tmp_path)
        assert library.find("TAIPEI", "next") is not None

        clip.unlink()
        library.reload()
        assert library.find("TAIPEI", "next") is None


class TestLookupOrder:
    """同一個車站的廣播內容不因列車走哪條線而不同，因此可以跨路線退路。"""

    def test_other_line_is_used_when_the_requested_one_has_nothing(
        self, tmp_path: Path
    ) -> None:
        """竹南同屬縱貫線與山線，只放一份檔案就該兩條線都找得到。"""
        _write(tmp_path / "west_north" / "ZHUNAN.next.ogg")
        library = BroadcastLibrary.load(tmp_path)
        assert library.find("ZHUNAN", "next", line_id="mountain") is not None

    def test_requested_line_wins_when_both_exist(self, tmp_path: Path) -> None:
        _write(tmp_path / "west_north" / "ZHUNAN.next.ogg")
        _write(tmp_path / "mountain" / "ZHUNAN.next.ogg")
        library = BroadcastLibrary.load(tmp_path)
        clip = library.find("ZHUNAN", "next", line_id="mountain")
        assert clip is not None
        assert clip.line_id == "mountain"


class TestVariants:
    """分歧站的方向版本由本班次的停靠表決定。"""

    def _library(self, tmp_path: Path) -> BroadcastLibrary:
        _write(tmp_path / "west_north" / "BADU.next.keelung.ogg")
        _write(tmp_path / "west_north" / "BADU.next.hualien.ogg")
        (tmp_path / "variants.json").write_text(
            json.dumps(
                {
                    "rules": {
                        "BADU": [
                            {"variant": "keelung", "when_service_calls_at": ["KEELUNG"]},
                            {"variant": "hualien", "when_service_calls_at": ["HUALIEN"]},
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        return BroadcastLibrary.load(tmp_path)

    def test_variant_follows_the_service(self, tmp_path: Path) -> None:
        library = self._library(tmp_path)
        clip = library.find_station_announcement(
            "BADU", "next", called_station_ids=("BADU", "KEELUNG")
        )
        assert clip is not None
        assert clip.variant == "keelung"

    def test_other_direction_gets_the_other_version(self, tmp_path: Path) -> None:
        library = self._library(tmp_path)
        clip = library.find_station_announcement(
            "BADU", "next", called_station_ids=("BADU", "HUALIEN")
        )
        assert clip is not None
        assert clip.variant == "hualien"

    def test_undecidable_plays_nothing_rather_than_the_wrong_direction(
        self, tmp_path: Path
    ) -> None:
        """播錯方向比不播更糟：文字播報照常送出，只是沒有音檔。"""
        library = self._library(tmp_path)
        assert (
            library.find_station_announcement(
                "BADU", "next", called_station_ids=("BADU", "TAIPEI")
            )
            is None
        )

    def test_has_still_reports_coverage_for_variant_only_stations(
        self, tmp_path: Path
    ) -> None:
        assert self._library(tmp_path).has("BADU", "next")

    def test_broken_rules_file_is_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "variants.json").write_text("{ 不是 JSON", encoding="utf-8")
        _write(tmp_path / "west_north" / "TAIPEI.next.ogg")
        library = BroadcastLibrary.load(tmp_path)
        assert library.find("TAIPEI", "next") is not None

    def test_rules_ignore_malformed_entries(self) -> None:
        rules = VariantRules.from_dict(
            {"rules": {"BADU": [{"variant": ""}, "壞掉的項目", {"variant": "ok",
                                                          "when_service_calls_at": ["X"]}]}}
        )
        assert rules.select("BADU", ["X"]) == "ok"


class TestImporter:
    """把整理前的檔名翻成程式看得懂的形式。"""

    def _source_map(self, game_data: GameData) -> SourceMap:
        return SourceMap.load(game_data.data_dir / "audio" / "source_map.json")

    def test_corrects_the_station_name_typos_in_the_source(
        self, game_data: GameData
    ) -> None:
        source_map = self._source_map(game_data)
        # 來源檔名的錯字：福州實為浮洲、經舞蹈站實為精武到站。
        assert source_map.parse_stem("14福州到站") == ("浮洲", "arrive", "")
        assert source_map.parse_stem("15經舞蹈站") == ("精武", "arrive", "")
        assert source_map.parse_stem("33三性橋") == ("三姓橋", "next", "")
        assert source_map.parse_stem("10麗玲到站") == ("栗林", "arrive", "")

    def test_reads_kind_and_direction_from_the_name(self, game_data: GameData) -> None:
        source_map = self._source_map(game_data)
        assert source_map.parse_stem("1基隆終點") == ("基隆", "terminus", "")
        assert source_map.parse_stem("3八堵(往基隆)") == ("八堵", "next", "keelung")

    def test_numeric_only_names_are_reported_not_fatal(
        self, game_data: GameData, tmp_path: Path
    ) -> None:
        """『4縱貫南』目前檔名還是純編號，尚未整理完。"""
        source = tmp_path / "1縱貫北"
        _write(source / "11台北.ogg")
        _write(source / "132n.ogg")

        plan = plan_import(
            [source], tmp_path / "data", game_data.station_names(),
            self._source_map(game_data),
        )
        assert [c.station_id for c in plan.copies] == ["TAIPEI"]
        assert any("132n" in item for item in plan.unresolved)

    def test_unknown_source_folder_is_skipped(
        self, game_data: GameData, tmp_path: Path
    ) -> None:
        source = tmp_path / "沒有對應路線的資料夾"
        _write(source / "11台北.ogg")
        plan = plan_import(
            [source], tmp_path / "data", game_data.station_names(),
            self._source_map(game_data),
        )
        assert plan.copies == []
        assert plan.skipped_dirs == ["沒有對應路線的資料夾"]

    def test_import_is_repeatable_and_only_rewrites_changed_files(
        self, game_data: GameData, tmp_path: Path
    ) -> None:
        source = tmp_path / "1縱貫北"
        clip = _write(source / "11台北.ogg", b"first version")
        data_dir = tmp_path / "data"
        source_map = self._source_map(game_data)
        names = game_data.station_names()

        first = apply_plan(plan_import([source], data_dir, names, source_map))
        assert first == {"added": 1, "updated": 0, "unchanged": 0}

        second = apply_plan(plan_import([source], data_dir, names, source_map))
        assert second == {"added": 0, "updated": 0, "unchanged": 1}

        clip.write_bytes(b"revised version")
        third = apply_plan(plan_import([source], data_dir, names, source_map))
        assert third == {"added": 0, "updated": 1, "unchanged": 0}

        imported = data_dir / "audio" / "announcements" / "west_north" / "TAIPEI.next.ogg"
        assert imported.read_bytes() == b"revised version"


class TestShippedData:
    """實際匯入的資料（縱貫線北段與山線）。"""

    def test_every_stopping_station_on_both_lines_has_a_next_announcement(
        self, game_data: GameData
    ) -> None:
        for line_id in ("west_north", "mountain"):
            missing = [
                station.name_zh_tw
                for station in game_data.stations.values()
                if line_id in station.line_ids
                and not game_data.broadcasts.has(station.id, "next")
            ]
            assert missing == [], f"{line_id} 缺少廣播：{missing}"

    def test_index_has_no_warnings(self, game_data: GameData) -> None:
        assert game_data.broadcasts.warnings == []

    def test_terminus_clips_are_indexed(self, game_data: GameData) -> None:
        for station_id in ("KEELUNG", "QIDU", "SHULIN", "TAICHUNG", "FENGYUAN", "HOULI"):
            assert game_data.broadcasts.has(station_id, "terminus")


class TestShippedDoorClips:
    """開關門聲（``common/DOOR.<open|close>.<車輛型式>``）。

    版本字串必須是**小寫的車輛型式代碼**，因為查詢時直接把 ``spec.id`` 當版本
    傳進去。這裡真正要防的是打錯字：檔名寫成 ``emu5000`` 不會有任何錯誤訊息，
    只會安安靜靜地不出聲，光聽是分不出「這型沒錄」與「檔名打錯」的。
    """

    def _door_clips(self, game_data: GameData) -> list:
        return [
            clip
            for clip in game_data.broadcasts.clips.values()
            if clip.station_id == "DOOR" and clip.kind in ("open", "close")
        ]

    def test_every_variant_is_a_real_train_type(self, game_data: GameData) -> None:
        known = {type_id.lower() for type_id in game_data.train_types}
        unknown = sorted(
            {
                clip.variant
                for clip in self._door_clips(game_data)
                if clip.variant and clip.variant not in known
            }
        )
        assert unknown == [], f"開關門聲的版本不是任何車輛型式：{unknown}"

    def test_the_recorded_types_have_both_directions(
        self, game_data: GameData
    ) -> None:
        """已經錄到的車型，開門與關門要成對，缺一邊等於少一半的回饋。

        EMU3000 目前只有關門聲，來源就只給了那一則，因此列為已知缺口而不是
        測試失敗；其餘車型都必須成對。
        """
        by_variant: dict[str, set[str]] = {}
        for clip in self._door_clips(game_data):
            by_variant.setdefault(clip.variant, set()).add(clip.kind)
        incomplete = sorted(
            variant
            for variant, kinds in by_variant.items()
            if kinds != {"open", "close"} and variant != "emu3000"
        )
        assert incomplete == [], f"這些車型只錄到單邊：{incomplete}"

    def test_lookup_uses_the_train_type_id_as_the_variant(
        self, game_data: GameData
    ) -> None:
        """用 ``spec.id`` 查得到，才表示播放時真的會找到這些檔案。"""
        for type_id in ("EMU500", "EMU700", "EMU800", "EMU900", "PP", "TEMU2000"):
            for kind in ("open", "close"):
                clip = game_data.broadcasts.find(
                    "DOOR", kind, line_id="common", variant=type_id
                )
                assert clip is not None, f"{type_id} 找不到{kind}門聲"
                assert clip.variant == type_id.lower()

    def test_a_type_without_a_recording_is_silent_not_an_error(
        self, game_data: GameData
    ) -> None:
        """沒錄到的車型查不到就是查不到，不可以拿別型的聲音頂替。"""
        assert (
            game_data.broadcasts.find(
                "DOOR", "open", line_id="common", variant="EMU600"
            )
            is None
        )
