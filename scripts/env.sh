# Source me: . scripts/env.sh
# Makes GTK4 + libadwaita typelibs visible to the system PyGObject even when the
# distro package is missing (Kali ships libgtk-4 but not gir1.2-gtk-4.0).
DESKBOARD_GIR="$HOME/.local/share/deskboard/gir"
if [ -d "$DESKBOARD_GIR" ]; then
  case ":$GI_TYPELIB_PATH:" in
    *":$DESKBOARD_GIR:"*) ;;  # already present (idempotent)
    *) export GI_TYPELIB_PATH="$DESKBOARD_GIR${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}" ;;
  esac
fi
case ":$PYTHONPATH:" in
  *":$PWD:"*) ;;
  *) export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}" ;;
esac
