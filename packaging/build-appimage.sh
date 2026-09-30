#!/usr/bin/env bash
# Build the epub-shrink AppImage.
#
# The GUI is PySide6, which PyInstaller bundles cleanly (Qt, its plugins and
# Pillow all travel inside the image). There is no Tcl/Tk dependency and no
# host Python is required, so this is a normal one-command build:
#
#   ./build-appimage.sh
#
# Overrides:
#   APPIMAGE=...          output path
#   BUILD=... DIST=...    working and output directories
#   PYTHON=...            interpreter to freeze with (must have PyInstaller)
#   APPIMAGETOOL=...      path to appimagetool
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PROJECT="$(dirname "$HERE")"
# The build tree deliberately lives on local disk rather than beside the
# project. The project sits on an SMB share that does not support symlinks, and
# PyInstaller's onedir collect step creates symlinks for Qt's shared libraries.
# Only the finished image is copied back to DIST.
BUILD="${BUILD:-${XDG_RUNTIME_DIR:-/tmp}/epub-shrink-build-$(id -u)}"
DIST="${DIST:-$PROJECT/dist}"
APPDIR="$BUILD/EpubCompressor.AppDir"
ID="dev.epubshrink.app"
IMAGE="${APPIMAGE:-$DIST/epub-shrink-x86_64.AppImage}"

# ---------------------------------------------------------------- interpreter
# PyInstaller is a build-only dependency, so it may live in a venv separate from
# the one the app runs on. Prefer an explicit choice, then the project's own
# venv, then whatever python3 is on PATH.
if [[ -n "${PYTHON:-}" ]]; then
    :
elif [[ -x "$HOME/.local/share/epub-shrink/venv/bin/python" ]]; then
    PYTHON="$HOME/.local/share/epub-shrink/venv/bin/python"
else
    PYTHON="$(command -v python3)"
fi

die() { echo "error: $*" >&2; exit 1; }

command -v "$PYTHON" >/dev/null 2>&1 || die "python not found: $PYTHON"
"$PYTHON" -c 'import PyInstaller' 2>/dev/null \
    || die "PyInstaller is not installed for $PYTHON
    install it with:  $PYTHON -m pip install pyinstaller"
echo "freezing with $("$PYTHON" -c 'import sys; print(sys.executable)')"

# -------------------------------------------------------------- appimagetool
# appimagetool ships as an AppImage itself, so it needs either FUSE or the
# extract-and-run fallback. Both are handled here rather than being left as a
# confusing failure halfway through the build.
find_appimagetool() {
    if [[ -n "${APPIMAGETOOL:-}" ]]; then
        echo "$APPIMAGETOOL"; return
    fi
    if command -v appimagetool >/dev/null 2>&1; then
        command -v appimagetool; return
    fi
    local candidate
    for candidate in \
        "$HOME/.local/opt/appimagetool/appimagetool.AppImage" \
        "$HOME/.local/bin/appimagetool.AppImage" \
        "/usr/local/bin/appimagetool"
    do
        [[ -x "$candidate" ]] && { echo "$candidate"; return; }
    done
    echo ""
}

TOOL="$(find_appimagetool)"
[[ -n "$TOOL" ]] || die "appimagetool not found.
    download it once with:
      mkdir -p ~/.local/opt/appimagetool
      curl -L -o ~/.local/opt/appimagetool/appimagetool.AppImage \\
        https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
      chmod +x ~/.local/opt/appimagetool/appimagetool.AppImage
    or point APPIMAGETOOL= at an existing copy."

# An AppImage can only be mounted with FUSE. Where /dev/fuse is unavailable the
# tool can still run by unpacking itself, at the cost of a slower start.
if [[ ! -e /dev/fuse ]]; then
    echo "note: /dev/fuse is unavailable, appimagetool will extract and run"
    TOOL=("$TOOL" --appimage-extract-and-run)
else
    TOOL=("$TOOL")
fi

# ------------------------------------------------------------------- icons
# One source of truth: the artwork in the project root. Raster sizes are
# generated rather than committed so the icon can be re-exported at any
# resolution without editing the packaging scripts.
ICON_SRC=""
for candidate in "$PROJECT/icon.webp" "$PROJECT/icon.png" "$PROJECT/icon.ico"; do
    [[ -f "$candidate" ]] && { ICON_SRC="$candidate"; break; }
done
[[ -n "$ICON_SRC" ]] || die "no icon found in $PROJECT (expected icon.webp, icon.png or icon.ico)"

