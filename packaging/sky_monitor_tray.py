"""Entry point of the windowed program, sky-monitor-tray.exe: the tray with the product's settings."""

import sys

from skymonitor.cli import main

if __name__ == "__main__":
    sys.exit(main(["tray", *sys.argv[1:]]))
