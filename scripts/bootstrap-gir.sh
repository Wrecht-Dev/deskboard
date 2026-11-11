#!/usr/bin/env bash
#
# Deskboard — private GIR typelib bootstrap.
#
# Some distributions ship the GTK 4 shared library but not the PyGObject
# typelibs (Kali, for example, has libgtk-4-1 but no gir1.2-gtk-4.0).  Installing
# system packages would need root, so this script downloads the Debian packages
# with `apt-get download`, unpacks them with `dpkg-deb -x`, and keeps the
# resulting typelibs under the user's own data directory.
#
# It is safe to run repeatedly: if GTK 4 + libadwaita already import, it exits
# immediately, and a working private bundle is reused.
#
# Usage:
#   scripts/bootstrap-gir.sh          install if needed
#   scripts/bootstrap-gir.sh --check  only report whether GTK 4 is usable
#   scripts/bootstrap-gir.sh --help
#
# Environment overrides:
#   DESKBOARD_PYTHON     python interpreter (default: python3)
#   DESKBOARD_GIR_DIR    typelib destination (default: ~/.local/share/deskboard/gir)
#   DESKBOARD_APT_GET    apt-get command (default: apt-get)
#   TMPDIR               scratch directory for .deb downloads
set -euo pipefail

PYTHON="${DESKBOARD_PYTHON:-python3}"
BASE="${XDG_DATA_HOME:-$HOME/.local/share}/deskboard"
GIR_DIR="${DESKBOARD_GIR_DIR:-$BASE/gir}"
APT_GET="${DESKBOARD_APT_GET:-apt-get}"
PACKAGES=(gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-graphene-1.0)

if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OFF=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_OFF=""
fi

say() { printf '%s\n' "$*"; }
ok() { printf '  %s✓%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
die() {
  printf '\n%sError:%s %b\n\n' "$C_ERR" "$C_OFF" "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Deskboard GIR bootstrap

Downloads the GTK 4 / libadwaita / Graphene typelibs into a private directory
without root, using apt-get download + dpkg-deb -x.

Usage:
  scripts/bootstrap-gir.sh          install the typelibs if they are missing
  scripts/bootstrap-gir.sh --check  only check whether GTK 4 imports
  scripts/bootstrap-gir.sh --help   this text

The bundle goes to ${XDG_DATA_HOME:-~/.local/share}/deskboard/gir and is
activated by scripts/env.sh, scripts/run.sh and the installed launcher.
EOF
}

CHECK_ONLY=0
case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
  --check)
    CHECK_ONLY=1
    shift
    ;;
  "")
    ;;
  *)
    die "Unknown argument: $1 (try --help)"
    ;;
esac
if [ "$#" -ne 0 ]; then
  die "Unexpected extra arguments: $*"
fi

# The snippet used for every probe. It imports the namespaces Deskboard paints
# with and touches no display, so it is safe in an SSH session.
read -r -d '' PROBE <<'PY' || true
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, Gsk, Graphene
assert Gtk.get_major_version() == 4
PY

# check_import [typelib-path]
# Returns 0 when Gtk 4 + Adw can be imported. With an argument, that directory is
# prepended to GI_TYPELIB_PATH for the probe only.
check_import() {
  local extra="${1:-}"
  if [ -n "$extra" ]; then
    GI_TYPELIB_PATH="$extra${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}" \
      "$PYTHON" -c "$PROBE" >/dev/null 2>&1
  else
    "$PYTHON" -c "$PROBE" >/dev/null 2>&1
  fi
}

import_error() {
  local extra="${1:-}"
  if [ -n "$extra" ]; then
    GI_TYPELIB_PATH="$extra${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}" \
      "$PYTHON" -c "$PROBE" 2>&1 || true
  else
    "$PYTHON" -c "$PROBE" 2>&1 || true
  fi
}

say ""
say "Deskboard GIR bootstrap"
say "  python : $("$PYTHON" -V 2>&1 || true)"
say "  bundle : $GIR_DIR"
say ""

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  die "Python 3 was not found ($PYTHON). Install python3 and python3-gi, then retry."
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  die "$PYTHON is too old; Deskboard needs Python 3.10 or newer."
fi

if ! "$PYTHON" -c 'import gi' >/dev/null 2>&1; then
  die "PyGObject is not installed for $PYTHON.

  Debian / Ubuntu / Kali:
      sudo apt install python3-gi

  Fedora:
      sudo dnf install python3-gobject

  Arch:
      sudo pacman -S python-gobject

  Then re-run this script."
fi

# 1. System typelibs already work.
if check_import; then
  ok "GTK 4 and libadwaita already import — nothing to download."
  say ""
  exit 0
fi

