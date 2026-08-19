"""捷運時刻表匯入（``railway_sim.dataset.mrt_timetable``）。

守的是三件事：

1. **不虛構**。來源只公布發車時刻，終點站的到達時刻不推估；沒有來源的路線
   （文湖線、環狀線、三鶯線、機場捷運）保持空白而不是填一份看起來合理的
   假時刻（規格 §2.3、§27）。
2. **串得對**。來源的序號是「本站的第幾班」而不是車次，只能靠時間串接；
   串不起來時要留白，不能硬串出一份不存在的時刻表。
3. **歸屬誠實**。來源的一組營運路線裡可能同時有好幾種營運模式，這種情形
   標成 ``derived_from_timetable`` 而不是 ``official``。
"""

from __future__ import annotations

import json

import pytest

from railway_sim.dataset.mrt_timetable import (
    MrtTimetableError,
    RouteTimetable,
    TimetableImportResult,
    build_match,
    build_mrt_timetables,
    chain_trip,
    match_service,
    read_timetable_file,
)
from railway_sim.timetable.service import Schedule

# 一份最小的「北市平台版」時刻表：兩條營運路線、四個車站。
# 刻意保留原檔的怪癖——Big5 編碼、欄名拼作 DestinationStaionID、
# 發車時刻包在 {序號,,,HH:MM,} 裡。
_HEADER = (
    "SEQNO,RouteID,StationID,StationName,Direction,DestinationStaionID,"
    "DestinationStationName,DepartureTimes,ServiceDays,UpdateTime,EffectiveDate"
)


def _row(seq, route, station, direction, destination, index, time, days="平日"):
    flags = "1,1,1,1,1,0,0,0" if days == "平日" else "0,0,0,0,0,1,1,1"
    return (
        f"{seq},{route},{station},{station}站,{direction},{destination},終點站,"
        f'"{{{index},,,{time},}}","{{\'{days}\',{flags}}}",20251110,20251110'
    )


def write_csv(path, rows, name="測試線平日時刻表(1141110啟用)北市平台版.csv"):
    target = path / name
    text = "\n".join([_HEADER, *rows]) + "\n"
    target.write_bytes(text.encode("big5"))
    return target


@pytest.fixture
def simple_csv(tmp_path):
    """A→B→C→D 的一條線，兩班車，班距十分鐘。"""
    rows = []
    seq = 1
    for index, start in enumerate((360, 370), start=1):
        for offset, station in enumerate(("A01", "A02", "A03")):
            hh, mm = divmod(start + offset * 2, 60)
            rows.append(
                _row(seq, "T-1", station, "0", "A04", index, f"{hh:02d}:{mm:02d}")
            )
            seq += 1
    return write_csv(tmp_path, rows)


class TestReading:
    def test_big5_and_the_odd_field_names_are_handled(self, simple_csv) -> None:
        file = read_timetable_file(simple_csv)
        assert file.service_days == "平日"
        assert file.effective_date == "20251110"
        assert [r.route_id for r in file.routes] == ["T-1"]

    def test_departures_are_grouped_by_station(self, simple_csv) -> None:
        route = read_timetable_file(simple_csv).routes[0]
        assert route.departures["A01"] == (360, 370)
        assert route.departures["A03"] == (364, 374)

    def test_destination_has_no_departures(self, simple_csv) -> None:
        """來源只公布發車時刻，終點站只有到達。"""
        route = read_timetable_file(simple_csv).routes[0]
        assert "A04" not in route.departures
        assert route.station_ids == {"A01", "A02", "A03", "A04"}

    def test_after_midnight_belongs_to_the_same_service_day(self, tmp_path) -> None:
        """00:20 是當天的末班車，不是隔天的首班車。"""
        rows = [
            _row(1, "T-1", "A01", "0", "A02", 1, "06:00"),
            _row(2, "T-1", "A01", "0", "A02", 2, "00:20"),
        ]
        route = read_timetable_file(write_csv(tmp_path, rows)).routes[0]
        assert route.departures["A01"] == (360, 24 * 60 + 20)
        assert route.last_departure("A01") == 24 * 60 + 20

    def test_missing_columns_are_rejected(self, tmp_path) -> None:
        path = tmp_path / "壞掉.csv"
        path.write_bytes("SEQNO,RouteID\n1,T-1\n".encode("big5"))
        with pytest.raises(MrtTimetableError, match="缺少欄位"):
            read_timetable_file(path)


