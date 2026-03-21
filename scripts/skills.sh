#!/bin/bash
# ============================================================
# Valentine Skills Manager
# Install, list, run, and manage modular skills for Valentine.
# Skills are executable scripts in the skills/ directory.
# ============================================================

set -euo pipefail

SKILLS_DIR="${VALENTINE_SKILLS_DIR:-/opt/valentine/skills}"
SKILLS_REGISTRY="${VALENTINE_SKILLS_REGISTRY:-https://raw.githubusercontent.com/Tee-David/valentine-skills/main}"
SKILLS_CONFIG="${SKILLS_DIR}/.installed"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

usage() {
    echo -e "${BLUE}Valentine Skills Manager${NC}"
    echo ""
    echo "Usage: skills.sh <command> [args]"
    echo ""
    echo "Commands:"
    echo "  list                 List all installed skills"
    echo "  available            List skills available for installation"
    echo "  install <name>       Install a skill"
    echo "  uninstall <name>     Remove a skill"
    echo "  run <name> [args]    Execute a skill"
    echo "  info <name>          Show details about a skill"
    echo "  update               Update all installed skills"
    echo "  init                 Initialize skills directory"
    echo ""
    echo "Built-in skill categories:"
    echo "  audio      - TTS/STT, voice processing"
    echo "  github     - Repo management, PRs, issues"
    echo "  server     - System monitoring, diagnostics"
    echo "  deploy     - Deployment and CI/CD"
    echo "  cli        - Shell utilities and automation"
}

init_skills() {
    mkdir -p "$SKILLS_DIR"
    touch "$SKILLS_CONFIG"
    echo -e "${GREEN}Skills directory initialized at ${SKILLS_DIR}${NC}"
}

list_skills() {
    if [ ! -d "$SKILLS_DIR" ]; then
        echo -e "${YELLOW}No skills directory. Run: skills.sh init${NC}"
        return
    fi

    local count=0
    echo -e "${BLUE}Installed Skills:${NC}"
    echo "─────────────────────────────────────────"

    for skill in "$SKILLS_DIR"/*.sh; do
        [ -f "$skill" ] || continue
        local name
        name=$(basename "$skill" .sh)
        local desc
        desc=$(grep -m1 '^# DESC:' "$skill" 2>/dev/null | sed 's/^# DESC: *//' || echo "No description")
        local category
        category=$(grep -m1 '^# CATEGORY:' "$skill" 2>/dev/null | sed 's/^# CATEGORY: *//' || echo "uncategorized")

        printf "  ${GREEN}%-20s${NC} [%s] %s\n" "$name" "$category" "$desc"
        count=$((count + 1))
    done

    if [ "$count" -eq 0 ]; then
        echo -e "  ${YELLOW}No skills installed. Run: skills.sh install <name>${NC}"
    fi
    echo ""
    echo "Total: $count skill(s)"
}

install_skill() {
    local name="${1:-}"
    if [ -z "$name" ]; then
        echo -e "${RED}Usage: skills.sh install <skill-name>${NC}"
        return 1
    fi

    mkdir -p "$SKILLS_DIR"

    # Check if it's a built-in skill
    local builtin_dir
    builtin_dir="$(dirname "$(readlink -f "$0")")/skills-builtin"

    if [ -f "$builtin_dir/${name}.sh" ]; then
        cp "$builtin_dir/${name}.sh" "$SKILLS_DIR/${name}.sh"
        chmod +x "$SKILLS_DIR/${name}.sh"
        echo "$name" >> "$SKILLS_CONFIG"
        echo -e "${GREEN}Installed built-in skill: ${name}${NC}"
        return
    fi

    # Try remote registry
    local url="${SKILLS_REGISTRY}/skills/${name}.sh"
    if command -v curl &>/dev/null; then
        if curl -sf "$url" -o "$SKILLS_DIR/${name}.sh" 2>/dev/null; then
            chmod +x "$SKILLS_DIR/${name}.sh"
            echo "$name" >> "$SKILLS_CONFIG"
            echo -e "${GREEN}Installed skill from registry: ${name}${NC}"
            return
        fi
    fi

    echo -e "${RED}Skill '${name}' not found in built-ins or registry.${NC}"
    return 1
}

uninstall_skill() {
    local name="${1:-}"
    if [ -z "$name" ]; then
        echo -e "${RED}Usage: skills.sh uninstall <skill-name>${NC}"
        return 1
    fi

    if [ -f "$SKILLS_DIR/${name}.sh" ]; then
        rm -f "$SKILLS_DIR/${name}.sh"
        sed -i "/^${name}$/d" "$SKILLS_CONFIG" 2>/dev/null || true
        echo -e "${GREEN}Uninstalled skill: ${name}${NC}"
    else
        echo -e "${YELLOW}Skill '${name}' is not installed.${NC}"
    fi
}

run_skill() {
    local name="${1:-}"
    if [ -z "$name" ]; then
        echo -e "${RED}Usage: skills.sh run <skill-name> [args...]${NC}"
        return 1
    fi

    local skill_path="$SKILLS_DIR/${name}.sh"
    if [ ! -f "$skill_path" ]; then
        echo -e "${RED}Skill '${name}' is not installed.${NC}"
        return 1
    fi

    shift
    bash "$skill_path" "$@"
}

info_skill() {
    local name="${1:-}"
    if [ -z "$name" ]; then
        echo -e "${RED}Usage: skills.sh info <skill-name>${NC}"
        return 1
    fi

    local skill_path="$SKILLS_DIR/${name}.sh"
    if [ ! -f "$skill_path" ]; then
        echo -e "${RED}Skill '${name}' is not installed.${NC}"
        return 1
    fi

    echo -e "${BLUE}Skill: ${name}${NC}"
    echo "─────────────────────────────────────────"
    grep '^# DESC:' "$skill_path" | sed 's/^# DESC: */Description: /'
    grep '^# CATEGORY:' "$skill_path" | sed 's/^# CATEGORY: */Category: /'
    grep '^# AUTHOR:' "$skill_path" | sed 's/^# AUTHOR: */Author: /'
    grep '^# VERSION:' "$skill_path" | sed 's/^# VERSION: */Version: /'
    grep '^# USAGE:' "$skill_path" | sed 's/^# USAGE: */Usage: /'
    echo ""
    echo "Path: $skill_path"
    echo "Size: $(wc -c < "$skill_path") bytes"
}

update_skills() {
    echo -e "${BLUE}Updating all installed skills...${NC}"
    if [ ! -f "$SKILLS_CONFIG" ]; then
        echo -e "${YELLOW}No skills to update.${NC}"
        return
    fi
    while IFS= read -r name; do
        [ -z "$name" ] && continue
        echo -n "  Updating $name... "
        install_skill "$name" 2>/dev/null && echo -e "${GREEN}done${NC}" || echo -e "${YELLOW}skipped${NC}"
    done < "$SKILLS_CONFIG"
}

# Main dispatch
case "${1:-}" in
    list)       list_skills ;;
    available)  list_skills ;; # Same as list for now
    install)    shift; install_skill "$@" ;;
    uninstall)  shift; uninstall_skill "$@" ;;
    run)        shift; run_skill "$@" ;;
    info)       shift; info_skill "$@" ;;
    update)     update_skills ;;
    init)       init_skills ;;
    -h|--help)  usage ;;
    *)          usage ;;
esac
