#!/bin/bash
# DESC: Transcribe audio files using Groq Whisper API
# CATEGORY: audio
# AUTHOR: Valentine
# VERSION: 1.0
# USAGE: audio-transcribe.sh <audio_file>

set -euo pipefail

AUDIO_FILE="${1:-}"

if [ -z "$AUDIO_FILE" ]; then
    echo "Usage: audio-transcribe.sh <audio_file>"
    echo "Supported formats: mp3, wav, ogg, m4a, webm, flac"
    exit 1
fi

if [ ! -f "$AUDIO_FILE" ]; then
    echo "ERROR: File not found: $AUDIO_FILE"
    exit 1
fi

# Convert OGG to WAV if needed
if [[ "$AUDIO_FILE" == *.ogg ]]; then
    WAV_FILE="${AUDIO_FILE%.ogg}.wav"
    ffmpeg -y -i "$AUDIO_FILE" -ar 16000 -ac 1 -c:a pcm_s16le "$WAV_FILE" 2>/dev/null
    AUDIO_FILE="$WAV_FILE"
fi

GROQ_API_KEY="${GROQ_API_KEY:-}"
if [ -z "$GROQ_API_KEY" ]; then
    echo "ERROR: GROQ_API_KEY not set"
    exit 1
fi

curl -s "https://api.groq.com/openai/v1/audio/transcriptions" \
    -H "Authorization: Bearer $GROQ_API_KEY" \
    -F "file=@$AUDIO_FILE" \
    -F "model=whisper-large-v3-turbo" \
    -F "response_format=text"
