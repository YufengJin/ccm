#!/usr/bin/env bash
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/.local/bin
ln -sf "$DIR/bin/ccm" ~/.local/bin/ccm
echo "已安装: ~/.local/bin/ccm -> $DIR/bin/ccm"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "警告: ~/.local/bin 不在 PATH" ;;
esac
echo "下一步: ccm migrate --dry-run"
