#!/bin/sh
# Wire a Databricks bundle project for Zed: copies the .zed/ task + settings templates
# (the extension itself provides schema validation and diagnostics).
#
# Usage: scripts/setup-project.sh [PROJECT_DIR]   (defaults to the current directory)
set -eu

TEMPLATE_DIR="$(cd "$(dirname "$0")/../project-template/.zed" && pwd)"
PROJECT_DIR="$(cd "${1:-.}" && pwd)"
ZED_DIR="$PROJECT_DIR/.zed"

mkdir -p "$ZED_DIR"

# Never overwrite user config: write alongside it and ask for a manual merge instead.
install_template() {
    name="$1"
    if [ ! -e "$ZED_DIR/$name" ]; then
        cp "$TEMPLATE_DIR/$name" "$ZED_DIR/$name"
        echo "created   .zed/$name"
    elif cmp -s "$TEMPLATE_DIR/$name" "$ZED_DIR/$name"; then
        echo "unchanged .zed/$name"
    else
        cp "$TEMPLATE_DIR/$name" "$ZED_DIR/databricks.$name"
        echo "exists    .zed/$name -> wrote .zed/databricks.$name; merge it in by hand"
    fi
}
install_template tasks.json
install_template settings.json

if ! command -v databricks >/dev/null 2>&1; then
    echo "warning: 'databricks' CLI not found on PATH; tasks, schema and diagnostics need it." >&2
fi
