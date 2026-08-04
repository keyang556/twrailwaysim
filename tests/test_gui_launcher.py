from __future__ import annotations

from railway_sim import gui_launcher


def test_gui_launcher_defaults_to_wx(monkeypatch) -> None:
    received: list[list[str]] = []
    monkeypatch.setattr(gui_launcher, "app_main", lambda args: received.append(args) or 0)

    assert gui_launcher.main([]) == 0
    assert received == [["--ui", "wx"]]


def test_gui_launcher_preserves_diagnostic_arguments(monkeypatch) -> None:
    received: list[list[str]] = []
    monkeypatch.setattr(gui_launcher, "app_main", lambda args: received.append(args) or 0)

    assert gui_launcher.main(["--check-gui"]) == 0
    assert received == [["--check-gui"]]
