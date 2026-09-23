#!/bin/sh
# Wire a Databricks bundle project for Zed: copies the .zed/ task + settings templates
# and generates the bundle JSON schema from the installed Databricks CLI.
#
# Usage: scripts/setup-project.sh [PROJECT_DIR]   (defaults to the current directory)
set -eu

TEMPLATE_DIR="$(cd "$(dirname "$0")/../project-template/.zed" && pwd)"
PROJECT_DIR="$(cd "${1:-.}" && pwd)"
ZED_DIR="$PROJECT_DIR/.zed"
SCHEMA="$ZED_DIR/databricks-bundle.schema.json"
SCHEMA_VERSION="$ZED_DIR/databricks-bundle.schema.version"

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

if [ -e "$PROJECT_DIR/.gitignore" ] && ! grep -q 'databricks-bundle.schema' "$PROJECT_DIR/.gitignore"; then
    printf '\n# Zed: bundle schema generated from the local Databricks CLI\n.zed/databricks-bundle.schema.*\n' >> "$PROJECT_DIR/.gitignore"
    echo "updated   .gitignore"
fi

if command -v databricks >/dev/null 2>&1; then
    databricks bundle schema > "$SCHEMA.tmp"
    mv "$SCHEMA.tmp" "$SCHEMA"
    databricks --version > "$SCHEMA_VERSION"
    echo "generated .zed/databricks-bundle.schema.json ($(cat "$SCHEMA_VERSION"))"
else
    echo "warning: 'databricks' CLI not found on PATH; install it, then run the" >&2
    echo "         'databricks: refresh bundle schema' task to enable bundle YAML validation." >&2
fi
