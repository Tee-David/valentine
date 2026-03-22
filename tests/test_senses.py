import pytest
from valentine.core.senses import EnvironmentScanner, SystemInfo


class TestEnvironmentScanner:
    @pytest.mark.asyncio
    async def test_scan_system(self):
        scanner = EnvironmentScanner()
        info = await scanner._scan_system()
        assert isinstance(info, SystemInfo)
        assert info.cpu_count > 0
        assert info.python_version != ""

    def test_scan_tools(self):
        scanner = EnvironmentScanner()
        tools = scanner._scan_tools()
        assert isinstance(tools, dict)
        assert "python3" in tools
        assert tools["python3"] is True

    @pytest.mark.asyncio
    async def test_quick_scan_returns_string(self):
        scanner = EnvironmentScanner()
        result = await scanner.quick_scan()
        assert isinstance(result, str)
        assert "System:" in result
        assert "Python:" in result
