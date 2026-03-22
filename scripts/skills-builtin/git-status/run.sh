#!/bin/bash
# DESC: Check git repository status, branches, and recent commits

REPO="${2:-.}"

case "${1:-status}" in
    status)
        cd "$REPO" && git status --short --branch 2>/dev/null || echo "Not a git repo: $REPO"
        ;;
    branches)
        cd "$REPO" && git branch -a --sort=-committerdate 2>/dev/null | head -20
        ;;
    log)
        cd "$REPO" && git log --oneline --graph --decorate -15 2>/dev/null
        ;;
    diff)
        cd "$REPO" && git diff --stat 2>/dev/null
        ;;
    *)
        echo "Usage: git-status <status|branches|log|diff> [repo-path]"
        ;;
esac