make_icons() {
    "$PYTHON" - "$ICON_SRC" "$APPDIR" "$ID" <<'PY'
import os
import sys
from PIL import Image

src, appdir, app_id = sys.argv[1:4]
# 256 is what the desktop file and most launchers ask for; the rest follow the
# hicolor theme so the AppImage integrates properly on any desktop.
SIZES = (16, 24, 32, 48, 64, 128, 256, 512)


def load_largest(path):
    """Open `path` at its largest available size.

    A .ico holds several frames and Pillow only offers direct access to the
    largest one through .size; picking the frame explicitly keeps this correct
    for both the webp and the ico the project ships.
    """
    image = Image.open(path)
    if image.format == "ICO":
        best = max(image.ico.sizes(), key=lambda size: size[0])
        image.ico.getimage(best).load()
        return image.ico.getimage(best)
    image.load()
    return image


image = load_largest(src).convert("RGBA")

for size in SIZES:
    resized = (image if image.size == (size, size)
               else image.resize((size, size), Image.LANCZOS))
    out = os.path.join(appdir, "usr", "share", "icons", "hicolor",
                       f"{size}x{size}", "apps", f"{app_id}.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    resized.save(out, "PNG", optimize=True)

# The AppDir root copy is what appimagetool embeds as the image's icon.
image.resize((256, 256), Image.LANCZOS).save(
    os.path.join(appdir, f"{app_id}.png"), "PNG", optimize=True)
print(f"  icons written from {os.path.basename(src)} "
      f"({image.size[0]}px source) at "
      + ", ".join(str(s) for s in SIZES) + " px")
PY
}

# ------------------------------------------------------------------- build
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" "$DIST"

# --onedir, not --onefile: an AppImage is already a compressed, read-only
# squashfs image, so there is nothing to gain from PyInstaller packing the
# bundle into a single executable and unpacking it into /tmp on every launch.
# That would add seconds of startup and a large temp copy for no benefit.
echo "freezing the application with PyInstaller"
"$PYTHON" -m PyInstaller \
    --noconfirm --clean \
    --name epub-shrink-gui \
    --distpath "$BUILD/pyinstaller-dist" \
    --workpath "$BUILD/pyinstaller" \
    --specpath "$BUILD" \
    --windowed \
    --onedir \
    --add-data "$PROJECT/epub_shrink.py:." \
    --add-data "$PROJECT/epub_shrink_settings.py:." \
    --add-data "$PROJECT/epub_shrink_abs.py:." \
    --collect-submodules PySide6 \
    "$HERE/frozen_main.py"

# PyInstaller writes dist/<name>/; move the whole tree into the AppDir rather
# than re-running the analysis just to change the destination.
FROZEN="$BUILD/pyinstaller-dist/epub-shrink-gui"
[[ -d "$FROZEN" ]] || die "PyInstaller did not produce $FROZEN"
cp -a "$FROZEN/." "$APPDIR/usr/bin/"

BIN="$APPDIR/usr/bin/epub-shrink-gui"
[[ -x "$BIN" ]] || die "PyInstaller did not produce $BIN"
chmod 755 "$BIN"
[[ -d "$APPDIR/usr/bin/_internal" ]] || die "PyInstaller's _internal tree is missing"
echo "  frozen tree: $(du -sh "$APPDIR/usr/bin" | cut -f1)"

install -m 755 "$HERE/AppRun" "$APPDIR/AppRun"
# The AppDir root copy is what launchers and appimagetool look at; the one
# under usr/share/applications is what an extracted image expects to find.
install -m 644 "$HERE/epub-shrink-gui.desktop" "$APPDIR/$ID.desktop"
install -m 644 "$HERE/epub-shrink-gui.desktop" \
    "$APPDIR/usr/share/applications/$ID.desktop"

# AppStream metadata lets the image describe itself to software centres and
# launchers. It is only worth including if it is actually well formed, so it is
# checked here and quietly left out on a hard error rather than failing the
# build. Warnings are tolerated: a missing homepage is not a reason to drop a
# perfectly good description, and inventing a URL to silence one would be worse.
METAINFO="$HERE/$ID.metainfo.xml"
if [[ -f "$METAINFO" ]] && command -v appstreamcli >/dev/null 2>&1; then
    mkdir -p "$APPDIR/usr/share/metainfo"
    install -m 644 "$METAINFO" "$APPDIR/usr/share/metainfo/$ID.metainfo.xml"
    if appstreamcli validate --no-net \
            "$APPDIR/usr/share/metainfo/$ID.metainfo.xml" 2>&1 \
            | grep -q '^E:'; then
        echo "note: AppStream metadata has errors, leaving it out" >&2
        rm -f "$APPDIR/usr/share/metainfo/$ID.metainfo.xml"
    fi
fi

echo "installing the icon"
make_icons

echo "smoke-testing the frozen binary (offscreen)"
QT_QPA_PLATFORM=offscreen "$BIN" --self-test

echo
echo "AppDir ready at $APPDIR"
echo "try it directly:  $APPDIR/AppRun"
echo
echo "building the image:"
# AppStream wrapping is only possible where appstreamcli exists; without it the
# metadata is still carried in the image, just without the embedded preview.
if command -v appstreamcli >/dev/null 2>&1; then
    "${TOOL[@]}" "$APPDIR" "$IMAGE"
else
    "${TOOL[@]}" --no-appstream "$APPDIR" "$IMAGE"
fi

echo
echo "done: $IMAGE"
ls -lh "$IMAGE"

# The AppDir is only a build artefact, but it is large and fully reproducible
# from this script. Leaving it behind by default keeps a multi-hundred-megabyte
# directory out of the project unless it is wanted for debugging.
if [[ -z "${KEEP_APPDIR:-}" ]]; then
    rm -rf "$APPDIR" "$BUILD/pyinstaller" "$BUILD/pyinstaller-dist" "$BUILD"/*.spec
    echo "removed the build tree (set KEEP_APPDIR=1 to keep it)"
fi
