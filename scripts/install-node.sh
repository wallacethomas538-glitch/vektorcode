#!/usr/bin/env bash
# install-node.sh — install Node.js 22.x for the VektorCode Render build.
#
# VERIFIED 2026-11-15 on Ubuntu 24.04 x86_64 (see docs/hypit-runtime-findings.md):
#   - official tarball v22.21.1: 30 MB download, 201 MB extracted, ~10 s total
#   - node v22.21.1 / npm 10.9.4, no root, no sudo, no system-wide install
#   - `tar --no-same-owner` is REQUIRED: plain `tar -xf` exits 2 on chown
#     errors ("Cannot change ownership to uid 1000") and would fail a
#     `set -e` buildCommand even though the files extract.
#
# Usage (do NOT wire into render.yaml without the builder's go-ahead):
#   buildCommand: bash scripts/install-node.sh && export PATH="$RENDER_PROJECT_DIR/.node/bin:$PATH" && npm install -g @hypit/hypit --prefix="$RENDER_PROJECT_DIR/.hypit" && pip install -r requirements.txt
#
# Notes:
# - Idempotent: if $INSTALL_DIR/bin/node already reports the requested
#   version, the script exits 0 without re-downloading.
# - Installs into $RENDER_PROJECT_DIR/.node (falls back to $PWD/.node).
#   Render's ephemeral disk is rebuilt every deploy, so this re-runs each
#   build (~10 s) — expected, no cache assumed.
# - Verifies the tarball SHA-256 against nodejs.org SHASUMS256.txt.
# - A sourced invocation also exports PATH for the rest of the shell:
#     source scripts/install-node.sh
#   An executed invocation cannot export to its parent; the buildCommand
#   must add $INSTALL_DIR/bin to PATH itself (see usage above).
set -euo pipefail

NODE_VERSION="${NODE_VERSION:-22.21.1}"
PROJECT_DIR="${RENDER_PROJECT_DIR:-$PWD}"
INSTALL_DIR="${NODE_INSTALL_DIR:-$PROJECT_DIR/.node}"
NODE_DIST_BASE="${NODE_DIST_BASE:-https://nodejs.org/dist}"
TARBALL="node-v${NODE_VERSION}-linux-x64.tar.xz"
URL="${NODE_DIST_BASE}/v${NODE_VERSION}/${TARBALL}"

if [ -x "$INSTALL_DIR/bin/node" ]; then
  INSTALLED="$("$INSTALL_DIR/bin/node" --version 2>/dev/null || true)"
  if [ "$INSTALLED" = "v$NODE_VERSION" ]; then
    echo "Node $NODE_VERSION already installed at $INSTALL_DIR — skipping."
    if [ "${BASH_SOURCE[0]:-$0}" != "$0" ]; then
      export PATH="$INSTALL_DIR/bin:$PATH"
    fi
    exit 0
  fi
  echo "Found Node $INSTALLED at $INSTALL_DIR, want v$NODE_VERSION — reinstalling."
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/node-install.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT
cd "$TMP_DIR"

echo "Downloading Node v$NODE_VERSION from $URL ..."
if command -v curl >/dev/null 2>&1; then
  curl -fsSL -o "$TARBALL" "$URL"
  curl -fsSL -o SHASUMS256.txt "${NODE_DIST_BASE}/v${NODE_VERSION}/SHASUMS256.txt"
elif command -v wget >/dev/null 2>&1; then
  wget -q -O "$TARBALL" "$URL"
  wget -q -O SHASUMS256.txt "${NODE_DIST_BASE}/v${NODE_VERSION}/SHASUMS256.txt"
else
  echo "ERROR: neither curl nor wget is available — cannot download Node." >&2
  exit 1
fi

echo "Verifying SHA-256 checksum ..."
if command -v sha256sum >/dev/null 2>&1; then
  grep "  $TARBALL\$" SHASUMS256.txt > "$TARBALL.sha256"
  sha256sum -c "$TARBALL.sha256"
else
  echo "WARNING: sha256sum not available — skipping checksum verification." >&2
fi

echo "Extracting to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
# --no-same-owner: the tarball preserves uid/gid 1000; without this flag tar
# exits 2 on chown errors in a non-root build and fails `set -e`.
tar --no-same-owner -xf "$TARBALL"
rm -rf "$INSTALL_DIR"
mv "node-v${NODE_VERSION}-linux-x64" "$INSTALL_DIR"

echo "Node version: $("$INSTALL_DIR/bin/node" --version)"
echo "npm version:  $("$INSTALL_DIR/bin/npm" --version)"
echo "Installed Node $NODE_VERSION to $INSTALL_DIR"
echo "Add to PATH: export PATH=\"$INSTALL_DIR/bin:\$PATH\""

if [ "${BASH_SOURCE[0]:-$0}" != "$0" ]; then
  export PATH="$INSTALL_DIR/bin:$PATH"
fi
