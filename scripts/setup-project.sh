#!/bin/sh
# Wire a Databricks project for Zed: copies the .zed/ task + settings templates and the
# Python type stubs for Databricks pipeline code (typings/pyspark-stubs, __builtins__.pyi).
# The extension itself provides bundle schema validation and diagnostics.
#
# Usage: scripts/setup-project.sh [PROJECT_DIR]   (defaults to the current directory)
set -eu

TEMPLATE_ROOT="$(cd "$(dirname "$0")/../project-template" && pwd)"
TEMPLATE_DIR="$TEMPLATE_ROOT/.zed"
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

# Python stubs: basedpyright (Zed's default) reads typings/ and __builtins__.pyi from the project root.
if [ ! -e "$PROJECT_DIR/typings/pyspark-stubs" ]; then
    mkdir -p "$PROJECT_DIR/typings"
    cp -R "$TEMPLATE_ROOT/typings/pyspark-stubs" "$PROJECT_DIR/typings/pyspark-stubs"
    echo "created   typings/pyspark-stubs"
else
    echo "exists    typings/pyspark-stubs (left unchanged)"
fi
if [ ! -e "$PROJECT_DIR/__builtins__.pyi" ]; then
    cp "$TEMPLATE_ROOT/__builtins__.pyi" "$PROJECT_DIR/__builtins__.pyi"
    echo "created   __builtins__.pyi"
else
    echo "exists    __builtins__.pyi (left unchanged; see project-template/__builtins__.pyi)"
fi

if ! command -v databricks >/dev/null 2>&1; then
    echo "warning: 'databricks' CLI not found on PATH; tasks, schema and diagnostics need it." >&2
fi
