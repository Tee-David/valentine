#!/bin/bash
# DESC: GitHub repository management — clone, status, pull, push, PRs
# CATEGORY: github
# AUTHOR: Valentine
# VERSION: 1.0
# USAGE: github-repo.sh <command> [args]

set -euo pipefail

WORKSPACE="${VALENTINE_WORKSPACE:-/tmp/valentine/workspace}"
GITHUB_PAT="${GITHUB_PAT:-}"

usage() {
    echo "Valentine GitHub Skill"
    echo ""
    echo "Commands:"
    echo "  clone <repo_url> [dir]   Clone a repository"
    echo "  status [dir]             Show git status"
    echo "  pull [dir]               Pull latest changes"
    echo "  push [dir] [msg]         Commit and push all changes"
    echo "  log [dir] [n]            Show last N commits (default: 10)"
    echo "  branch [dir]             Show current branch"
    echo "  branches [dir]           List all branches"
    echo "  diff [dir]               Show uncommitted changes"
    echo "  pr-list [dir]            List open pull requests (requires gh)"
    echo "  pr-create [dir] <title>  Create a pull request (requires gh)"
    echo "  issues [owner/repo]      List open issues (requires gh)"
}

require_git() {
    if ! command -v git &>/dev/null; then
        echo "ERROR: git not installed"
        exit 1
    fi
}

clone_repo() {
    require_git
    local url="${1:-}"
    local dir="${2:-}"

    if [ -z "$url" ]; then
        echo "Usage: github-repo.sh clone <repo_url> [directory]"
        return 1
    fi

    # Inject PAT for private repos if available
    if [ -n "$GITHUB_PAT" ] && [[ "$url" == https://github.com/* ]]; then
        url="${url/https:\/\//https:\/\/${GITHUB_PAT}@}"
    fi

    if [ -z "$dir" ]; then
        dir="$WORKSPACE/$(basename "$url" .git)"
    fi

    git clone "$url" "$dir" 2>&1
    echo "Cloned to: $dir"
}

repo_status() {
    require_git
    local dir="${1:-$WORKSPACE}"
    cd "$dir"
    echo "Branch: $(git branch --show-current)"
    echo ""
    git status --short
}

repo_pull() {
    require_git
    local dir="${1:-$WORKSPACE}"
    cd "$dir"
    git pull 2>&1
}

repo_push() {
    require_git
    local dir="${1:-$WORKSPACE}"
    local msg="${2:-Auto-commit by Valentine}"
    cd "$dir"
    git add -A
    git commit -m "$msg" 2>&1 || echo "Nothing to commit"
    git push 2>&1
}

repo_log() {
    require_git
    local dir="${1:-$WORKSPACE}"
    local n="${2:-10}"
    cd "$dir"
    git log --oneline -n "$n"
}

repo_diff() {
    require_git
    local dir="${1:-$WORKSPACE}"
    cd "$dir"
    git diff
}

repo_branches() {
    require_git
    local dir="${1:-$WORKSPACE}"
    cd "$dir"
    git branch -a
}

pr_list() {
    local dir="${1:-$WORKSPACE}"
    cd "$dir"
    if command -v gh &>/dev/null; then
        gh pr list
    else
        echo "ERROR: GitHub CLI (gh) not installed. Install: https://cli.github.com"
    fi
}

pr_create() {
    local dir="${1:-$WORKSPACE}"
    local title="${2:-}"
    cd "$dir"
    if [ -z "$title" ]; then
        echo "Usage: github-repo.sh pr-create [dir] <title>"
        return 1
    fi
    if command -v gh &>/dev/null; then
        gh pr create --title "$title" --fill
    else
        echo "ERROR: GitHub CLI (gh) not installed."
    fi
}

list_issues() {
    local repo="${1:-}"
    if [ -z "$repo" ]; then
        echo "Usage: github-repo.sh issues <owner/repo>"
        return 1
    fi
    if command -v gh &>/dev/null; then
        gh issue list -R "$repo"
    else
        echo "ERROR: GitHub CLI (gh) not installed."
    fi
}

case "${1:-}" in
    clone)      shift; clone_repo "$@" ;;
    status)     shift; repo_status "$@" ;;
    pull)       shift; repo_pull "$@" ;;
    push)       shift; repo_push "$@" ;;
    log)        shift; repo_log "$@" ;;
    branch)     shift; require_git; cd "${1:-$WORKSPACE}"; git branch --show-current ;;
    branches)   shift; repo_branches "$@" ;;
    diff)       shift; repo_diff "$@" ;;
    pr-list)    shift; pr_list "$@" ;;
    pr-create)  shift; pr_create "$@" ;;
    issues)     shift; list_issues "$@" ;;
    -h|--help)  usage ;;
    *)          usage ;;
esac
