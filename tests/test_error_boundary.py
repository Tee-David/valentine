import pytest
from valentine.security import sanitise_output


class TestErrorSanitization:
    def test_raw_url_stripped(self):
        """Internal API URLs should not appear in user-facing text."""
        text = "Connection failed: https://api.groq.com/openai/v1/chat/completions returned 429"
        result = sanitise_output(text)
        assert "api.groq.com" not in result

    def test_traceback_stripped(self):
        """Python tracebacks should be replaced with a friendly message."""
        text = 'Traceback (most recent call last):\n  File "foo.py", line 42\nKeyError: "x"'
        result = sanitise_output(text)
        assert "Traceback" not in result

    def test_normal_text_unchanged(self):
        """Normal response text should pass through unchanged."""
        text = "The weather in London is 18°C and partly cloudy."
        result = sanitise_output(text)
        assert result == text

    def test_multiple_urls_stripped(self):
        text = "Failed to reach https://api.cerebras.ai/v1 and https://api.sambanova.ai/v1"
        result = sanitise_output(text)
        assert "cerebras" not in result
        assert "sambanova" not in result

    def test_empty_string(self):
        result = sanitise_output("")
        assert result == ""

    def test_none_input(self):
        result = sanitise_output(None)
        assert result is None or result == ""
