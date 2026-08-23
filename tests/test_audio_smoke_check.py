"""The non-interactive packaged-audio diagnostic is testable without hardware."""

from __future__ import annotations

from pathlib import Path

import pytest

from railway_sim import app
from railway_sim.audio.player import PlayerCreation


class FakePlayer:
    def __init__(self, *, backend_name: str = "pygame", can_load: bool = True) -> None:
        self.backend_name = backend_name
        self._can_load = can_load
        self.closed = False

    def can_load(self, path: Path) -> bool:
        return self._can_load

    def close(self) -> None:
        self.closed = True


def test_check_audio_requires_pygame_and_loads_bundled_clip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clip = tmp_path / app._AUDIO_SMOKE_CLIP
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"ogg")
    player = FakePlayer()
    monkeypatch.setattr(
        app,
        "create_player_with_diagnostics",
        lambda: PlayerCreation(player, ()),
    )
    monkeypatch.setattr(app, "default_data_dir", lambda: tmp_path)

    assert app._check_audio(None) == 0
    assert player.closed is True
    assert "pygame audio backend loaded bundled clip" in capsys.readouterr().out


def test_check_audio_rejects_ffplay_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    player = FakePlayer(backend_name="ffplay")
    monkeypatch.setattr(
        app,
        "create_player_with_diagnostics",
        lambda: PlayerCreation(player, ("using ffplay fallback: ffplay",)),
    )

    assert app._check_audio(None) == 2
    assert player.closed is True
    assert "expected the bundled pygame backend" in capsys.readouterr().err
