"""Application entry point for the Alpine Recording Station."""

from __future__ import annotations

import argparse
import logging
import sys

from PyQt6.QtWidgets import QApplication

from app.audio_engine import AudioEngine
from app.config_manager import ConfigManager
from app.samba_manager import SambaManager
from app.ui.main_window import MainWindow


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Alpine Recording Station - Single Track USB Audio Recorder with Studio Talkback & Samba Share"
    )
    parser.add_argument(
        "--windowed",
        action="store_true",
        help="Launch in a resizable window instead of fullscreen kiosk mode.",
    )
    parser.add_argument(
        "--storage-dir",
        default=None,
        help="Override persistent WAV storage directory (default: /storage).",
    )
    parser.add_argument(
        "--config-dir",
        default=None,
        help="Override configuration directory (default: /etc/alpine-recorder).",
    )
    parser.add_argument(
        "--simulate-audio",
        action="store_true",
        help="Provide a simulated USB audio interface when no hardware is attached.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = parse_args(argv)

    config_manager = ConfigManager(
        storage_dir=args.storage_dir,
        etc_config_dir=args.config_dir,
    )
    audio_engine = AudioEngine(simulate_if_empty=args.simulate_audio)
    samba_manager = SambaManager(config_manager=config_manager)

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("Alpine Recording Station")

    window = MainWindow(
        config_manager=config_manager,
        audio_engine=audio_engine,
        samba_manager=samba_manager,
        fullscreen=not args.windowed,
    )
    window.show()
    return qt_app.exec()


if __name__ == "__main__":
    sys.exit(main())
