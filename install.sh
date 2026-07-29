#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(dirname "$0")"
SRC="$SRC_DIR/agents/executor.md"
DEST_DIR="$HOME/.claude/agents"
DEST="$DEST_DIR/executor.md"

mkdir -p "$DEST_DIR"

if [ -f "$DEST" ]; then
  BAK="$DEST.bak.$(date +%Y%m%d%H%M%S)"
  cp "$DEST" "$BAK"
  echo "已备份原有文件到：$BAK"
fi

cp "$SRC" "$DEST"

echo "安装完成，新开会话后生效"
