#!/bin/bash
# DESC: Text-to-speech using edge-tts with multiple voices
# CATEGORY: audio
# AUTHOR: Valentine
# VERSION: 1.0
# USAGE: audio-tts.sh "<text>" [voice] [output_path]

set -euo pipefail

TEXT="${1:-}"
VOICE="${2:-en-US-AriaNeural}"
OUTPUT="${3:-/tmp/valentine/workspace/tts_output.mp3}"

if [ -z "$TEXT" ]; then
    echo "Usage: audio-tts.sh \"<text>\" [voice] [output_path]"
    echo ""
    echo "Available voices:"
    echo "  en-US-AriaNeural     (Female, warm)"
    echo "  en-US-GuyNeural      (Male, friendly)"
    echo "  en-US-JennyNeural    (Female, professional)"
    echo "  en-GB-SoniaNeural    (British Female)"
    echo "  en-AU-NatashaNeural  (Australian Female)"
    echo ""
    echo "List all: edge-tts --list-voices"
    exit 1
fi

mkdir -p "$(dirname "$OUTPUT")"

if ! command -v edge-tts &>/dev/null; then
    echo "ERROR: edge-tts not installed. Run: pip install edge-tts"
    exit 1
fi

edge-tts --voice "$VOICE" --text "$TEXT" --write-media "$OUTPUT" 2>/dev/null
echo "$OUTPUT"
