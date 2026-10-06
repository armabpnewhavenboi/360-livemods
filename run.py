"""Entry point for the desktop app (and for PyInstaller)."""
import sys


def _attach_console() -> None:
    """The packaged app is a windowed exe; give command-line use a console to print to."""
    if sys.platform == "win32" and sys.stdout is None:
        try:
            import ctypes
            if ctypes.windll.kernel32.AttachConsole(-1) or ctypes.windll.kernel32.AllocConsole():
                sys.stdout = open("CONOUT$", "w", buffering=1)
                sys.stderr = sys.stdout
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _attach_console()
        from livemods.cli import main
    else:
        from livemods.gui import main
    raise SystemExit(main())