# 2. A previously bootstrapped bundle works.
if [ -d "$GIR_DIR" ] && check_import "$GIR_DIR"; then
  ok "Deskboard's private typelib bundle is already complete."
  say "  Activate it with: . scripts/env.sh"
  say "  (or run scripts/run.sh, which sources it automatically)"
  say ""
  exit 0
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  warn "GTK 4 typelibs are not importable yet."
  say "  Run scripts/bootstrap-gir.sh (without --check) to install them privately."
  say ""
  exit 1
fi

# 3. Bootstrap with apt. This intentionally does not call sudo or write outside
#    the user's XDG data directory.
if ! command -v "$APT_GET" >/dev/null 2>&1 || ! command -v dpkg-deb >/dev/null 2>&1; then
  die "This bootstrap uses apt-get and dpkg-deb (Debian/Ubuntu/Kali).
  Neither command was found, so install the typelibs with your package manager:

      Debian / Ubuntu / Kali:  sudo apt install ${PACKAGES[*]}
      Fedora:                  sudo dnf install gtk4 libadwaita graphene
      Arch:                    sudo pacman -S gtk4 libadwaita graphene

  Alternatively point DESKBOARD_GIR_DIR at a directory that already contains
  Gtk-4.0.typelib, Adw-1.typelib and Graphene-1.0.typelib."
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/deskboard-gir.XXXXXX")" \
  || die "Could not create a temporary directory."
cleanup() {
  case "$TMP_DIR" in
    "${TMPDIR:-/tmp}"/deskboard-gir.*) rm -rf -- "$TMP_DIR" ;;
    *) warn "Leaving unexpected temporary path in place: $TMP_DIR" ;;
  esac
}
trap cleanup EXIT INT TERM

say "Fetching GIR packages (no root required):"
for package in "${PACKAGES[@]}"; do
  say "  • $package"
done
say ""

if ! (
  cd "$TMP_DIR"
  "$APT_GET" \
    -o Acquire::http::Timeout=30 \
    -o Acquire::https::Timeout=30 \
    -o Acquire::Retries=1 \
    download "${PACKAGES[@]}"
) >"$TMP_DIR/apt.log" 2>&1; then
  sed 's/^/    /' "$TMP_DIR/apt.log" >&2 || true
  die "Could not download the GIR packages.

  The apt repositories were unreachable, or the package names changed.

  What to try:
    • Check your network / VPN / proxy settings.
    • Refresh package lists as an administrator:  sudo apt-get update
    • Or install them system-wide:  sudo apt install ${PACKAGES[*]}
    • Or download the .deb files elsewhere and extract them by hand:
        dpkg-deb -x gir1.2-gtk-4.0_*.deb \"$TMP_DIR/extract\"
        cp \"$TMP_DIR\"/extract/usr/lib/*/girepository-1.0/*.typelib \"$GIR_DIR\""
fi

shopt -s nullglob
debs=("$TMP_DIR"/*.deb)
shopt -u nullglob
if [ "${#debs[@]}" -eq 0 ]; then
  die "apt-get reported success but no .deb files appeared in $TMP_DIR.

  Try again, or install the packages system-wide:
      sudo apt install ${PACKAGES[*]}"
fi

say "Extracting ${#debs[@]} package(s)…"
for deb in "${debs[@]}"; do
  dpkg-deb -x "$deb" "$TMP_DIR/extract" \
    || die "Could not extract $deb. The download may be corrupt; try again."
done

mkdir -p -- "$GIR_DIR" || die "Could not create $GIR_DIR"

count=0
while IFS= read -r -d '' typelib; do
  cp -f -- "$typelib" "$GIR_DIR/" || die "Could not copy $typelib to $GIR_DIR"
  count=$((count + 1))
done < <(find "$TMP_DIR/extract" -type f -name '*.typelib' -print0)

if [ "$count" -eq 0 ]; then
  die "The downloaded packages contained no typelib files.

  This usually means the distribution package layout changed. Please report it,
  or install the typelibs system-wide: sudo apt install ${PACKAGES[*]}"
fi

if ! check_import "$GIR_DIR"; then
  error_text="$(import_error "$GIR_DIR")"
  die "The typelibs were copied to $GIR_DIR, but GTK 4 still fails to import:

$error_text

  The installed libgtk shared library may be older than the typelib package.
  Either install matching system packages:
      sudo apt install ${PACKAGES[*]}
  or retry after: sudo apt-get update && sudo apt-get upgrade"

fi

version="$(
  GI_TYPELIB_PATH="$GIR_DIR${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}" \
    "$PYTHON" -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1");
from gi.repository import Gtk, Adw
print(f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()} · libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}")' 2>/dev/null || true
)"

ok "Installed $count typelib(s) into $GIR_DIR"
if [ -n "$version" ]; then
  ok "$version"
fi
say ""
say "Activate the bundle with one of:"
say "  . scripts/env.sh          # current shell"
say "  scripts/run.sh            # launcher (sources env automatically)"
say "  scripts/install.sh        # permanent launcher + desktop entry"
say ""
