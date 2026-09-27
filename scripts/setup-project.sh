#!/bin/sh
# Wire a Databricks project for Zed: copies the .zed/ tasks, debug scenarios and helpers and the
# Python type stubs for Databricks pipeline code (typings/pyspark-stubs, __builtins__.pyi), and
# merges the template's settings into .zed/settings.json.
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
# Settings are merged instead: the template's keys are added to the project's file (values it
# already sets win), keeping the original as settings.json.bak. Without python3, or if the
# file can't be parsed, fall back to writing it alongside for a manual merge.
install_settings() {
    if [ -e "$ZED_DIR/settings.json" ] && command -v python3 >/dev/null 2>&1 &&
        python3 "$(dirname "$0")/merge_settings.py" "$TEMPLATE_DIR/settings.json" "$ZED_DIR/settings.json"; then
        return
    fi
    install_template settings.json
}

install_template tasks.json
install_settings
install_template debug.json

# Helpers the tasks and debug scenarios run: the Databricks Connect runner and the CLI wrapper.
mkdir -p "$ZED_DIR/databricks"
for helper in "$TEMPLATE_DIR"/databricks/*; do
    [ -f "$helper" ] || continue  # e.g. a __pycache__ left by the tests
    name="databricks/$(basename "$helper")"
    if [ ! -e "$ZED_DIR/$name" ]; then
        cp "$helper" "$ZED_DIR/$name"
        echo "created   .zed/$name"
    elif cmp -s "$helper" "$ZED_DIR/$name"; then
        echo "unchanged .zed/$name"
    else
        echo "exists    .zed/$name (left unchanged; compare with $helper)"
    fi
done

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
