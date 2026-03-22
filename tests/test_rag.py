import pytest
from valentine.core.rag import CodebaseRAG, CodeChunk


class TestCodeChunking:
    def test_chunk_file(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("line1\n" * 200)
        rag = CodebaseRAG()
        chunks = rag._chunk_file(str(test_file))
        assert len(chunks) >= 2
        assert all(isinstance(c, CodeChunk) for c in chunks)
        assert chunks[0].start_line == 1
        assert chunks[0].language == "py"

    def test_scan_directory_skips_pycache(self, tmp_path):
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "foo.py").write_text("cached")
        (tmp_path / "real.py").write_text("real code")
        rag = CodebaseRAG()
        files = rag._scan_directory(str(tmp_path))
        assert len(files) == 1
        assert "real.py" in files[0]

    def test_scan_filters_extensions(self, tmp_path):
        (tmp_path / "code.py").write_text("python")
        (tmp_path / "binary.exe").write_text("not code")
        (tmp_path / "data.json").write_text("{}")
        rag = CodebaseRAG()
        files = rag._scan_directory(str(tmp_path))
        assert len(files) == 2  # .py and .json
        extensions = {f.split(".")[-1] for f in files}
        assert "exe" not in extensions

    def test_empty_file_chunks(self, tmp_path):
        test_file = tmp_path / "empty.py"
        test_file.write_text("")
        rag = CodebaseRAG()
        chunks = rag._chunk_file(str(test_file))
        # Empty or single empty chunk is fine
        assert isinstance(chunks, list)
