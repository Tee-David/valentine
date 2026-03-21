#!/bin/bash
# DESC: CLI utilities — file search, text processing, archives, downloads
# CATEGORY: cli
# AUTHOR: Valentine
# VERSION: 1.0
# USAGE: cli-utils.sh <command> [args]

set -euo pipefail

usage() {
    echo "Valentine CLI Utilities"
    echo ""
    echo "Commands:"
    echo "  search <pattern> [dir]    Search files for a pattern (ripgrep/grep)"
    echo "  find <name> [dir]         Find files by name"
    echo "  count <ext> [dir]         Count files by extension"
    echo "  size [dir]                Show directory sizes"
    echo "  download <url> [output]   Download a file"
    echo "  extract <archive>         Extract an archive (tar/zip/gz)"
    echo "  json-format <file>        Pretty-print a JSON file"
    echo "  csv-preview <file> [n]    Preview first N rows of a CSV"
    echo "  hash <file>               Show file checksums"
    echo "  env-check                 Check required environment variables"
    echo "  ports-in-use              Show ports currently in use"
    echo "  kill-port <port>          Kill process on a port"
}

cmd_search() {
    local pattern="${1:-}"
    local dir="${2:-.}"
    if [ -z "$pattern" ]; then
        echo "Usage: cli-utils.sh search <pattern> [directory]"
        return 1
    fi
    if command -v rg &>/dev/null; then
        rg --color=never -n "$pattern" "$dir" 2>/dev/null | head -50
    else
        grep -rn "$pattern" "$dir" 2>/dev/null | head -50
    fi
}

cmd_find() {
    local name="${1:-}"
    local dir="${2:-.}"
    if [ -z "$name" ]; then
        echo "Usage: cli-utils.sh find <name> [directory]"
        return 1
    fi
    find "$dir" -name "*${name}*" -type f 2>/dev/null | head -30
}

cmd_count() {
    local ext="${1:-}"
    local dir="${2:-.}"
    if [ -z "$ext" ]; then
        echo "File type breakdown in $dir:"
        find "$dir" -type f 2>/dev/null | sed 's/.*\.//' | sort | uniq -c | sort -rn | head -20
    else
        local count
        count=$(find "$dir" -name "*.${ext}" -type f 2>/dev/null | wc -l)
        echo "$count .${ext} files found in $dir"
    fi
}

cmd_size() {
    local dir="${1:-.}"
    du -sh "$dir"/* 2>/dev/null | sort -rh | head -20
}

cmd_download() {
    local url="${1:-}"
    local output="${2:-}"
    if [ -z "$url" ]; then
        echo "Usage: cli-utils.sh download <url> [output_filename]"
        return 1
    fi
    if [ -z "$output" ]; then
        output=$(basename "$url")
    fi
    if command -v curl &>/dev/null; then
        curl -L -o "$output" "$url" 2>&1
    elif command -v wget &>/dev/null; then
        wget -O "$output" "$url" 2>&1
    else
        echo "ERROR: Neither curl nor wget found"
        return 1
    fi
    echo "Downloaded: $output ($(wc -c < "$output") bytes)"
}

cmd_extract() {
    local archive="${1:-}"
    if [ -z "$archive" ]; then
        echo "Usage: cli-utils.sh extract <archive>"
        return 1
    fi
    case "$archive" in
        *.tar.gz|*.tgz)  tar xzf "$archive" ;;
        *.tar.bz2)       tar xjf "$archive" ;;
        *.tar.xz)        tar xJf "$archive" ;;
        *.tar)           tar xf "$archive" ;;
        *.zip)           unzip "$archive" ;;
        *.gz)            gunzip "$archive" ;;
        *.bz2)           bunzip2 "$archive" ;;
        *)               echo "Unknown archive format: $archive"; return 1 ;;
    esac
    echo "Extracted: $archive"
}

cmd_json_format() {
    local file="${1:-}"
    if [ -z "$file" ]; then
        echo "Usage: cli-utils.sh json-format <file>"
        return 1
    fi
    python3 -m json.tool "$file" 2>/dev/null || jq '.' "$file" 2>/dev/null || echo "ERROR: Cannot parse JSON"
}

cmd_csv_preview() {
    local file="${1:-}"
    local n="${2:-10}"
    if [ -z "$file" ]; then
        echo "Usage: cli-utils.sh csv-preview <file> [rows]"
        return 1
    fi
    head -n "$((n + 1))" "$file" | column -t -s',' 2>/dev/null || head -n "$((n + 1))" "$file"
}

cmd_hash() {
    local file="${1:-}"
    if [ -z "$file" ]; then
        echo "Usage: cli-utils.sh hash <file>"
        return 1
    fi
    echo "MD5:    $(md5sum "$file" | awk '{print $1}')"
    echo "SHA256: $(sha256sum "$file" | awk '{print $1}')"
}

cmd_env_check() {
    echo "=== Valentine Environment Check ==="
    local vars=("GROQ_API_KEY" "CEREBRAS_API_KEY" "SAMBANOVA_API_KEY" "TELEGRAM_BOT_TOKEN" "REDIS_URL" "GITHUB_PAT")
    for var in "${vars[@]}"; do
        if [ -n "${!var:-}" ]; then
            echo "  $var: SET (${#!var} chars)"
        else
            echo "  $var: NOT SET"
        fi
    done
}

cmd_kill_port() {
    local port="${1:-}"
    if [ -z "$port" ]; then
        echo "Usage: cli-utils.sh kill-port <port>"
        return 1
    fi
    local pid
    pid=$(lsof -ti ":$port" 2>/dev/null || ss -tlnp "sport = :$port" 2>/dev/null | grep -oP 'pid=\K[0-9]+')
    if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null
        echo "Killed process $pid on port $port"
    else
        echo "No process found on port $port"
    fi
}

case "${1:-}" in
    search)      shift; cmd_search "$@" ;;
    find)        shift; cmd_find "$@" ;;
    count)       shift; cmd_count "$@" ;;
    size)        shift; cmd_size "$@" ;;
    download)    shift; cmd_download "$@" ;;
    extract)     shift; cmd_extract "$@" ;;
    json-format) shift; cmd_json_format "$@" ;;
    csv-preview) shift; cmd_csv_preview "$@" ;;
    hash)        shift; cmd_hash "$@" ;;
    env-check)   cmd_env_check ;;
    ports-in-use) ss -tuln 2>/dev/null || netstat -tuln ;;
    kill-port)   shift; cmd_kill_port "$@" ;;
    -h|--help)   usage ;;
    *)           usage ;;
esac