class TestChaining:
    """序號是「本站的第幾班」而不是車次，只能靠時間串。"""

    def _route(self, departures) -> RouteTimetable:
        return RouteTimetable(
            route_id="T-1", direction="0", destination="A04", departures=departures
        )

    def test_follows_the_earliest_departure_after_the_previous_station(self) -> None:
        route = self._route(
            {"A01": (360, 370), "A02": (362, 372), "A03": (364, 374)}
        )
        assert chain_trip(route, ["A01", "A02", "A03", "A04"], 370) == {
            "A01": 370,
            "A02": 372,
            "A03": 374,
        }

    def test_terminus_is_left_out(self) -> None:
        """終點站沒有發車時刻，補一個等於虛構（§2.3）。"""
        route = self._route({"A01": (360,), "A02": (362,), "A03": (364,)})
        trip = chain_trip(route, ["A01", "A02", "A03", "A04"], 360)
        assert trip is not None
        assert "A04" not in trip

    def test_gives_up_when_a_station_has_no_service(self) -> None:
        """硬串下去會得到一份看起來合理、實際上不存在的時刻表。"""
        route = self._route({"A01": (360,), "A02": (362,), "A03": (600,)})
        assert chain_trip(route, ["A01", "A02", "A03", "A04"], 360) is None

    def test_unknown_start_time_is_rejected(self) -> None:
        route = self._route({"A01": (360,), "A02": (362,)})
        assert chain_trip(route, ["A01", "A02"], 999) is None


class TestMatching:
    def _file(self, simple_csv):
        return read_timetable_file(simple_csv)

    def test_exact_station_set_is_official(self, simple_csv) -> None:
        matched = match_service(["A01", "A02", "A03", "A04"], [self._file(simple_csv)])
        assert matched is not None
        assert matched[2] is True

    def test_a_subset_is_only_derived(self, simple_csv) -> None:
        """來源那一組裡還有別的營運模式時，時刻是真的，歸屬是推得的。"""
        matched = match_service(["A02", "A03", "A04"], [self._file(simple_csv)])
        assert matched is not None
        assert matched[2] is False

    def test_a_different_destination_never_matches(self, simple_csv) -> None:
        """只比車站集合會把去程與回程混為一談。"""
        assert match_service(["A04", "A03", "A02"], [self._file(simple_csv)]) is None

    def test_build_match_reports_the_span_of_the_day(self, simple_csv) -> None:
        match = build_match(
            "T1001", ["A01", "A02", "A03", "A04"], [self._file(simple_csv)]
        )
        assert match is not None
        assert match.first_departure == "06:00"
        assert match.last_departure == "06:10"
        assert match.departures_per_day == 2
        assert match.run_time_min == 4
        assert match.verification_status == "official"

    def test_two_station_shuttles_have_no_run_time(self, tmp_path) -> None:
        """只有起站有時刻，「全程幾分」這個數字沒有意義。"""
        rows = [_row(1, "T-3", "B01", "0", "B02", 1, "06:00")]
        file = read_timetable_file(write_csv(tmp_path, rows))
        match = build_match("T1051", ["B01", "B02"], [file])
        assert match is not None
        assert match.run_time_min is None


