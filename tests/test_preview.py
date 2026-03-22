# tests/test_preview.py
import os
import pytest
from unittest.mock import patch, MagicMock

from valentine.core.preview import (
    _detect_server_command,
    _find_cloudflared,
    PreviewSession,
    stop_preview,
)


class TestDetectServerCommand:
    def test_node_project_with_dev_script(self, tmp_path):
        pkg = tmp_path / "package.json"
        pkg.write_text('{"scripts": {"dev": "next dev"}}')
        cmd, port = _detect_server_command(str(tmp_path))
        assert cmd == "npm run dev"
        assert port == 3000

    def test_node_project_with_start_script(self, tmp_path):
        pkg = tmp_path / "package.json"
        pkg.write_text('{"scripts": {"start": "node server.js"}}')
        cmd, port = _detect_server_command(str(tmp_path))
        assert cmd == "npm start"
        assert port == 3000

    def test_django_project(self, tmp_path):
        (tmp_path / "manage.py").write_text("#!/usr/bin/env python")
        cmd, port = _detect_server_command(str(tmp_path))
        assert "manage.py runserver" in cmd
        assert port == 8000

    def test_flask_project(self, tmp_path):
        (tmp_path / "app.py").write_text("from flask import Flask")
        cmd, port = _detect_server_command(str(tmp_path))
        assert cmd == "python app.py"
        assert port == 5000

    def test_static_html_project(self, tmp_path):
        (tmp_path / "index.html").write_text("<html></html>")
        cmd, port = _detect_server_command(str(tmp_path))
        assert "http.server" in cmd
        assert port == 8080

    def test_empty_project_fallback(self, tmp_path):
        cmd, port = _detect_server_command(str(tmp_path))
        assert "http.server" in cmd


class TestFindCloudflared:
    @patch("shutil.which", return_value="/usr/local/bin/cloudflared")
    def test_found(self, mock_which):
        assert _find_cloudflared() == "/usr/local/bin/cloudflared"

    @patch("shutil.which", return_value=None)
    def test_not_found(self, mock_which):
        assert _find_cloudflared() is None


class TestPreviewSession:
    def test_stop_kills_processes(self):
        server = MagicMock()
        server.poll.return_value = None
        server.pid = 12345
        tunnel = MagicMock()
        tunnel.poll.return_value = None
        tunnel.pid = 12346

        session = PreviewSession(
            project_dir="/tmp/test",
            port=3000,
            url="https://test.trycloudflare.com",
            server_proc=server,
            tunnel_proc=tunnel,
        )

        with patch("os.killpg"):
            session.stop()
            server.wait.assert_called_once()
            tunnel.wait.assert_called_once()


class TestStopPreview:
    @pytest.mark.asyncio
    async def test_stop_no_active(self):
        result = await stop_preview()
        assert "No active" in result
