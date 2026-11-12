#!/usr/bin/env bash
#
# Deskboard installer / uninstaller.
#
# Installs into the user's XDG prefix, no root required:
#
#   ~/.local/share/deskboard/{deskboard,data,assets,scripts,...}  application
#   ~/.local/bin/deskboard                                      launcher
#   ~/.local/share/applications/deskboard.desktop                menu entry
#   ~/.local/share/icons/hicolor/scalable/apps/dev.wrecht.Deskboard.svg
#
# Usage:
#   scripts/install.sh                  install or update
#   scripts/install.sh --uninstall      remove the app, launcher, menu entry, icon
#   scripts/install.sh --uninstall --purge
#                                       also remove session cookies, preferences,
#                                       cache and the private GIR bundle
#   scripts/install.sh --help
set -euo pipefail

APP_ID="dev.wrecht.Deskboard"
APP_NAME="Deskboard"

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
BASE="$DATA_HOME/deskboard"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$DATA_HOME/applications"
ICONS_ROOT="$DATA_HOME/icons/hicolor"
ICONS_DIR="$ICONS_ROOT/scalable/apps"

if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OFF=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_OFF=""
fi

say() { printf '%s\n' "$*"; }
ok() { printf '  %s✓%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
die() { printf '\n%sError:%s %b\n\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Deskboard installer

Usage:
  scripts/install.sh                    install or update into ~/.local
  scripts/install.sh --uninstall        remove the application and launcher
  scripts/install.sh --uninstall --purge
                                        also remove cookies, preferences, cache
  scripts/install.sh --help

The installer never needs root. It copies the application to
${XDG_DATA_HOME:-~/.local/share}/deskboard, creates ~/.local/bin/deskboard,
and installs the desktop entry and icon for the current user.
EOF
}

resolve_script_dir() {
  local src="${BASH_SOURCE[0]}" dir
  while [ -L "$src" ]; do
    dir="$(cd -P -- "$(dirname -- "$src")" >/dev/null 2>&1 && pwd)"
    src="$(readlink -- "$src")"
    case "$src" in
      /*) ;;
      *) src="$dir/$src" ;;
    esac
  done
  cd -P -- "$(dirname -- "$src")" >/dev/null 2>&1 && pwd
}

SCRIPT_DIR="$(resolve_script_dir)"
ROOT="$(cd -P -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"

# Guard against a mis-set XDG_DATA_HOME pointing into the source checkout: a
# copy or (worse) an uninstall there could overwrite or delete the repository.
if [ "$ROOT" != "$BASE" ]; then
  case "$BASE/" in
    "$ROOT"/*)
      die "Refusing to operate inside the source checkout ($ROOT).

  BASE resolves to $BASE. Unset XDG_DATA_HOME (or point it at your real data
  directory) and run the installer again."
      ;;
  esac
fi

DO_UNINSTALL=0
DO_PURGE=0
for argument in "$@"; do
  case "$argument" in
    -h|--help)
      usage
      exit 0
      ;;
    --uninstall|-u)
      DO_UNINSTALL=1
      ;;
    --purge)
      DO_PURGE=1
      ;;
    *)
      die "Unknown argument: $argument (try --help)"
      ;;
  esac
done
if [ "$DO_PURGE" -eq 1 ] && [ "$DO_UNINSTALL" -eq 0 ]; then
  die "--purge is only valid together with --uninstall"
fi

# A checkout installed as the data directory (BASE == ROOT) must not be treated
# as an installed copy during uninstall; .git is the reliable tell.
if [ "$DO_UNINSTALL" -eq 1 ] && [ "$ROOT" = "$BASE" ] && [ -d "$ROOT/.git" ]; then
  die "Refusing to uninstall a source checkout ($ROOT).

  Run the uninstaller from the checkout with a normal XDG_DATA_HOME, or delete
  the copy you created manually."
fi

# Every path we create or remove must be absolute, otherwise `cd` or a weird
# environment variable could turn a cleanup into an accident.
for variable_name in HOME DATA_HOME CONFIG_HOME CACHE_HOME; do
  variable_value="${!variable_name}"
  case "$variable_value" in
    /*) ;;
    *) die "$variable_name must be an absolute path (got: $variable_value)" ;;
  esac
done

safe_rm() {
  local path="$1" candidate root allowed=0
  case "$path" in
    *"/../"*|*"/.."|"..") die "Refusing to remove a path containing '..': $path" ;;
  esac
  if [ ! -e "$path" ] && [ ! -L "$path" ]; then
    return 0
  fi
  for root in "$HOME" "$DATA_HOME" "$CONFIG_HOME" "$CACHE_HOME"; do
    [ -n "$root" ] || continue
    case "$path" in
      "$root"/*) allowed=1 ;;
    esac
  done
  [ "$allowed" -eq 1 ] || die "Refusing to remove an unexpected path: $path"
  for root in "$HOME" "$DATA_HOME" "$CONFIG_HOME" "$CACHE_HOME"; do
    [ "$path" = "$root" ] && die "Refusing to remove directory root: $path"
  done
  rm -rf -- "$path"
}

refresh_caches() {
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
  fi
  if command -v gtk-update-icon-cache >/dev/null 2>&1 && [ -d "$ICONS_ROOT" ]; then
    gtk-update-icon-cache -f -t "$ICONS_ROOT" >/dev/null 2>&1 || true
  fi
}

print_gtk_hint() {
  if ! GI_TYPELIB_PATH="$BASE/gir${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}" \
    "${DESKBOARD_PYTHON:-python3}" -c \
    'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' \
    >/dev/null 2>&1; then
    warn "GTK 4 typelibs are not importable yet."
    say "      Run: $ROOT/scripts/bootstrap-gir.sh"
  fi
}

# --------------------------------------------------------------------------- #
# Uninstall
# --------------------------------------------------------------------------- #
if [ "$DO_UNINSTALL" -eq 1 ]; then
  say ""
  say "Uninstalling $APP_NAME"
  safe_rm "$BIN_DIR/deskboard"
  safe_rm "$APPS_DIR/deskboard.desktop"
  safe_rm "$ICONS_DIR/$APP_ID.svg"
  safe_rm "$BASE/deskboard"
  safe_rm "$BASE/data"
  safe_rm "$BASE/assets"
  safe_rm "$BASE/scripts"
  safe_rm "$BASE/README.md"
  safe_rm "$BASE/pyproject.toml"
  safe_rm "$BASE/deskboard.desktop"
  safe_rm "$BASE/.gitignore"
  refresh_caches
  ok "Removed the launcher, desktop entry, icon and application files."

  if [ "$DO_PURGE" -eq 1 ]; then
    safe_rm "$BASE/cookies.json"
    safe_rm "$BASE/gir"
    safe_rm "$CONFIG_HOME/deskboard"
    safe_rm "$CACHE_HOME/deskboard"
    rmdir "$BASE" >/dev/null 2>&1 || true
    ok "Removed session cookies, preferences, cache and the GIR bundle."
    say ""
  else
    say ""
    say "Kept your data:"
    [ -e "$BASE/cookies.json" ] && say "  • session cookies: $BASE/cookies.json"
    [ -d "$BASE/gir" ] && say "  • GTK typelibs:    $BASE/gir"
    [ -d "$CONFIG_HOME/deskboard" ] && say "  • preferences:     $CONFIG_HOME/deskboard"
    [ -d "$CACHE_HOME/deskboard" ] && say "  • image cache:     $CACHE_HOME/deskboard"
    say ""
    say "  To remove those too: $ROOT/scripts/install.sh --uninstall --purge"
    say ""
  fi
  exit 0
fi

# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #
if [ "$(id -u)" -eq 0 ]; then
  warn "Running as root. Deskboard is designed to install into a normal user's ~/.local."
fi

for required in \
  "$ROOT/deskboard/__main__.py" \
  "$ROOT/data/fixtures" \
  "$ROOT/assets/icon.svg" \
  "$ROOT/deskboard.desktop" \
  "$ROOT/scripts/env.sh"
do
  [ -e "$required" ] || die "Missing source file: $required

  Run this script from a complete Deskboard checkout."
done

say ""
say "Installing $APP_NAME into $BASE"
mkdir -p "$BASE" "$BIN_DIR" "$APPS_DIR" "$ICONS_DIR" \
  || die "Could not create the XDG directories under $DATA_HOME and $HOME/.local."

if [ "$ROOT" != "$BASE" ]; then
  if command -v tar >/dev/null 2>&1; then
    if ! tar -C "$ROOT" -cf - \
      --exclude='__pycache__' \
      --exclude='*.pyc' \
      --exclude='data/cache' \
      deskboard data assets scripts README.md pyproject.toml deskboard.desktop .gitignore \
      | tar -C "$BASE" -xf -; then
      die "Could not copy the application files into $BASE."
    fi
  else
    # Portable fallback; the __pycache__ directories are harmless.
    if ! cp -a "$ROOT/deskboard" "$ROOT/data" "$ROOT/assets" "$ROOT/scripts" "$BASE/" \
      || ! cp -a "$ROOT/README.md" "$ROOT/pyproject.toml" "$ROOT/deskboard.desktop" \
                 "$ROOT/.gitignore" "$BASE/"; then
      die "Could not copy the application files into $BASE."
    fi
  fi
else
  say "  Already running from the installed copy."
fi
ok "Application files: $BASE"

# Launcher wrapper. `%q` keeps paths with spaces/quotes safe.
base_q="$(printf '%q' "$BASE")"
gir_q="$(printf '%q' "$BASE/gir")"
WRAPPER="$BIN_DIR/deskboard"
wrapper_tmp="$BIN_DIR/.deskboard.tmp.$$"
if ! cat > "$wrapper_tmp" <<EOF
#!/usr/bin/env bash
# Generated by Deskboard scripts/install.sh — safe to delete.
set -euo pipefail
APP_DIR=$base_q
GIR_DIR=$gir_q
if [ -d "\$GIR_DIR" ]; then
  export GI_TYPELIB_PATH="\$GIR_DIR\${GI_TYPELIB_PATH:+:\$GI_TYPELIB_PATH}"
fi
export PYTHONPATH="\$APP_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
# Run from the install directory: with `-m`, sys.path[0] is the cwd, so a
# checkout named "deskboard" in the caller's directory would shadow this copy.
cd "\$APP_DIR"
exec "\${DESKBOARD_PYTHON:-python3}" -m deskboard "\$@"
EOF
then
  rm -f -- "$wrapper_tmp" 2>/dev/null || true
  die "Could not write the launcher at $WRAPPER."
fi
chmod 0755 "$wrapper_tmp"
mv -f -- "$wrapper_tmp" "$WRAPPER" || die "Could not install the launcher at $WRAPPER."
ok "Launcher: $WRAPPER"

# Desktop entry + icon.
DESKTOP_DEST="$APPS_DIR/deskboard.desktop"
cp -f -- "$ROOT/deskboard.desktop" "$DESKTOP_DEST" || die "Could not install the desktop entry."
chmod 0644 "$DESKTOP_DEST"
ok "Desktop entry: $DESKTOP_DEST"

ICON_DEST="$ICONS_DIR/$APP_ID.svg"
cp -f -- "$ROOT/assets/icon.svg" "$ICON_DEST" || die "Could not install the icon."
chmod 0644 "$ICON_DEST"
ok "Icon: $ICON_DEST"

# Record provenance so `--doctor` can detect a stale installed copy.
BUILD_FILE="$BASE/BUILD"
APP_DIGEST=$("${DESKBOARD_PYTHON:-python3}" - "$BASE/deskboard" <<'PYDIGEST'
import hashlib, pathlib, sys
root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
files = sorted(p for p in root.rglob("*")
               if p.is_file() and p.suffix in (".py", ".css") and "__pycache__" not in p.parts)
for path in files:
    digest.update(str(path.relative_to(root)).encode())
    digest.update(path.read_bytes())
print(digest.hexdigest())
PYDIGEST
)
GIT_COMMIT=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo "unknown")
{
  echo "source=$ROOT"
  echo "commit=$GIT_COMMIT"
  echo "installed=$(date -Is)"
  echo "digest=$APP_DIGEST"
} > "$BUILD_FILE" || warn "Could not write $BUILD_FILE"
ok "Build record: $BUILD_FILE (commit $GIT_COMMIT)"

if command -v desktop-file-validate >/dev/null 2>&1; then
  if ! desktop-file-validate "$DESKTOP_DEST" >/dev/null 2>&1; then
    warn "desktop-file-validate reported a problem with $DESKTOP_DEST"
  fi
fi
refresh_caches

say ""
case ":$PATH:" in
  *":$BIN_DIR:"*)
    ok "Launcher directory is on PATH."
    ;;
  *)
    warn "$BIN_DIR is not on your PATH."
    say "      Add this to your shell profile:"
    say "        export PATH=\"\$HOME/.local/bin:\$PATH\""
    ;;
esac

print_gtk_hint

say ""
say "Next steps"
say "  1. Check the installation and connectivity:"
say "       deskboard --doctor"
say "  2. Try it offline with bundled sample data:"
say "       deskboard --demo"
say "  3. Start Deskboard:"
say "       deskboard"
say ""
say "Signing in"
say "  Tumblr has no public OAuth application registration for third-party"
say "  clients, so Deskboard uses your own browser session:"
say "    • open Settings → Account and import Firefox/Chromium, or paste a"
say "      Cookie: header from the browser dev tools;"
say "    • or run:  deskboard --import-cookies cookies.txt"
say "  Cookies are stored with mode 0600 in $BASE/cookies.json and are only"
say "  sent to www.tumblr.com. They never leave this machine."
say ""
say "Uninstall"
say "  $ROOT/scripts/install.sh --uninstall"
say ""
