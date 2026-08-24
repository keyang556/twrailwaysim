"""播放後端測試（規格 §20.1）。

播放能力是選用的：這台機器有沒有後端、檔案在不在、播放成不成功，都不可
以變成例外。這些測試刻意**不驗證有沒有聲音**——那需要音效裝置，也不是
遊戲功能的必要條件。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from railway_sim.audio import player as player_module
from railway_sim.audio.player import AudioPlayer, create_player


class SilentBackend:
    """不出聲的後端，只記下被要求播了什麼。"""

    name = "silent"

    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped = 0
        self.closed = 0

    def start(self, path: Path) -> bool:
        self.started.append(path.name)
        return True

    def is_busy(self) -> bool:
        return False

    def stop(self) -> None:
        self.stopped += 1

    def close(self) -> None:
        self.closed += 1


class SlowStartBackend:
    """``start()`` 故意卡住一段時間，讓測試能準確把 ``stop()`` 對準它
    正在執行的那個時間點，驗證兩者是不是真的互斥。"""

    name = "slow"

    def __init__(self, start_delay_s: float) -> None:
        self.started: list[str] = []
        self.stop_calls = 0
        self._start_delay_s = start_delay_s
        self.start_call_began = threading.Event()

    def start(self, path: Path) -> bool:
        self.start_call_began.set()
        time.sleep(self._start_delay_s)
        self.started.append(path.name)
        return True

    def is_busy(self) -> bool:
        return False

    def stop(self) -> None:
        self.stop_calls += 1

    def close(self) -> None:
        pass


class TestCreatePlayer:
    def test_never_raises(self) -> None:
        """沒有任何後端時回傳 None，不是例外。"""
        player = create_player()
        try:
            assert player is None or isinstance(player, AudioPlayer)
        finally:
            if player is not None:
                player.close()

    def test_uses_pygame_when_it_initializes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = SilentBackend()
        backend.name = "pygame"
        monkeypatch.setattr(player_module, "PygameBackend", lambda: backend)

        creation = player_module.create_player_with_diagnostics()
        assert creation.player is not None
        try:
            assert creation.player.backend_name == "pygame"
            assert creation.diagnostics == ()
        finally:
            creation.player.close()

    def test_falls_back_to_ffplay_with_pygame_diagnostic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unavailable_pygame() -> None:
            raise RuntimeError("mixer initialization failed")

        monkeypatch.setattr(player_module, "PygameBackend", unavailable_pygame)
        monkeypatch.setattr(
            player_module, "_find_ffplay", lambda: "C:/tools/ffplay.exe"
        )

        creation = player_module.create_player_with_diagnostics()
        assert creation.player is not None
        try:
            assert creation.player.backend_name == "ffplay"
            assert "pygame unavailable (RuntimeError): mixer initialization failed" in (
                creation.diagnostics
            )
            assert "using ffplay fallback: C:/tools/ffplay.exe" in creation.diagnostics
        finally:
            creation.player.close()

    def test_no_backends_returns_diagnostics(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unavailable_pygame() -> None:
            raise ImportError("No module named pygame")

        monkeypatch.setattr(player_module, "PygameBackend", unavailable_pygame)
        monkeypatch.setattr(player_module, "_find_ffplay", lambda: None)

        creation = player_module.create_player_with_diagnostics()
        assert creation.player is None
        assert "pygame unavailable (ImportError): No module named pygame" in (
            creation.diagnostics
        )
        assert "ffplay not found on PATH" in creation.diagnostics


class TestQueueing:
    def _player(self) -> tuple[AudioPlayer, SilentBackend]:
        backend = SilentBackend()
        return AudioPlayer(backend), backend  # type: ignore[arg-type]

    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        player, backend = self._player()
        try:
            assert player.play(tmp_path / "沒有這個檔.ogg") is False
            assert backend.started == []
        finally:
            player.close()

    def test_existing_file_is_queued(self, tmp_path: Path) -> None:
        clip = tmp_path / "TAIPEI.next.ogg"
        clip.write_bytes(b"clip")
        player, backend = self._player()
        try:
            assert player.play(clip) is True
            player._queue.join()
            assert backend.started == ["TAIPEI.next.ogg"]
        finally:
            player.close()

    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        player, backend = self._player()
        player.close()
        player.close()
        assert backend.closed == 1
        assert player.available is False

    def test_play_after_close_is_refused(self, tmp_path: Path) -> None:
        clip = tmp_path / "TAIPEI.next.ogg"
        clip.write_bytes(b"clip")
        player, _ = self._player()
        player.close()
        assert player.play(clip) is False

    def test_queue_does_not_grow_without_bound(self, tmp_path: Path) -> None:
        """廣播很長，堆積太多只會嚴重落後於列車位置，不如丟掉。"""
        clip = tmp_path / "TAIPEI.next.ogg"
        clip.write_bytes(b"clip")

        class BusyBackend(SilentBackend):
            def is_busy(self) -> bool:
                return True

        backend = BusyBackend()
        player = AudioPlayer(backend)  # type: ignore[arg-type]
        try:
            accepted = [player.play(clip) for _ in range(20)]
            assert accepted.count(True) <= 6
            assert accepted[-1] is False
        finally:
            player.close()


class TestLooping:
    """「請勿上車」要在車門開著的整段時間裡持續播放。"""

    def _player(self) -> tuple[AudioPlayer, SilentBackend]:
        backend = SilentBackend()
        return AudioPlayer(backend), backend  # type: ignore[arg-type]

    def test_a_looping_clip_replays_itself(self, tmp_path: Path) -> None:
        clip = tmp_path / "NOTICE.do_not_board.ogg"
        clip.write_bytes(b"clip")
        player, backend = self._player()
        try:
            assert player.play(clip, loop=True) is True
            assert player.looping is True
            # 播完會自己再排一次，因此會播不只一遍。
            deadline = time.monotonic() + 2.0
            while len(backend.started) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(backend.started) >= 3
            assert set(backend.started) == {"NOTICE.do_not_board.ogg"}
        finally:
            player.close()

    def test_stop_waits_out_an_in_flight_start_then_stops_it(
        self, tmp_path: Path
    ) -> None:
        """核對世代跟呼叫 backend.start() 是不是真的鎖在同一段：如果
        stop() 跟 backend.start() 撞在一起，stop() 必須等 start() 做完
        才能返回——不然核對通過的項目還是可能在 stop() 宣告完成之後才
        真正開始播（見 stop() 的說明）。"""
        clip = tmp_path / "NOTICE.do_not_board.ogg"
        clip.write_bytes(b"clip")
        backend = SlowStartBackend(start_delay_s=0.2)
        player = AudioPlayer(backend)  # type: ignore[arg-type]
        try:
            player.play(clip, loop=True)
            assert backend.start_call_began.wait(timeout=1.0)
            before = time.monotonic()
            player.stop()
            elapsed = time.monotonic() - before
            # 空檔存在的話 stop() 幾乎會立刻返回；真的鎖在同一段才會等。
            assert elapsed >= 0.15
            assert backend.started == ["NOTICE.do_not_board.ogg"]
            player._queue.join()
            assert player.looping is False
            time.sleep(0.05)
            assert backend.started == ["NOTICE.do_not_board.ogg"]
        finally:
            player.close()

    def test_stop_cancels_the_loop(self, tmp_path: Path) -> None:
        """關門動作一開始就要停，不能等這一輪播完。"""
        clip = tmp_path / "NOTICE.do_not_board.ogg"
        clip.write_bytes(b"clip")
        player, backend = self._player()
        try:
            player.play(clip, loop=True)
            player.stop()
            assert player.looping is False
            player._queue.join()
            played = len(backend.started)
            time.sleep(0.2)
            assert len(backend.started) == played
        finally:
            player.close()

    def test_a_stale_item_pulled_before_stop_is_discarded(
        self, tmp_path: Path
    ) -> None:
        """stop() 只能清掉還在佇列裡的項目；一旦背景執行緒已經用 get() 把
        循環的下一輪取出、只是還沒開始播，清空佇列完全碰不到它。用世代
        編號補這個洞：手動模擬「已取出、世代卻是舊的」這個先天無法穩定
        用真實時間點重現的情境，確認背景執行緒真的會丟棄它，不會播出來。
        """
        clip = tmp_path / "NOTICE.do_not_board.ogg"
        clip.write_bytes(b"clip")
        player, backend = self._player()
        try:
            player.play(clip, loop=True)
            player.stop()
            assert backend.started == []
            # 模擬 stop() 呼叫當下，那一輪早就被 get() 取出、只是還沒開始
            # 播的項目——它帶著 stop() 之前的舊世代編號。
            player._queue.put((0, clip))
            player._queue.join()
            assert backend.started == []
        finally:
            player.close()

    def test_a_plain_clip_is_played_once(self, tmp_path: Path) -> None:
        clip = tmp_path / "TAIPEI.next.ogg"
        clip.write_bytes(b"clip")
        player, backend = self._player()
        try:
            player.play(clip)
            player._queue.join()
            time.sleep(0.2)
            assert backend.started == ["TAIPEI.next.ogg"]
            assert player.looping is False
        finally:
            player.close()
