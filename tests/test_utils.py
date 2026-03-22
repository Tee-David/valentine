from valentine.utils import safe_parse_json


class TestSafeParseJson:
    def test_valid_json(self):
        assert safe_parse_json('{"a": 1}') == {"a": 1}

    def test_markdown_wrapped(self):
        assert safe_parse_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_text_before_json(self):
        result = safe_parse_json('Sure, here you go: {"tool": "weather"}')
        assert result == {"tool": "weather"}

    def test_invalid_json_returns_none(self):
        assert safe_parse_json("This is not JSON at all") is None

    def test_empty_string(self):
        assert safe_parse_json("") is None

    def test_nested_json(self):
        result = safe_parse_json('{"actions": [{"type": "shell", "cmd": "ls"}]}')
        assert result["actions"][0]["type"] == "shell"

    def test_json_array(self):
        result = safe_parse_json('[{"type": "respond"}]')
        assert isinstance(result, list)