class TestWeekdayPreference:
    def test_weekday_wins_over_holiday(self, tmp_path) -> None:
        """遊戲要的是最貼近日常的那一份。"""
        weekday = write_csv(
            tmp_path,
            [
                _row(1, "T-1", "A01", "0", "A02", 1, "06:00", days="平日"),
            ],
            name="平日.csv",
        )
        holiday = write_csv(
            tmp_path,
            [
                _row(1, "T-1", "A01", "0", "A02", 1, "07:00", days="假日"),
            ],
            name="假日.csv",
        )
        files = [read_timetable_file(holiday), read_timetable_file(weekday)]
        match = build_match("T1001", ["A01", "A02"], files)
        assert match is not None
        assert match.file.service_days == "平日"
        assert match.first_departure == "06:00"


class TestShippedData:
    """實際匯入到 ``data/mrt`` 的結果。"""

    def test_lines_with_a_published_timetable_have_real_times(self, mrt_data) -> None:
        for number in ("BL1001", "R1001", "G1001", "O1001"):
            service = mrt_data.service(number)
            assert service.departure_times
            assert service.schedule is not None

    def test_lines_without_a_source_stay_empty(self, mrt_data) -> None:
        """文湖線、環狀線、三鶯線與機場捷運沒有逐班時刻的公開來源。

        補一份推估的只會讓人以為那是真的（§2.3），因此空白才是正確的。
        """
        for number in ("BR1001", "Y1001", "LB1001", "A1001", "A1003"):
            service = mrt_data.service(number)
            assert service.departure_times == {}
            assert service.schedule is None

    def test_no_arrival_times_are_invented(self, mrt_data) -> None:
        for service in mrt_data.services.values():
            assert service.arrival_times == {}

    def test_departure_times_only_name_stations_on_the_route(self, mrt_data) -> None:
        for service in mrt_data.services.values():
            assert set(service.departure_times) <= set(service.stop_station_ids)

    def test_times_run_forward_along_the_route(self, mrt_data) -> None:
        """串錯的話時刻會倒退，那是最容易看出來的症狀。"""
        for service in mrt_data.services.values():
            times = [
                service.departure_times[sid]
                for sid in service.stop_station_ids
                if sid in service.departure_times
            ]
            minutes = [int(t[:2]) * 60 + int(t[3:]) for t in times]
            # 首班車不會跨午夜，因此單純遞增即可。
            assert minutes == sorted(minutes), service.train_number

    def test_terminus_has_no_departure(self, mrt_data) -> None:
        for service in mrt_data.services.values():
            if not service.departure_times:
                continue
            assert service.stop_station_ids[-1] not in service.departure_times

    def test_journey_times_are_plausible(self, mrt_data) -> None:
        """板南線全程實際約四十幾分鐘；差太多就是串錯了。"""
        schedule = mrt_data.service("BL1001").schedule
        assert schedule is not None
        assert 35 <= schedule.run_time_min <= 55

    def test_provenance_names_the_source_file(self, mrt_data) -> None:
        schedule = mrt_data.service("BL1001").schedule
        assert schedule is not None
        assert "臺北市政府資料平台" in schedule.source
        assert schedule.service_days == "平日"
        assert schedule.effective_date


