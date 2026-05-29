#!/usr/bin/env bash
# GNOME Wayland 截图：勿用 grim（不支持 Mutter），勿用 org.gnome.Shell.Screenshot（AccessDenied）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-screenshots/screen.png}"
mkdir -p "$(dirname "$OUT")"
ABS="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"

is_gnome() {
  [[ "${XDG_CURRENT_DESKTOP:-}" == *GNOME* ]] \
    || [[ "${DESKTOP_SESSION:-}" == *gnome* ]]
}

if is_gnome; then
  if ! python3 "$ROOT/scripts/portal-screenshot.py" "$ABS" 2>&1; then
    echo "提示: GNOME 需在**本机图形会话的终端**运行 Gateway，并在「设置 → 隐私 → 屏幕截图」允许终端/Python。" >&2
    echo "首次可试: python3 scripts/portal-screenshot.py screenshots/screen.png --interactive" >&2
    exit 1
  fi
  exit 0
fi

if [[ "${XDG_SESSION_TYPE:-}" == wayland ]] && command -v grim >/dev/null 2>&1; then
  grim "$ABS"
  echo "$ABS"
  exit 0
fi

if command -v scrot >/dev/null 2>&1; then
  scrot "$ABS"
  echo "$ABS"
  exit 0
fi

echo "未找到可用截图方式。" >&2
exit 1
