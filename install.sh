#!/usr/bin/env bash
# Install epub-shrink into your PATH.
#
# The project is relocatable: it can live in any folder. This script only
# creates links into ~/.local/bin so the two commands can be run by name.
# Re-run it after moving the project folder to refresh the links.
#
#   ./install.sh            link into ~/.local/bin
#   ./install.sh --uninstall remove the links again
#
# Runtime requirements: Python 3.10+ with Pillow for the CLI; the GUI
# additionally needs PySide6 (Qt 6):
#   python3 -m pip install --user PySide6 Pillow

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
LINK_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
COMMANDS=(epub-shrink epub-shrink-gui)

uninstall() {
    for name in "${COMMANDS[@]}"; do
        target="$LINK_DIR/$name"
        if [[ -L "$target" ]]; then
            rm -f "$target"
            echo "removed $target"
        elif [[ -e "$target" ]]; then
            echo "left alone (not a link, so not ours to delete): $target" >&2
        fi
    done
}

if [[ "${1:-}" == "--uninstall" ]]; then
    uninstall
    exit 0
fi

mkdir -p "$LINK_DIR"

# The runtime (PySide6 + Pillow) lives in its own venv under the user's home
# rather than in this folder. Two reasons: the system Python is PEP 668
# managed, so `pip install --user` is refused, and this project may sit on a
# network mount that cannot hold a venv (no symlink support).
VENV="${EPUB_SHRINK_VENV:-$HOME/.local/share/epub-shrink/venv}"
if [[ -x "$VENV/bin/python3" ]] \
   && "$VENV/bin/python3" -c "import PySide6, PIL" 2>/dev/null; then
    echo "runtime already present: $VENV"
else
    echo "creating runtime: $VENV"
    mkdir -p "$(dirname "$VENV")"
    "${PYTHON:-python3}" -m venv "$VENV"
    "$VENV/bin/python3" -m pip install --quiet --upgrade pip
    "$VENV/bin/python3" -m pip install --quiet PySide6 Pillow
    "$VENV/bin/python3" -c "import PySide6, PIL" \
        || { echo "failed to install PySide6 and Pillow into $VENV" >&2; exit 1; }
    echo "installed PySide6 and Pillow"
fi

for name in "${COMMANDS[@]}"; do
    source_path="$HERE/$name"
    if [[ ! -f "$source_path" ]]; then
        echo "missing $source_path, skipping" >&2
        continue
    fi
    target="$LINK_DIR/$name"

    if [[ -e "$target" && ! -L "$target" ]]; then
        # An older copy of this project was left on PATH. It is a duplicate of
        # the files in this folder, so it is replaced rather than kept, but the
        # caller is told exactly what was removed.
        backup="$target.replaced-$(date +%Y%m%d%H%M%S)"
        echo "existing file found, moving to $backup"
        mv "$target" "$backup"
    fi

    ln -sfn "$source_path" "$target"
    echo "linked $target -> $source_path"
done

# Make sure the link directory is actually on PATH for future shells.
if ! grep -qs "XDG_BIN_HOME\|\.local/bin" "$HOME/.profile" 2>/dev/null; then
    echo
    echo "note: add this to ~/.profile if the commands are not found:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
