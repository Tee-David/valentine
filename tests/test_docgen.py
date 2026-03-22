import pytest
import json
from valentine.core.docgen import DocumentGenerator


class TestDocumentGenerator:
    @pytest.mark.asyncio
    async def test_generate_csv(self, tmp_path):
        gen = DocumentGenerator(output_dir=str(tmp_path))
        doc = await gen.generate_csv(
            data=[["Alice", 30], ["Bob", 25]],
            headers=["Name", "Age"],
            file_name="people",
        )
        assert doc.file_type == "csv"
        assert doc.file_path.endswith(".csv")
        content = open(doc.file_path).read()
        assert "Alice" in content
        assert "Name,Age" in content

    @pytest.mark.asyncio
    async def test_generate_json(self, tmp_path):
        gen = DocumentGenerator(output_dir=str(tmp_path))
        doc = await gen.generate_json({"key": "value"}, file_name="test")
        assert doc.file_type == "json"
        data = json.load(open(doc.file_path))
        assert data["key"] == "value"

    @pytest.mark.asyncio
    async def test_generate_text(self, tmp_path):
        gen = DocumentGenerator(output_dir=str(tmp_path))
        doc = await gen.generate_text("Hello world", file_name="greeting")
        assert doc.file_type == "txt"
        assert open(doc.file_path).read() == "Hello world"

    @pytest.mark.asyncio
    async def test_generate_html(self, tmp_path):
        gen = DocumentGenerator(output_dir=str(tmp_path))
        doc = await gen.generate_html("<h1>Test</h1>", file_name="page")
        assert doc.file_type == "html"
        assert "<h1>Test</h1>" in open(doc.file_path).read()
