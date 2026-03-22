import pytest
from valentine.core.evolution import SelfEvolver


class TestSelfEvolver:
    def test_suggest_missing_python_module(self):
        evolver = SelfEvolver()
        suggestion = evolver.suggest_install("ModuleNotFoundError: No module named 'openpyxl'")
        assert suggestion == "openpyxl"

    def test_suggest_missing_command(self):
        evolver = SelfEvolver()
        suggestion = evolver.suggest_install("ffmpeg: command not found")
        assert suggestion == "ffmpeg"

    def test_suggest_unknown_returns_none(self):
        evolver = SelfEvolver()
        result = evolver.suggest_install("Something random happened")
        assert result is None

    def test_is_available_python3(self):
        evolver = SelfEvolver()
        assert evolver.is_available("python3") is True

    def test_is_available_missing(self):
        evolver = SelfEvolver()
        assert evolver.is_available("definitely_not_a_real_command_xyz") is False

    def test_import_to_tool_mapping(self):
        evolver = SelfEvolver()
        suggestion = evolver.suggest_install("No module named 'PIL'")
        assert suggestion == "pillow"

    def test_suggest_docx(self):
        evolver = SelfEvolver()
        suggestion = evolver.suggest_install("No module named 'docx'")
        assert suggestion == "python-docx"
