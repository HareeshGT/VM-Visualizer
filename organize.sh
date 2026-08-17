#!/usr/bin/env bash
# organize_ec2_manager.sh — run this from inside your project directory
# (the folder that currently has all the .py files loose in it).
#
# Layout after running:
#   .
#   ├── main.py, requirements.txt, README.md, build_app.sh   (stay at root)
#   ├── app/        — every interdependent .py module (imports untouched)
#   └── assets/     — icons
#
# Safe by design: nothing here rewrites an `import` statement. main.py gets
# one line added (sys.path.insert) so `app/` still resolves as if every
# module were sitting next to main.py, exactly like today.

set -euo pipefail

if [ ! -f "main.py" ]; then
  echo "Run this from the project root (the folder containing main.py)." >&2
  exit 1
fi

mkdir -p app assets

# Use `git mv` if this is a git repo (keeps history), else plain `mv`.
mv_cmd() {
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git mv "$1" "$2"
  else
    mv "$1" "$2"
  fi
}

APP_FILES=(
  dashboard_tab.py
  dialogs.py
  editor_widgets.py
  file_widgets.py
  k8s_cards.py
  kubernetes_tab.py
  lock_screen.py
  main_window.py
  preview.py
  progress_ring.py
  security.py
  settings_dialog.py
  sidebar.py
  splash.py
  sudo_fs.py
  terminal_widget.py
  theme_picker.py
  themes.py
  utils.py
  workers.py
)

ASSET_FILES=(
  VM_Visualizer.icns
  VM_Visualizer.ico
)

for f in "${APP_FILES[@]}"; do
  if [ -f "$f" ]; then
    mv_cmd "$f" "app/$f"
    echo "moved $f -> app/"
  else
    echo "skip (not found): $f"
  fi
done

for f in "${ASSET_FILES[@]}"; do
  if [ -f "$f" ]; then
    mv_cmd "$f" "assets/$f"
    echo "moved $f -> assets/"
  else
    echo "skip (not found): $f"
  fi
done

# ── Patch main.py so `app/` resolves exactly like the old flat layout ──
# Inserted *after* any leading module docstring, so main.py's __doc__
# stays intact instead of being pushed out by the new lines.
if ! grep -q 'sys.path.insert(0, os.path.join' main.py; then
  python3 - <<'PY'
import io

path = "main.py"
with io.open(path, "r", encoding="utf-8") as f:
    content = f.read()

patch = (
    "import sys\n"
    "import os\n"
    "sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), \"app\"))\n"
)

stripped = content.lstrip()
insert_at = None
if stripped[:3] in ('"""', "'''"):
    quote = stripped[:3]
    end = stripped.find(quote, 3)
    if end != -1:
        offset = len(content) - len(stripped)
        after_quote = offset + end + 3
        nl = content.find("\n", after_quote)
        insert_at = (nl + 1) if nl != -1 else after_quote

if insert_at is not None:
    content = content[:insert_at] + "\n" + patch + content[insert_at:]
else:
    content = patch + "\n" + content

with io.open(path, "w", encoding="utf-8") as f:
    f.write(content)
PY
  echo "patched main.py to add app/ to sys.path (docstring preserved)"
else
  echo "main.py already patched — skipped"
fi

echo
echo "Done. Try: python3 main.py"
echo
echo "NOTE: build_app.sh (PyInstaller) and README.md were left untouched —"
echo "if build_app.sh references any of the moved .py files or icon paths"
echo "directly, it'll need those paths updated to app/ and assets/. Share"
echo "its contents and I can patch it too."