class TestRebuildKeepsTimes:
    """由條目重建資料集時不可以把匯入好的時刻清掉。"""

    def test_existing_times_are_carried_over(self, tmp_path) -> None:
        from railway_sim.dataset.mrt import _existing_departure_times

        payload = {
            "services": [
                {
                    "train_number": "BL1001",
                    "stop_station_ids": ["BL01", "BL02"],
                    "departure_times": {"BL01": "06:00"},
                    "arrival_times": {},
                    "schedule": {"service_days": "平日"},
                },
                {
                    "train_number": "A1001",
                    "stop_station_ids": ["A1", "A2"],
                    "departure_times": {},
                },
            ]
        }
        (tmp_path / "timetables.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        carried = _existing_departure_times(tmp_path)

        assert ("BL1001", ("BL01", "BL02")) in carried
        assert carried[("BL1001", ("BL01", "BL02"))]["departure_times"] == {
            "BL01": "06:00"
        }
        # 沒有時刻的班次不必留，留了也只是空字典。
        assert ("A1001", ("A1", "A2")) not in carried

    def test_changed_stops_drop_the_old_times(self, tmp_path) -> None:
        """停靠站變了就不是同一回事，舊時刻對不上新路線。"""
        from railway_sim.dataset.mrt import _existing_departure_times

        payload = {
            "services": [
                {
                    "train_number": "BL1001",
                    "stop_station_ids": ["BL01", "BL02"],
                    "departure_times": {"BL01": "06:00"},
                }
            ]
        }
        (tmp_path / "timetables.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        carried = _existing_departure_times(tmp_path)
        assert ("BL1001", ("BL01", "BL02", "BL03")) not in carried

    def test_missing_file_is_not_an_error(self, tmp_path) -> None:
        from railway_sim.dataset.mrt import _existing_departure_times

        assert _existing_departure_times(tmp_path) == {}

    def test_meta_carries_forward_the_timetable_provenance(self) -> None:
        """時刻留著了，說明時刻來源的欄位不能被條目匯入的通用 meta 蓋掉。

        否則寫出來的檔案會一邊帶著真實時刻、一邊在 meta 裡宣稱「捷運不公布
        逐班時刻」，自相矛盾也遺失了 timetable_sources 這份出處紀錄。
        """
        from railway_sim.dataset.mrt import _timetables_payload

        existing_meta = {
            "description": "捷運營運模式。有公布逐班時刻的路線帶有實際發車時刻。",
            "timetable_policy": "departure_times 取自來源時刻表的當天第一班。",
            "timetable_sources": [{"file": "板南線平日.csv", "service_days": "平日"}],
            "provenance": {
                "source": "維基百科各線條目的列車營運模式章節",
                "generator": "railway_sim.dataset.mrt",
                "timetable_source": "臺北市政府資料平台　捷運各線時刻表（北市平台版）",
                "timetable_generator": "railway_sim.dataset.mrt_timetable",
            },
        }
        services = [
            {
                "train_number": "BL1001",
                "stop_station_ids": ["BL01"],
                "departure_times": {"BL01": "06:00"},
            }
        ]

        meta = _timetables_payload(services, existing_meta)["meta"]

        assert "不公布" not in meta["description"]
        assert meta["timetable_sources"] == existing_meta["timetable_sources"]
        assert meta["provenance"]["timetable_generator"] == "railway_sim.dataset.mrt_timetable"
        # 條目匯入自己的出處說明也要留著，不是整段被時刻表那邊蓋過去。
        assert meta["provenance"]["generator"] == "railway_sim.dataset.mrt"

    def test_meta_stays_generic_when_nothing_was_carried_over(self) -> None:
        """沒有任何班次留著時刻時，不該平白冒出時刻表的來源說明。"""
        from railway_sim.dataset.mrt import _timetables_payload

        existing_meta = {
            "timetable_sources": [{"file": "板南線平日.csv"}],
            "provenance": {"timetable_generator": "railway_sim.dataset.mrt_timetable"},
        }
        services = [
            {"train_number": "BL1001", "stop_station_ids": ["BL01"], "departure_times": {}}
        ]

        meta = _timetables_payload(services, existing_meta)["meta"]

        assert "不公布" in meta["description"]
        assert "timetable_sources" not in meta


class TestBuildingIntoTheDataset:
    """把時刻併進 ``timetables.json`` 的那一步。"""

    def _dataset(self, tmp_path, services):
        data_dir = tmp_path / "mrt"
        data_dir.mkdir()
        (data_dir / "timetables.json").write_text(
            json.dumps({"meta": {}, "services": services}, ensure_ascii=False),
            encoding="utf-8",
        )
        return data_dir

    def _source(self, tmp_path):
        source = tmp_path / "csv"
        source.mkdir()
        rows = []
        seq = 1
        for index, start in enumerate((360, 370), start=1):
            for offset, station in enumerate(("A01", "A02", "A03")):
                hh, mm = divmod(start + offset * 2, 60)
                rows.append(
                    _row(seq, "T-1", station, "0", "A04", index, f"{hh:02d}:{mm:02d}")
                )
                seq += 1
        write_csv(source, rows)
        return source

    def test_matched_services_get_times_and_a_schedule(self, tmp_path) -> None:
        data_dir = self._dataset(
            tmp_path,
            [
                {
                    "train_number": "T1001",
                    "stop_station_ids": ["A01", "A02", "A03", "A04"],
                    "departure_times": {},
                    "arrival_times": {},
                }
            ],
        )
        result = build_mrt_timetables(self._source(tmp_path), data_dir)
        service = result.timetables["services"][0]

        assert service["departure_times"]["A01"] == "06:00"
        assert service["schedule"]["verification_status"] == "official"
        assert result.unmatched == []

    def test_unmatched_services_are_left_alone_and_reported(self, tmp_path) -> None:
        """補一份推估的只會讓人以為那是真的（§2.3）。"""
        data_dir = self._dataset(
            tmp_path,
            [
                {
                    "train_number": "Z9001",
                    "stop_station_ids": ["Z01", "Z02"],
                    "departure_times": {},
                    "arrival_times": {},
                }
            ],
        )
        result = build_mrt_timetables(self._source(tmp_path), data_dir)
        service = result.timetables["services"][0]

        assert service["departure_times"] == {}
        assert "schedule" not in service
        assert result.unmatched == ["Z9001"]
        assert any("Z9001" in w for w in result.warnings)

    def test_meta_no_longer_claims_there_are_no_published_times(
        self, tmp_path
    ) -> None:
        """原本寫著「捷運不公布逐班時刻」，對這幾條線已經不成立了。"""
        data_dir = self._dataset(tmp_path, [])
        result = build_mrt_timetables(self._source(tmp_path), data_dir)
        meta = result.timetables["meta"]

        assert "不公布" not in meta["description"]
        assert meta["timetable_sources"]
        assert meta["provenance"]["timetable_generator"].endswith("mrt_timetable")

    def test_running_twice_gives_the_same_result(self, tmp_path) -> None:
        """重跑匯入不該讓資料漂移。"""
        data_dir = self._dataset(
            tmp_path,
            [
                {
                    "train_number": "T1001",
                    "stop_station_ids": ["A01", "A02", "A03", "A04"],
                    "departure_times": {},
                    "arrival_times": {},
                }
            ],
        )
        source = self._source(tmp_path)
        first = build_mrt_timetables(source, data_dir)
        (data_dir / "timetables.json").write_text(
            json.dumps(first.timetables, ensure_ascii=False), encoding="utf-8"
        )
        second = build_mrt_timetables(source, data_dir)
        assert second.timetables == first.timetables

    def test_a_source_folder_without_csv_files_is_rejected(self, tmp_path) -> None:
        data_dir = self._dataset(tmp_path, [])
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(MrtTimetableError, match="沒有 CSV"):
            build_mrt_timetables(empty, data_dir)

    def test_existing_payload_is_matched_against_instead_of_the_file_on_disk(
        self, tmp_path
    ) -> None:
        """``--source`` 與 ``--dry-run`` 合併執行時，磁碟還是重建前的舊檔。

        這時要比對的是這次重建出、還沒寫入的候選資料——給了 ``existing_payload``
        就不能再去讀磁碟，否則預覽會用錯資料（見
        ``railway_sim.dataset.__main__._run_mrt``）。
        """
        # 磁碟上的舊檔停靠站對不上來源；若被誤讀，這一班就會落在 unmatched。
        data_dir = self._dataset(
            tmp_path,
            [
                {
                    "train_number": "T1001",
                    "stop_station_ids": ["Z01", "Z02"],
                    "departure_times": {},
                    "arrival_times": {},
                }
            ],
        )
        fresh_payload = {
            "meta": {},
            "services": [
                {
                    "train_number": "T1001",
                    "stop_station_ids": ["A01", "A02", "A03", "A04"],
                    "departure_times": {},
                    "arrival_times": {},
                }
            ],
        }

        result = build_mrt_timetables(
            self._source(tmp_path), data_dir, existing_payload=fresh_payload
        )

        assert result.unmatched == []
        assert result.timetables["services"][0]["departure_times"]["A01"] == "06:00"
        assert result.timetables is fresh_payload


class TestCombinedCliDryRun:
    """CLI 合併 ``--source`` 與 ``--timetables`` 且 ``--dry-run`` 時的預覽。

    重建那一步在 dry-run 下不寫入磁碟，補時刻那一步因此不能照舊去讀
    磁碟上的 ``timetables.json``——那是重建前的舊資料，讀了會得到跟真的
    執行不一致的預覽（見 ``railway_sim.dataset.__main__._run_mrt``）。
    """

    def test_the_rebuilt_candidate_is_forwarded_to_the_timetable_step(
        self, tmp_path, monkeypatch
    ) -> None:
        from railway_sim.dataset import __main__ as cli
        from railway_sim.dataset.mrt import MrtBuildResult

        built_timetables = {"meta": {}, "services": [{"train_number": "FRESH"}]}
        seen: list[dict | None] = []

        def fake_build_mrt_dataset(source_dir, out_dir):
            return MrtBuildResult(
                stations={}, routes={}, timetables=built_timetables, report=["建置完成"]
            )

        def fake_build_mrt_timetables(source_dir, out_dir, *, existing_payload=None):
            seen.append(existing_payload)
            return TimetableImportResult(
                timetables=existing_payload if existing_payload is not None else {},
                report=["時刻比對完成"],
            )

        monkeypatch.setattr(cli, "build_mrt_dataset", fake_build_mrt_dataset)
        monkeypatch.setattr(cli, "build_mrt_timetables", fake_build_mrt_timetables)

        code = cli.main(
            [
                "--system",
                "mrt",
                "--source",
                str(tmp_path / "wiki"),
                "--timetables",
                str(tmp_path / "csv"),
                "--out",
                str(tmp_path),
                "--dry-run",
            ]
        )

        # 這裡若沒有先擋掉最後的驗證摘要，main() 會接著去讀 tmp_path 底下
        # 根本不存在的正式資料檔而整個失敗——能跑到這裡就同時證明了
        # dry-run 不會再做那一次多餘的驗證。
        assert code == 0
        assert seen == [built_timetables]

    def test_run_mrt_dataset_only_returns_the_candidate_when_dry_run(
        self, tmp_path, monkeypatch
    ) -> None:
        """非 dry-run 成功時第二個回傳值是 ``None``：那一步已經寫入磁碟了。"""
        from types import SimpleNamespace

        from railway_sim.dataset import __main__ as cli
        from railway_sim.dataset.mrt import MrtBuildResult

        built_timetables = {"meta": {}, "services": []}
        monkeypatch.setattr(
            cli,
            "build_mrt_dataset",
            lambda source_dir, out_dir: MrtBuildResult(
                stations={}, routes={}, timetables=built_timetables, report=[]
            ),
        )

        dry_args = SimpleNamespace(source=str(tmp_path / "wiki"), dry_run=True)
        code, payload = cli._run_mrt_dataset(dry_args, tmp_path)
        assert code == 0
        assert payload is built_timetables

        monkeypatch.setattr(
            cli, "write_mrt_dataset", lambda result, out_dir: [out_dir / "routes.json"]
        )
        write_args = SimpleNamespace(source=str(tmp_path / "wiki"), dry_run=False)
        code, payload = cli._run_mrt_dataset(write_args, tmp_path)
        assert code == 0
        assert payload is None


class TestScheduleModel:
    def test_absent_schedule_is_none_not_an_empty_shell(self) -> None:
        """``None`` 說得出「沒有來源」，空殼子說不出。"""
        from railway_sim.timetable.service import Service

        service = Service.from_dict(
            {"train_number": "X", "train_type": "metro", "route_id": "R"}
        )
        assert service.schedule is None

    def test_schedule_defaults_are_conservative(self) -> None:
        assert Schedule().verification_status == "unknown"
        assert Schedule().run_time_min is None
