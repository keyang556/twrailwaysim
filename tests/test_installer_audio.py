"""Windows release metadata and frozen-audio smoke-test guards."""

from __future__ import annotations

import tomllib
from pathlib import Path


class TestInstallerAudioRequirements:
    _ROOT = Path(__file__).resolve().parents[1]

    def test_installer_extra_includes_the_audio_backend_requirement(self) -> None:
        project = tomllib.loads((self._ROOT / "pyproject.toml").read_text("utf-8"))
        extras = project["project"]["optional-dependencies"]
        audio_pygame = next(
            requirement
            for requirement in extras["audio"]
            if requirement.startswith("pygame")
        )

        assert audio_pygame in extras["installer"]

    def test_frozen_build_requires_and_smoke_tests_pygame(self) -> None:
        build_script = (self._ROOT / "scripts" / "build-installer.ps1").read_text(
            encoding="utf-8"
        )
        spec = (self._ROOT / "packaging" / "twrailwaysim.spec").read_text(
            encoding="utf-8"
        )

        assert "import pygame" in build_script
        assert "pygame or SDL module" in build_script
        assert "knownOptionalPygameWarnings" in build_script
        assert '"--check-audio"' in build_script
        assert "SDL_AUDIODRIVER" in build_script
        assert '"pygame"' in spec
