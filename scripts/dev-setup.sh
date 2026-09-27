#!/bin/sh
# Set up the development environment for this repository, isolated in ./.venv:
#   - Python deps (tests, PySpark + spark-pipelines, chispa) via uv
#   - a Temurin JDK for local Spark, unpacked into .venv/jdk with `java` linked into .venv/bin
# Rust (rustup + wasm32-wasip2) is only checked, since rustup manages its own per-user install.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv"
JDK_MAJOR=21

cd "$ROOT"
uv sync

if [ ! -x "$VENV/jdk/bin/java" ]; then
    case "$(uname -s)-$(uname -m)" in
        Linux-x86_64) os=linux arch=x64 ;;
        Linux-aarch64) os=linux arch=aarch64 ;;
        Darwin-arm64) os=mac arch=aarch64 ;;
        Darwin-x86_64) os=mac arch=x64 ;;
        *) echo "unsupported platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
    esac
    meta="$(curl -fsSL "https://api.adoptium.net/v3/assets/latest/$JDK_MAJOR/hotspot?architecture=$arch&image_type=jdk&os=$os&vendor=eclipse")"
    link="$(printf '%s' "$meta" | "$VENV/bin/python" -c 'import json,sys; print(json.load(sys.stdin)[0]["binary"]["package"]["link"])')"
    sum="$(printf '%s' "$meta" | "$VENV/bin/python" -c 'import json,sys; print(json.load(sys.stdin)[0]["binary"]["package"]["checksum"])')"

    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    echo "downloading $link"
    curl -fsSL "$link" -o "$tmp/jdk.tar.gz"
    if command -v sha256sum >/dev/null 2>&1; then
        echo "$sum  $tmp/jdk.tar.gz" | sha256sum -c -
    else
        echo "$sum  $tmp/jdk.tar.gz" | shasum -a 256 -c -
    fi
    mkdir -p "$tmp/jdk"
    tar -xzf "$tmp/jdk.tar.gz" -C "$tmp/jdk" --strip-components=1
    rm -rf "$VENV/jdk"
    # macOS tarballs nest the JDK under Contents/Home
    if [ -d "$tmp/jdk/Contents/Home" ]; then mv "$tmp/jdk/Contents/Home" "$VENV/jdk"; else mv "$tmp/jdk" "$VENV/jdk"; fi
fi
ln -sf "$VENV/jdk/bin/java" "$VENV/bin/java"
"$VENV/bin/java" -version 2>&1 | head -1

if command -v cargo >/dev/null 2>&1 || [ -x "$HOME/.cargo/bin/cargo" ]; then
    PATH="$HOME/.cargo/bin:$PATH"
    rustup target list --installed | grep -q wasm32-wasip2 || rustup target add wasm32-wasip2
    rustc --version
else
    echo "note: Rust is not installed; it's needed for the extension and databricks-bundle-ls." >&2
    echo "      Install: curl https://sh.rustup.rs -sSf | sh -s -- -y --target wasm32-wasip2" >&2
fi
