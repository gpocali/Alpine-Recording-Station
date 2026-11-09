"""Fullscreen Storage File Manager dialog for browsing, playing, renaming, and deleting recordings and project folders."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QKeyEvent, QPalette, QResizeEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.audio_engine import AudioDeviceCapabilities, AudioEngine
from app.config_manager import (
    ConfigManager,
    ProjectFolderSummary,
    RecordingEntry,
    StorageStatus,
    format_remaining_time,
    format_storage_bytes,
    sanitize_project_name,
)
from app.ui.on_screen_keyboard import OnScreenKeyboardController
from app.ui.storage_monitor import StorageMonitorWidget
from app.ui.styles import (
    build_main_stylesheet,
    build_playback_button_style,
    build_stop_playback_button_style,
    compute_scale_factor,
    configure_combo_box_popup,
)

logger = logging.getLogger(__name__)


class DeleteFileConfirmDialog(QDialog):
    """Modal confirmation dialog before deleting a single recording file."""

    def __init__(
        self,
        entry: RecordingEntry,
        storage_status: StorageStatus,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.entry = entry
        self.storage_status = storage_status

        self.setWindowTitle("Confirm File Deletion")
        self.setModal(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        if parent is not None and parent.width() >= 480:
            pw, ph = parent.width(), parent.height()
            self._scale = compute_scale_factor(pw, ph)
            self.resize(max(520, int(pw * 0.56)), max(300, int(ph * 0.44)))
        else:
            self._scale = 1.0
            self.resize(560, 320)

        self._build_ui()
        self.setStyleSheet(build_main_stylesheet(self._scale))

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        title_label = QLabel("⚠  Confirm Recording File Deletion")
        title_label.setObjectName("HeaderTitle")
        title_label.setStyleSheet("color: #FF6B7A; font-weight: bold;")
        layout.addWidget(title_label)

        desc_label = QLabel(
            f"Are you sure you want to permanently delete this recording from "
            f"project folder '{self.entry.project_name}/'?"
        )
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet("color: #E6EDF3;")
        layout.addWidget(desc_label)

        # File & Space Impact Card
        info_card = QFrame()
        info_card.setObjectName("CardFrame")
        info_card.setStyleSheet(
            "QFrame#CardFrame { background-color: #0D1014; border: 1px solid #30363D; border-radius: 8px; }"
        )
        card_layout = QVBoxLayout(info_card)
        card_layout.setContentsMargins(14, 10, 14, 10)
        card_layout.setSpacing(6)

        ch_label = "Stereo" if self.entry.channels == 2 else f"{self.entry.channels}ch"
        file_line = QLabel(
            f"File: {self.entry.project_name}/{self.entry.filename}\n"
            f"Format: {self.entry.formatted_duration}  •  "
            f"{self.entry.bit_depth}-bit {self.entry.sample_rate:,} Hz {ch_label}"
        )
        file_line.setWordWrap(True)
        file_line.setStyleSheet("color: #F0F6FC; font-family: monospace; font-weight: bold;")
        card_layout.addWidget(file_line)

        st = self.storage_status
        freed_str = format_storage_bytes(self.entry.size_bytes)
        if st.available and st.total_bytes > 0:
            new_free = min(st.total_bytes, st.free_bytes + self.entry.size_bytes)
            new_used = max(0, st.total_bytes - new_free)
            new_pct = (new_used / st.total_bytes) * 100.0
            gain_sec = (
                float(self.entry.size_bytes) / float(st.byte_rate)
                if st.byte_rate > 0
                else 0.0
            )
            space_txt = (
                f"Space to Clear: {freed_str}  (+{format_remaining_time(gain_sec)} recording time)\n"
                f"Drive Utilization: {st.used_percent:.1f}% → {new_pct:.1f}% used  "
                f"({st.formatted_free} → {format_storage_bytes(new_free)} free)"
            )
        else:
            space_txt = f"Space to Clear: {freed_str}"

        self.space_impact_label = QLabel(space_txt)
        self.space_impact_label.setWordWrap(True)
        self.space_impact_label.setStyleSheet(
            "color: #7EE787; font-family: monospace; font-weight: bold;"
        )
        card_layout.addWidget(self.space_impact_label)
        layout.addWidget(info_card)

        warn_label = QLabel("This file will be permanently removed from /storage and cannot be recovered.")
        warn_label.setWordWrap(True)
        warn_label.setStyleSheet("color: #8B949E;")
        layout.addWidget(warn_label)

        layout.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setToolTip("Keep the file and close [Hotkey: Esc]")
        self.cancel_btn.setDefault(True)
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)

        self.confirm_delete_btn = QPushButton(f"✕ Delete File ({freed_str})")
        self.confirm_delete_btn.setToolTip("Permanently delete this WAV file from /storage")
        self.confirm_delete_btn.setStyleSheet(
            "background-color: #D72638; color: #FFFFFF; border: 2px solid #FF6B7A; font-weight: bold;"
        )
        self.confirm_delete_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.confirm_delete_btn)

        layout.addLayout(btn_row)


class RecursiveProjectConfirmDialog(QDialog):
    """Two-step extra confirmation dialog before recursive project folder cleanup or deletion.

    Step 1 presents the warning, file list preview, and drive utilization impact.
    Step 2 presents the final danger prompt with a mandatory confirmation checkbox
    that must be checked before the final destructive button becomes enabled.
    """

    def __init__(
        self,
        folder_summary: ProjectFolderSummary,
        recordings: List[RecordingEntry],
        storage_status: StorageStatus,
        mode: str = "delete",  # "delete" (delete folder & contents) or "cleanup" (empty files inside folder)
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.folder_summary = folder_summary
        self.recordings = list(recordings)
        self.storage_status = storage_status
        self.mode = "cleanup" if mode == "cleanup" else "delete"
        self._current_step: int = 1
        self._confirmed_recursive: bool = False

        action_title = (
            "Clean Up Project Folder Files"
            if self.mode == "cleanup"
            else "Delete Project Folder && Contents"
        )
        self.setWindowTitle(f"Confirm Recursive Deletion — {action_title}")
        self.setModal(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        if parent is not None and parent.width() >= 480:
            pw, ph = parent.width(), parent.height()
            self._scale = compute_scale_factor(pw, ph)
            self.resize(max(600, int(pw * 0.66)), max(400, int(ph * 0.62)))
        else:
            self._scale = 1.0
            self.resize(660, 440)

        self._build_ui()
        self.setStyleSheet(build_main_stylesheet(self._scale))
        self._configure_checkbox_palette()
        self._update_step_view()

    @property
    def current_step(self) -> int:
        return self._current_step

    @property
    def confirmed_recursive(self) -> bool:
        return self._confirmed_recursive

    def _configure_checkbox_palette(self) -> None:
        chk_fg = QColor("#F0F6FC")
        pal = self.confirm_checkbox.palette()
        for grp in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            pal.setColor(grp, QPalette.ColorRole.WindowText, chk_fg)
            pal.setColor(grp, QPalette.ColorRole.Text, chk_fg)
            pal.setColor(grp, QPalette.ColorRole.ButtonText, chk_fg)
        self.confirm_checkbox.setPalette(pal)
        self.confirm_checkbox.setStyleSheet("color: #F0F6FC; font-weight: bold;")

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        self.step_header_label = QLabel()
        self.step_header_label.setObjectName("HeaderTitle")
        layout.addWidget(self.step_header_label)

        self.active_warning_banner = QLabel(
            f"⚠ ACTIVE PROJECT WARNING: '{self.folder_summary.project_name}/' is currently selected "
            "as your active recording project."
        )
        self.active_warning_banner.setWordWrap(True)
        self.active_warning_banner.setVisible(bool(self.folder_summary.is_active_project))
        self.active_warning_banner.setStyleSheet(
            "background-color: #3B2300; color: #FFDF5D; border: 1px solid #D29922; "
            "border-radius: 6px; padding: 6px 10px; font-weight: bold;"
        )
        layout.addWidget(self.active_warning_banner)

        self.desc_label = QLabel()
        self.desc_label.setWordWrap(True)
        self.desc_label.setStyleSheet("color: #E6EDF3;")
        layout.addWidget(self.desc_label)

        # Impact & Files Preview Card
        impact_card = QFrame()
        impact_card.setObjectName("CardFrame")
        impact_card.setStyleSheet(
            "QFrame#CardFrame { background-color: #0D1014; border: 1px solid #30363D; border-radius: 8px; }"
        )
        impact_layout = QVBoxLayout(impact_card)
        impact_layout.setContentsMargins(14, 10, 14, 10)
        impact_layout.setSpacing(6)

        st = self.storage_status
        freed_bytes = self.folder_summary.total_size_bytes
        freed_str = format_storage_bytes(freed_bytes)
        total_items = self.folder_summary.total_item_count

        if st.available and st.total_bytes > 0:
            new_free = min(st.total_bytes, st.free_bytes + freed_bytes)
            new_used = max(0, st.total_bytes - new_free)
            new_pct = (new_used / st.total_bytes) * 100.0
            gain_sec = (
                float(freed_bytes) / float(st.byte_rate)
                if st.byte_rate > 0
                else 0.0
            )
            impact_text = (
                f"Target Folder: /storage/{self.folder_summary.project_name}/\n"
                f"Files to Remove: {total_items} file(s) ({self.folder_summary.recording_count} WAV recording(s))\n"
                f"Total Space to Clear: {freed_str}  (+{format_remaining_time(gain_sec)} recording time)\n"
                f"Drive Utilization: {st.used_percent:.1f}% → {new_pct:.1f}% used  "
                f"({st.formatted_free} → {format_storage_bytes(new_free)} free)"
            )
        else:
            impact_text = (
                f"Target Folder: /storage/{self.folder_summary.project_name}/\n"
                f"Files to Remove: {total_items} file(s)\n"
                f"Total Space to Clear: {freed_str}"
            )

        self.impact_summary_label = QLabel(impact_text)
        self.impact_summary_label.setWordWrap(True)
        self.impact_summary_label.setStyleSheet(
            "color: #7EE787; font-family: monospace; font-weight: bold;"
        )
        impact_layout.addWidget(self.impact_summary_label)

        if self.recordings:
            preview_names = [f"  • {r.filename} ({r.formatted_size})" for r in self.recordings[:5]]
            if len(self.recordings) > 5:
                preview_names.append(f"  ... and {len(self.recordings) - 5} more recording(s)")
            files_preview_txt = "Recordings inside folder:\n" + "\n".join(preview_names)
        elif total_items > 0:
            files_preview_txt = f"Contains {total_items} non-WAV file(s) ({freed_str})."
        else:
            files_preview_txt = "Folder is currently empty (0 files)."

        self.files_preview_label = QLabel(files_preview_txt)
        self.files_preview_label.setWordWrap(True)
        self.files_preview_label.setStyleSheet("color: #8B949E; font-family: monospace;")
        impact_layout.addWidget(self.files_preview_label)
        layout.addWidget(impact_card)

        # Step 2 Final Confirmation Frame (hidden during Step 1)
        self.step2_frame = QFrame()
        self.step2_frame.setObjectName("Step2DangerFrame")
        self.step2_frame.setStyleSheet(
            "QFrame#Step2DangerFrame { background-color: #240D11; border: 2px solid #FF6B7A; border-radius: 8px; }"
        )
        step2_layout = QVBoxLayout(self.step2_frame)
        step2_layout.setContentsMargins(14, 10, 14, 10)
        step2_layout.setSpacing(8)

        step2_warn = QLabel(
            f"⚠ FINAL EXTRA CONFIRMATION REQUIRED: Recursive deletion of "
            f"'/storage/{self.folder_summary.project_name}/' cannot be undone."
        )
        step2_warn.setWordWrap(True)
        step2_warn.setStyleSheet("color: #FFDF5D; font-weight: bold; border: none;")
        step2_layout.addWidget(step2_warn)

        chk_text = (
            f"I confirm that I want to permanently delete all {total_items} file(s) ({freed_str}) "
            f"inside '{self.folder_summary.project_name}/'."
            if self.mode == "cleanup"
            else f"I confirm that I want to recursively delete folder '{self.folder_summary.project_name}/' "
            f"and all {total_items} file(s) ({freed_str}) inside it."
        )
        self.confirm_checkbox = QCheckBox(chk_text)
        self.confirm_checkbox.toggled.connect(self._on_checkbox_toggled)
        step2_layout.addWidget(self.confirm_checkbox)
        layout.addWidget(self.step2_frame)

        layout.addStretch(1)

        # Bottom Buttons Row
        btn_row = QHBoxLayout()
        self.back_btn = QPushButton("← Back")
        self.back_btn.setToolTip("Return to Step 1 overview")
        self.back_btn.clicked.connect(self._on_back_to_step1)
        btn_row.addWidget(self.back_btn)

        btn_row.addStretch(1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setToolTip("Abort and keep all project files [Hotkey: Esc]")
        self.cancel_btn.setDefault(True)
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)

        self.step1_continue_btn = QPushButton("Continue to Final Confirmation (Step 2) →")
        self.step1_continue_btn.setToolTip(
            "Proceed to the second confirmation step required for recursive deletion"
        )
        self.step1_continue_btn.setStyleSheet(
            "background-color: #9E6A03; color: #FFFFFF; border: 2px solid #F0B72F; font-weight: bold;"
        )
        self.step1_continue_btn.clicked.connect(self.advance_to_final_step)
        btn_row.addWidget(self.step1_continue_btn)

        final_btn_label = (
            f"✕ Permanently Empty Project ({freed_str})"
            if self.mode == "cleanup"
            else f"✕ Permanently Delete Folder && Contents ({freed_str})"
        )
        self.final_confirm_btn = QPushButton(final_btn_label)
        self.final_confirm_btn.setToolTip(
            "Execute permanent recursive deletion after checking the confirmation box above"
        )
        self.final_confirm_btn.setEnabled(False)
        self.final_confirm_btn.setStyleSheet(
            "QPushButton { background-color: #D72638; color: #FFFFFF; border: 2px solid #FF6B7A; font-weight: bold; }"
            "QPushButton:disabled { background-color: #2D2024; color: #6E7681; border: 1px solid #444C56; }"
        )
        self.final_confirm_btn.clicked.connect(self._on_final_confirm_clicked)
        btn_row.addWidget(self.final_confirm_btn)

        layout.addLayout(btn_row)

    def advance_to_final_step(self) -> None:
        """Transition from Step 1 warning to Step 2 final extra confirmation."""
        self._current_step = 2
        self.confirm_checkbox.setChecked(False)
        self._update_step_view()

    def _on_back_to_step1(self) -> None:
        self._current_step = 1
        self.confirm_checkbox.setChecked(False)
        self._update_step_view()

    def _on_checkbox_toggled(self, checked: bool) -> None:
        self.final_confirm_btn.setEnabled(self._current_step == 2 and bool(checked))

    def _on_final_confirm_clicked(self) -> None:
        if self._current_step != 2 or not self.confirm_checkbox.isChecked():
            return
        self._confirmed_recursive = True
        self.accept()

    def _update_step_view(self) -> None:
        is_step2 = self._current_step == 2
        self.step2_frame.setVisible(is_step2)
        self.back_btn.setVisible(is_step2)
        self.step1_continue_btn.setVisible(not is_step2)
        self.final_confirm_btn.setVisible(is_step2)
        self.final_confirm_btn.setEnabled(is_step2 and self.confirm_checkbox.isChecked())

        fname = self.folder_summary.project_name
        if not is_step2:
            if self.mode == "cleanup":
                self.step_header_label.setText(
                    f"⚠  Step 1 of 2: Clean Up Project Folder '{fname}/'"
                )
                self.desc_label.setText(
                    f"You are requesting to delete all recordings and files inside "
                    f"'/storage/{fname}/' while keeping the empty project folder. "
                    "Please review the files and disk space below before proceeding to the final confirmation."
                )
            else:
                self.step_header_label.setText(
                    f"⚠  Step 1 of 2: Delete Project Folder '{fname}/'"
                )
                self.desc_label.setText(
                    f"You are requesting a recursive deletion of project folder '/storage/{fname}/' "
                    "and all recordings inside it. Please review the contents and space impact below "
                    "before proceeding to the final confirmation."
                )
            self.step_header_label.setStyleSheet("color: #FFDF5D; font-weight: bold;")
        else:
            if self.mode == "cleanup":
                self.step_header_label.setText(
                    f"⚠  Step 2 of 2: FINAL CONFIRMATION — Empty '{fname}/'"
                )
                self.desc_label.setText(
                    f"Check the confirmation box below to permanently delete all files inside '/storage/{fname}/'."
                )
            else:
                self.step_header_label.setText(
                    f"⚠  Step 2 of 2: FINAL CONFIRMATION — Delete Folder '{fname}/'"
                )
                self.desc_label.setText(
                    f"Check the confirmation box below to permanently delete '/storage/{fname}/' and all of its contents."
                )
            self.step_header_label.setStyleSheet("color: #FF6B7A; font-weight: bold;")


class StorageFileManagerDialog(QDialog):
    """Fullscreen Storage File Manager for browsing, playing, renaming, and deleting files and project folders."""

    def __init__(
        self,
        config_manager: ConfigManager,
        audio_engine: AudioEngine,
        output_device: Optional[AudioDeviceCapabilities] = None,
        rename_dialog_factory: Optional[Callable[[ConfigManager, Path, QWidget], QDialog]] = None,
        fullscreen: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.config_manager = config_manager
        self.audio_engine = audio_engine
        self._output_device = output_device
        self._rename_dialog_factory = rename_dialog_factory
        self._fullscreen_requested = bool(
            fullscreen and (parent is None or parent.isFullScreen())
        )

        self.setWindowTitle("Storage File Manager — All Projects && Drive Utilization")
        self.setModal(True)
        self.setMinimumSize(640, 420)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        if parent is not None and parent.width() >= 480:
            pw, ph = parent.width(), parent.height()
            self._scale = compute_scale_factor(pw, ph)
            self.resize(pw, ph)
            self.move(parent.geometry().topLeft())
        else:
            self._scale = 1.0
            self.resize(1120, 680)

        self._project_summaries: List[ProjectFolderSummary] = []
        self._all_recordings: List[RecordingEntry] = []
        self._displayed_recordings: List[RecordingEntry] = []
        self._selected_project_filter: str = "*"  # "*" means All Projects
        self._session_cleared_bytes: int = 0
        self._folders_pane_visible: bool = True
        self._last_storage_refresh_mono: float = 0.0

        self.osk_controller = OnScreenKeyboardController(
            is_enabled=lambda: bool(self.config_manager.settings.on_screen_keyboard),
            parent=self,
        )

        self._build_ui()
        self._apply_responsive_scaling()
        self.osk_controller.register_line_edit(
            self.search_edit,
            title="Filter Recordings",
            hint="Filter by filename or project name across /storage",
        )
        self.osk_controller.attach_to_container(self)

        self.refresh_all()

        # UI timer for live playback progress and storage telemetry updates
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(100)
        self._ui_timer.timeout.connect(self._on_ui_tick)
        self._ui_timer.start()

    @property
    def session_cleared_bytes(self) -> int:
        """Total bytes freed by file and folder deletions during this File Manager session."""
        return self._session_cleared_bytes

    @property
    def selected_project_filter(self) -> str:
        """Currently selected project filter ('*' for All Projects, or project folder name)."""
        return self._selected_project_filter

    # -------------------------------------------------------------------------
    # UI Construction
    # -------------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(8)

        # 1. Top Header Bar: Title, Space Cleared Session Badge, Fullscreen List Toggle, Refresh, Close
        top_frame = QFrame()
        top_frame.setStyleSheet(
            "QFrame { background-color: #161B22; border: 1px solid #30363D; border-radius: 8px; }"
        )
        top_layout = QHBoxLayout(top_frame)
        top_layout.setContentsMargins(12, 6, 12, 6)
        top_layout.setSpacing(10)

        self.title_label = QLabel("▤  /storage File Manager — All Projects && Recordings")
        self.title_label.setObjectName("HeaderTitle")
        self.title_label.setStyleSheet("border: none; color: #F0F6FC; font-weight: bold;")
        top_layout.addWidget(self.title_label)

        top_layout.addStretch(1)

        self.space_cleared_badge = QLabel("SPACE CLEARED: 0 B")
        self.space_cleared_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.space_cleared_badge.setToolTip(
            "Total disk space freed on /storage during this File Manager session"
        )
        top_layout.addWidget(self.space_cleared_badge)

        self.toggle_folders_pane_btn = QPushButton("⇄ Fullscreen File List")
        self.toggle_folders_pane_btn.setToolTip(
            "Toggle the Project Folders sidebar so the file list uses 100% of the screen width [Hotkey: F11]"
        )
        self.toggle_folders_pane_btn.clicked.connect(self.toggle_folders_sidebar)
        top_layout.addWidget(self.toggle_folders_pane_btn)

        self.refresh_btn = QPushButton("↻ Refresh")
        self.refresh_btn.setToolTip("Rescan /storage project folders, files, and drive utilization [Hotkey: F5]")
        self.refresh_btn.clicked.connect(self.refresh_all)
        top_layout.addWidget(self.refresh_btn)

        self.close_btn = QPushButton("✓ Done && Close")
        self.close_btn.setToolTip("Close File Manager and return to Studio Deck [Hotkey: Esc]")
        self.close_btn.setStyleSheet(
            "background-color: #1F6FEB; color: #FFFFFF; font-weight: bold;"
        )
        self.close_btn.clicked.connect(self.accept)
        top_layout.addWidget(self.close_btn)

        root.addWidget(top_frame)

        # 2. Drive Utilization Monitor (Smooth Capacity Pill + Free Space + Recording Time Remaining)
        self.storage_monitor = StorageMonitorWidget(parent=self)
        root.addWidget(self.storage_monitor)

        # 3. Main Split Content: Project Folders Sidebar (Left) + Full-Screen File List (Right)
        content_row = QHBoxLayout()
        content_row.setSpacing(10)

        # 3A. Left Pane: Project Folders & Recursive Cleanup / Delete Controls
        self.folders_frame = QFrame()
        self.folders_frame.setObjectName("FoldersPane")
        self.folders_frame.setStyleSheet(
            "QFrame#FoldersPane { background-color: #161B22; border: 1px solid #30363D; border-radius: 10px; }"
        )
        folders_layout = QVBoxLayout(self.folders_frame)
        folders_layout.setContentsMargins(10, 8, 10, 8)
        folders_layout.setSpacing(6)

        folders_hdr_row = QHBoxLayout()
        folders_hdr = QLabel("PROJECT FOLDERS")
        folders_hdr.setObjectName("SectionHeader")
        folders_hdr_row.addWidget(folders_hdr)
        folders_hdr_row.addStretch(1)

        self.cleanup_empty_btn = QPushButton("↺ Clean Empty")
        self.cleanup_empty_btn.setToolTip(
            "Remove all empty project folders in /storage that contain 0 files"
        )
        self.cleanup_empty_btn.clicked.connect(self._on_cleanup_empty_folders_clicked)
        folders_hdr_row.addWidget(self.cleanup_empty_btn)
        folders_layout.addLayout(folders_hdr_row)

        self.project_list = QListWidget()
        self.project_list.setToolTip(
            "Select 'All Projects' to view files across every project, or select a specific project folder "
            "to filter files, clean up takes, or delete the folder."
        )
        self.project_list.currentRowChanged.connect(self._on_project_selection_changed)
        folders_layout.addWidget(self.project_list, stretch=1)

        # Project Folder Actions
        proj_actions_row1 = QHBoxLayout()
        proj_actions_row1.setSpacing(6)

        self.set_active_project_btn = QPushButton("✓ Set Active Project")
        self.set_active_project_btn.setToolTip(
            "Set the selected folder as the active recording project for new takes"
        )
        self.set_active_project_btn.clicked.connect(self._on_set_active_project_clicked)
        proj_actions_row1.addWidget(self.set_active_project_btn)
        folders_layout.addLayout(proj_actions_row1)

        proj_actions_row2 = QHBoxLayout()
        proj_actions_row2.setSpacing(6)

        self.cleanup_project_btn = QPushButton("◈ Empty Folder...")
        self.cleanup_project_btn.setToolTip(
            "Delete all recordings inside the selected project folder while keeping the folder itself "
            "(requires 2-step confirmation)"
        )
        self.cleanup_project_btn.setStyleSheet(
            "QPushButton { background-color: #3B2300; color: #FFDF5D; border: 1px solid #D29922; font-weight: bold; }"
            "QPushButton:hover { background-color: #5A3000; }"
            "QPushButton:disabled { background-color: #1A1E24; color: #6E7681; border: 1px solid #21262D; }"
        )
        self.cleanup_project_btn.clicked.connect(self._on_cleanup_project_folder_clicked)
        proj_actions_row2.addWidget(self.cleanup_project_btn)

        self.delete_project_btn = QPushButton("✕ Delete Folder...")
        self.delete_project_btn.setToolTip(
            "Recursively delete the selected project folder and all files inside it "
            "(requires 2-step extra confirmation)"
        )
        self.delete_project_btn.setStyleSheet(
            "QPushButton { background-color: #8E1523; color: #FFFFFF; border: 1px solid #FF6B7A; font-weight: bold; }"
            "QPushButton:hover { background-color: #B01B2B; }"
            "QPushButton:disabled { background-color: #1A1E24; color: #6E7681; border: 1px solid #21262D; }"
        )
        self.delete_project_btn.clicked.connect(self._on_delete_project_folder_clicked)
        proj_actions_row2.addWidget(self.delete_project_btn)

        folders_layout.addLayout(proj_actions_row2)
        content_row.addWidget(self.folders_frame, stretch=3)

        # 3B. Right / Main Pane: Fullscreen Recordings List + Filter/Sort + Play/Rename/Delete
        self.files_frame = QFrame()
        self.files_frame.setObjectName("FilesPane")
        self.files_frame.setStyleSheet(
            "QFrame#FilesPane { background-color: #161B22; border: 1px solid #30363D; border-radius: 10px; }"
        )
        files_layout = QVBoxLayout(self.files_frame)
        files_layout.setContentsMargins(12, 8, 12, 8)
        files_layout.setSpacing(6)

        # Filter & Sort Toolbar
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)

        self.files_scope_label = QLabel("All Projects")
        self.files_scope_label.setStyleSheet("color: #79C0FF; font-weight: bold;")
        filter_row.addWidget(self.files_scope_label)

        filter_row.addStretch(1)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter by filename or project...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(lambda _t: self._rebuild_files_list())
        filter_row.addWidget(self.search_edit, stretch=1)

        self.search_osk_btn = QPushButton("⌨")
        self.search_osk_btn.setToolTip("Open On-Screen Keyboard to filter files")
        self.search_osk_btn.setVisible(bool(self.config_manager.settings.on_screen_keyboard))
        self.search_osk_btn.clicked.connect(
            lambda: self.osk_controller.open_for_line_edit(self.search_edit)
        )
        filter_row.addWidget(self.search_osk_btn)

        sort_lbl = QLabel("Sort:")
        sort_lbl.setStyleSheet("color: #8B949E;")
        filter_row.addWidget(sort_lbl)

        self.sort_combo = QComboBox()
        self.sort_combo.addItem("Newest First", "newest")
        self.sort_combo.addItem("Oldest First", "oldest")
        self.sort_combo.addItem("Largest Size First", "size_desc")
        self.sort_combo.addItem("Smallest Size First", "size_asc")
        self.sort_combo.addItem("Filename (A → Z)", "name_asc")
        self.sort_combo.addItem("Project Name (A → Z)", "project_asc")
        self.sort_combo.setToolTip("Sort recordings list (use 'Largest Size First' to spot big files)")
        self.sort_combo.currentIndexChanged.connect(lambda _i: self._rebuild_files_list())
        filter_row.addWidget(self.sort_combo)

        files_layout.addLayout(filter_row)

        # Full-screen Recordings List Widget
        self.files_list = QListWidget()
        self.files_list.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.files_list.setToolTip(
            "Recordings across /storage. Double-click or press [P] to play, "
            "[N] to rename, or [Del] to delete with confirmation."
        )
        self.files_list.itemDoubleClicked.connect(
            lambda _item: self._on_play_pause_clicked(force_new=True)
        )
        self.files_list.currentRowChanged.connect(
            lambda _row: self._update_action_buttons_state()
        )
        files_layout.addWidget(self.files_list, stretch=1)

        # Bottom File Actions & Playback Bar
        actions_row = QHBoxLayout()
        actions_row.setSpacing(8)

        self.play_btn = QPushButton("▶ Play / Pause")
        self.play_btn.setToolTip("Play or pause the selected recording [Hotkey: P or Space]")
        self.play_btn.clicked.connect(self._on_play_pause_clicked)
        actions_row.addWidget(self.play_btn)

        self.stop_play_btn = QPushButton("■ Stop")
        self.stop_play_btn.setToolTip("Stop audio playback [Hotkey: S]")
        self.stop_play_btn.clicked.connect(self._on_stop_playback_clicked)
        actions_row.addWidget(self.stop_play_btn)

        self.playback_status_label = QLabel("")
        self.playback_status_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.playback_status_label.setStyleSheet(
            "color: #8B949E; font-family: monospace;"
        )
        actions_row.addWidget(self.playback_status_label, stretch=1)

        self.rename_file_btn = QPushButton("✎ Rename File")
        self.rename_file_btn.setToolTip(
            "Rename the selected recording file inside its project folder [Hotkey: N or F4]"
        )
        self.rename_file_btn.clicked.connect(self._on_rename_file_clicked)
        actions_row.addWidget(self.rename_file_btn)

        self.delete_file_btn = QPushButton("✕ Delete File...")
        self.delete_file_btn.setToolTip(
            "Delete the selected recording file after confirmation [Hotkey: Del]"
        )
        self.delete_file_btn.setStyleSheet(
            "QPushButton { background-color: #8E1523; color: #FFFFFF; border: 1px solid #FF6B7A; font-weight: bold; }"
            "QPushButton:hover { background-color: #B01B2B; }"
            "QPushButton:disabled { background-color: #1A1E24; color: #6E7681; border: 1px solid #21262D; }"
        )
        self.delete_file_btn.clicked.connect(self._on_delete_file_clicked)
        actions_row.addWidget(self.delete_file_btn)

        files_layout.addLayout(actions_row)
        content_row.addWidget(self.files_frame, stretch=8)

        root.addLayout(content_row, stretch=1)

        # 4. Bottom Telemetry & Action Feedback Bar
        bottom_frame = QFrame()
        bottom_frame.setObjectName("SubtleIpBarFrame")
        bottom_layout = QHBoxLayout(bottom_frame)
        bottom_layout.setContentsMargins(10, 4, 10, 4)
        bottom_layout.setSpacing(12)

        self.storage_summary_label = QLabel()
        self.storage_summary_label.setStyleSheet(
            "color: #8B949E; font-family: monospace;"
        )
        bottom_layout.addWidget(self.storage_summary_label, stretch=1)

        self.status_banner_label = QLabel("Ready — Select any recording to play, rename, or delete.")
        self.status_banner_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.status_banner_label.setStyleSheet("color: #58A6FF; font-weight: 600;")
        bottom_layout.addWidget(self.status_banner_label)

        root.addWidget(bottom_frame)

    # -------------------------------------------------------------------------
    # Responsive Scaling & Fullscreen Support
    # -------------------------------------------------------------------------
    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if self._fullscreen_requested:
            self.showFullScreen()
            self._fullscreen_requested = False

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._apply_responsive_scaling()

    def _apply_responsive_scaling(self) -> None:
        w = max(640, self.width())
        h = max(420, self.height())
        self._scale = compute_scale_factor(w, h)

        self.setStyleSheet(build_main_stylesheet(self._scale))
        configure_combo_box_popup(self.sort_combo, self._scale)
        self.storage_monitor.apply_scale(self._scale)
        self._refresh_space_cleared_badge()

    def toggle_folders_sidebar(self) -> None:
        """Collapse or expand the Project Folders sidebar to give the file list 100% screen width."""
        self._folders_pane_visible = not self._folders_pane_visible
        self.folders_frame.setVisible(self._folders_pane_visible)
        if self._folders_pane_visible:
            self.toggle_folders_pane_btn.setText("⇄ Fullscreen File List")
        else:
            self.toggle_folders_pane_btn.setText("▤ Show Project Folders")

    # -------------------------------------------------------------------------
    # Data Loading & List Rebuilding
    # -------------------------------------------------------------------------
    def refresh_all(
        self,
        select_project: Optional[str] = None,
        select_filepath: Optional[Path] = None,
    ) -> None:
        """Rescan /storage project folders, recordings across all projects, and drive utilization."""
        if select_project is not None:
            self._selected_project_filter = select_project

        if select_filepath is None:
            curr_entry = self.get_selected_recording()
            if curr_entry is not None:
                select_filepath = curr_entry.filepath

        self._project_summaries = self.config_manager.list_project_folders()
        self._all_recordings = self.config_manager.list_all_recordings()

        # If selected project folder no longer exists, fall back to All Projects ("*")
        valid_proj_names = {s.project_name for s in self._project_summaries}
        if any(r.project_name == "Root" for r in self._all_recordings):
            valid_proj_names.add("Root")
        if (
            self._selected_project_filter != "*"
            and self._selected_project_filter not in valid_proj_names
        ):
            self._selected_project_filter = "*"

        self._rebuild_project_list()
        self._rebuild_files_list(select_filepath=select_filepath)
        self._refresh_storage_telemetry()

    def _rebuild_project_list(self) -> None:
        self.project_list.blockSignals(True)
        self.project_list.clear()

        total_rec_count = len(self._all_recordings)
        total_rec_bytes = sum(r.size_bytes for r in self._all_recordings)
        all_item_text = (
            f"◈ All Projects  ({total_rec_count} take(s) • {format_storage_bytes(total_rec_bytes)})"
        )
        all_item = QListWidgetItem(all_item_text)
        all_item.setData(Qt.ItemDataRole.UserRole, "*")
        self.project_list.addItem(all_item)

        selected_row = 0
        for idx, summary in enumerate(self._project_summaries, start=1):
            active_tag = " [ACTIVE]" if summary.is_active_project else ""
            icon = "●" if summary.is_active_project else ("○" if summary.is_empty else "◈")
            if summary.is_empty:
                size_desc = "0 takes • Empty"
            elif summary.other_file_count > 0:
                size_desc = (
                    f"{summary.recording_count} take(s) +{summary.other_file_count} file(s) • "
                    f"{summary.formatted_size}"
                )
            else:
                size_desc = f"{summary.recording_count} take(s) • {summary.formatted_size}"

            text = f"{icon} {summary.project_name}/{active_tag}  ({size_desc})"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, summary.project_name)
            self.project_list.addItem(item)
            if self._selected_project_filter == summary.project_name:
                selected_row = idx

        # Include Root virtual entry if any legacy root-level WAV files exist
        root_recs = [r for r in self._all_recordings if r.project_name == "Root"]
        if root_recs:
            root_bytes = sum(r.size_bytes for r in root_recs)
            root_item = QListWidgetItem(
                f"◈ [Root /storage]  ({len(root_recs)} take(s) • {format_storage_bytes(root_bytes)})"
            )
            root_item.setData(Qt.ItemDataRole.UserRole, "Root")
            self.project_list.addItem(root_item)
            if self._selected_project_filter == "Root":
                selected_row = self.project_list.count() - 1

        self.project_list.setCurrentRow(selected_row)
        self.project_list.blockSignals(False)

        has_empty = any(s.is_empty for s in self._project_summaries)
        self.cleanup_empty_btn.setEnabled(has_empty)

    def _rebuild_files_list(self, select_filepath: Optional[Path] = None) -> None:
        if select_filepath is None:
            curr_entry = self.get_selected_recording()
            if curr_entry is not None:
                select_filepath = curr_entry.filepath

        proj_filter = self._selected_project_filter
        search_query = self.search_edit.text().strip().lower()
        sort_mode = str(self.sort_combo.currentData() or "newest")

        filtered: List[RecordingEntry] = []
        for entry in self._all_recordings:
            if proj_filter != "*" and entry.project_name != proj_filter:
                continue
            if search_query:
                haystack = f"{entry.project_name}/{entry.filename}".lower()
                if search_query not in haystack:
                    continue
            filtered.append(entry)

        if sort_mode == "oldest":
            filtered.sort(key=lambda e: e.modified_time)
        elif sort_mode == "size_desc":
            filtered.sort(key=lambda e: (e.size_bytes, e.modified_time), reverse=True)
        elif sort_mode == "size_asc":
            filtered.sort(key=lambda e: (e.size_bytes, e.modified_time))
        elif sort_mode == "name_asc":
            filtered.sort(key=lambda e: (e.filename.lower(), e.project_name.lower()))
        elif sort_mode == "project_asc":
            filtered.sort(key=lambda e: (e.project_name.lower(), -e.modified_time))
        else:
            filtered.sort(key=lambda e: e.modified_time, reverse=True)

        self._displayed_recordings = filtered

        view_bytes = sum(e.size_bytes for e in filtered)
        if proj_filter == "*":
            scope_title = (
                f"All Projects — {len(filtered)} recording(s) ({format_storage_bytes(view_bytes)})"
            )
        else:
            scope_title = (
                f"Project: {proj_filter}/ — {len(filtered)} recording(s) ({format_storage_bytes(view_bytes)})"
            )
        self.files_scope_label.setText(scope_title)

        self.files_list.blockSignals(True)
        self.files_list.clear()

        selected_row = 0
        for idx, entry in enumerate(filtered):
            ch_label = "Stereo" if entry.channels == 2 else f"{entry.channels}ch"
            row_text = (
                f"[{entry.project_name}]   {entry.filename}    |    "
                f"{entry.formatted_duration}    |    "
                f"{entry.bit_depth}-bit {entry.sample_rate:,} Hz {ch_label}    |    "
                f"{entry.formatted_modified}    |    "
                f"{entry.formatted_size}"
            )
            item = QListWidgetItem(row_text)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self.files_list.addItem(item)
            if select_filepath is not None and entry.filepath == select_filepath:
                selected_row = idx

        if filtered:
            self.files_list.setCurrentRow(selected_row)
        self.files_list.blockSignals(False)
        self._update_action_buttons_state()

    def _refresh_storage_telemetry(self) -> None:
        status = self.config_manager.get_storage_status()
        self.storage_monitor.set_status(
            status,
            is_recording=self.audio_engine.is_recording,
            blink_on=True,
        )
        self._refresh_space_cleared_badge(status)
        self._update_storage_summary_line(status)
        self._last_storage_refresh_mono = time.monotonic()

    def _refresh_space_cleared_badge(
        self, status: Optional[StorageStatus] = None
    ) -> None:
        small_px = max(10, int(round(12 * self._scale)))
        pad_v = max(3, int(round(4 * self._scale)))
        pad_h = max(8, int(round(12 * self._scale)))

        if self._session_cleared_bytes > 0:
            cleared_str = format_storage_bytes(self._session_cleared_bytes)
            st = status or self.storage_monitor.status
            if st.available and st.byte_rate > 0:
                gain_sec = float(self._session_cleared_bytes) / float(st.byte_rate)
                txt = f"✓ SPACE CLEARED: +{cleared_str} (+{format_remaining_time(gain_sec)})"
            else:
                txt = f"✓ SPACE CLEARED: +{cleared_str}"
            bg, fg, border = "#0F2D1E", "#7EE787", "#2EA043"
        else:
            txt = "SPACE CLEARED: 0 B"
            bg, fg, border = "#0D1014", "#8B949E", "#30363D"

        self.space_cleared_badge.setText(txt)
        self.space_cleared_badge.setStyleSheet(
            f"background-color: {bg}; color: {fg}; border: 1px solid {border}; "
            f"border-radius: 6px; padding: {pad_v}px {pad_h}px; "
            f"font-family: monospace; font-weight: bold; font-size: {small_px}px;"
        )

    def _update_storage_summary_line(
        self, status: Optional[StorageStatus] = None
    ) -> None:
        st = status or self.storage_monitor.status
        total_projects = len(self._project_summaries)
        total_files = len(self._all_recordings)
        total_rec_bytes = sum(s.total_size_bytes for s in self._project_summaries) + sum(
            r.size_bytes for r in self._all_recordings if r.project_name == "Root"
        )

        sel_rec = self.get_selected_recording()
        sel_proj = self.get_selected_project_summary()

        parts = [
            f"Projects: {total_projects}",
            f"All Recordings: {total_files} ({format_storage_bytes(total_rec_bytes)})",
        ]
        if sel_proj is not None:
            parts.append(
                f"Folder '{sel_proj.project_name}/': {sel_proj.formatted_size} ({sel_proj.total_item_count} file(s))"
            )
        if sel_rec is not None:
            parts.append(
                f"Selected File: {sel_rec.formatted_size}"
            )
        if st.available:
            parts.append(f"Drive Free: {st.formatted_free} ({100.0 - st.used_percent:.1f}% free)")

        self.storage_summary_label.setText("  •  ".join(parts))

    # -------------------------------------------------------------------------
    # Selection Helpers & Button States
    # -------------------------------------------------------------------------
    def get_selected_recording(self) -> Optional[RecordingEntry]:
        item = self.files_list.currentItem()
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        return data if isinstance(data, RecordingEntry) else None

    def get_selected_project_summary(self) -> Optional[ProjectFolderSummary]:
        if self._selected_project_filter in ("*", "", "Root"):
            # If viewing All Projects, check if a recording is selected and resolve its project folder
            rec = self.get_selected_recording()
            if rec is not None and rec.project_name != "Root":
                for s in self._project_summaries:
                    if s.project_name == rec.project_name:
                        return s
            return None
        for s in self._project_summaries:
            if s.project_name == self._selected_project_filter:
                return s
        return None

    def _on_project_selection_changed(self, _row: int) -> None:
        item = self.project_list.currentItem()
        if item is None:
            self._selected_project_filter = "*"
        else:
            self._selected_project_filter = str(
                item.data(Qt.ItemDataRole.UserRole) or "*"
            )
        self._rebuild_files_list()
        self._update_storage_summary_line()

    def _update_action_buttons_state(self, paused: Optional[bool] = None) -> None:
        is_rec = self.audio_engine.is_recording
        sel_rec = self.get_selected_recording()
        has_file = sel_rec is not None and sel_rec.filepath.is_file()
        is_playing = self.audio_engine.is_playing
        is_paused = self.audio_engine.is_paused if paused is None else paused

        self.play_btn.setEnabled(not is_rec and has_file)
        self.stop_play_btn.setEnabled(not is_rec and is_playing)
        self.stop_play_btn.setStyleSheet(
            build_stop_playback_button_style(bool(not is_rec and is_playing), self._scale)
        )
        self.rename_file_btn.setEnabled(not is_rec and has_file)
        self.delete_file_btn.setEnabled(not is_rec and has_file)

        if is_playing:
            if is_paused:
                self.play_btn.setText("▶ Resume")
                self.play_btn.setStyleSheet(
                    build_playback_button_style("paused", self._scale)
                )
            else:
                self.play_btn.setText("❚❚ Pause")
                self.play_btn.setStyleSheet(
                    build_playback_button_style("playing", self._scale)
                )
        else:
            self.play_btn.setText("▶ Play / Pause")
            self.play_btn.setStyleSheet(
                build_playback_button_style("ready", self._scale)
            )
            if sel_rec is not None:
                self.playback_status_label.setText(
                    f"Selected: {sel_rec.project_name}/{sel_rec.filename} ({sel_rec.formatted_size})"
                )
            else:
                self.playback_status_label.setText(
                    f"{len(self._displayed_recordings)} file(s) shown"
                )

        # Project folder action buttons
        explicit_proj: Optional[ProjectFolderSummary] = None
        if self._selected_project_filter not in ("*", "", "Root"):
            for s in self._project_summaries:
                if s.project_name == self._selected_project_filter:
                    explicit_proj = s
                    break
        target_proj = explicit_proj or self.get_selected_project_summary()

        can_modify_proj = not is_rec and target_proj is not None
        self.set_active_project_btn.setEnabled(
            can_modify_proj and not target_proj.is_active_project  # type: ignore[union-attr]
        )
        self.cleanup_project_btn.setEnabled(
            can_modify_proj and not target_proj.is_empty  # type: ignore[union-attr]
        )
        self.delete_project_btn.setEnabled(can_modify_proj)

        if target_proj is not None:
            self.cleanup_project_btn.setText(
                f"◈ Empty '{target_proj.project_name}/'..."
            )
            self.delete_project_btn.setText(
                f"✕ Delete '{target_proj.project_name}/'..."
            )
        else:
            self.cleanup_project_btn.setText("◈ Empty Folder...")
            self.delete_project_btn.setText("✕ Delete Folder...")

        self._update_storage_summary_line()

    # -------------------------------------------------------------------------
    # Playback, Rename & Delete File Actions
    # -------------------------------------------------------------------------
    def _on_play_pause_clicked(self, force_new: bool = False) -> None:
        if self.audio_engine.is_recording:
            return
        sel_rec = self.get_selected_recording()
        if sel_rec is None or not sel_rec.filepath.is_file():
            return

        if (
            not force_new
            and self.audio_engine.is_playing
            and self.audio_engine.playing_filepath == sel_rec.filepath
        ):
            paused = self.audio_engine.toggle_pause_playback()
            self._update_action_buttons_state(paused=paused)
            return

        self.audio_engine.start_playback(
            filepath=sel_rec.filepath,
            output_device=self._output_device,
            on_finished=lambda: QTimer.singleShot(
                0, self._update_action_buttons_state
            ),
            on_error=lambda err: QTimer.singleShot(
                0,
                lambda: QMessageBox.warning(
                    self, "Playback Error", f"Could not play WAV file:\n{err}"
                ),
            ),
        )
        self._update_action_buttons_state()

    def _on_stop_playback_clicked(self) -> None:
        self.audio_engine.stop_playback()
        self._update_action_buttons_state()

    def _on_rename_file_clicked(self) -> None:
        if self.audio_engine.is_recording:
            return
        sel_rec = self.get_selected_recording()
        if sel_rec is None or not sel_rec.filepath.is_file():
            return

        if (
            self.audio_engine.is_playing
            and self.audio_engine.playing_filepath == sel_rec.filepath
        ):
            self.audio_engine.stop_playback()

        if self._rename_dialog_factory is not None:
            dlg = self._rename_dialog_factory(
                self.config_manager, sel_rec.filepath, self
            )
        else:
            from app.ui.main_window import RenameRecordingDialog

            dlg = RenameRecordingDialog(
                config_manager=self.config_manager,
                filepath=sel_rec.filepath,
                parent=self,
            )

        if dlg.exec() and getattr(dlg, "renamed_path", None) is not None:
            new_path: Path = dlg.renamed_path  # type: ignore[assignment]
            self.status_banner_label.setText(
                f"✓ Renamed '{sel_rec.filename}' → '{new_path.name}' in {sel_rec.project_name}/"
            )
            self.status_banner_label.setStyleSheet("color: #7EE787; font-weight: bold;")
            self.refresh_all(select_filepath=new_path)
        else:
            self._update_action_buttons_state()

    def _on_delete_file_clicked(self) -> None:
        if self.audio_engine.is_recording:
            return
        sel_rec = self.get_selected_recording()
        if sel_rec is None or not sel_rec.filepath.is_file():
            return

        status = self.config_manager.get_storage_status()
        dlg = DeleteFileConfirmDialog(
            entry=sel_rec,
            storage_status=status,
            parent=self,
        )
        if not dlg.exec():
            return

        self.execute_delete_recording(sel_rec)

    def execute_delete_recording(self, entry: RecordingEntry) -> Tuple[bool, str, int]:
        """Delete the specified recording entry after confirmation and update storage telemetry."""
        if (
            self.audio_engine.is_playing
            and self.audio_engine.playing_filepath == entry.filepath
        ):
            self.audio_engine.stop_playback()

        ok, msg, freed_bytes = self.config_manager.delete_recording(entry.filepath)
        if ok:
            self._session_cleared_bytes += max(0, int(freed_bytes))
            self.status_banner_label.setText(f"✓ {msg}")
            self.status_banner_label.setStyleSheet("color: #7EE787; font-weight: bold;")
            self.refresh_all()
        else:
            self.status_banner_label.setText(f"⚠ {msg}")
            self.status_banner_label.setStyleSheet("color: #FF6B7A; font-weight: bold;")
        return (ok, msg, freed_bytes)

    # -------------------------------------------------------------------------
    # Project Folder Cleanup & Recursive Deletion Actions
    # -------------------------------------------------------------------------
    def _on_set_active_project_clicked(self) -> None:
        if self.audio_engine.is_recording:
            return
        target_proj = self.get_selected_project_summary()
        if target_proj is None:
            return
        self.config_manager.settings.project_name = target_proj.project_name
        self.config_manager.save(commit_lbu=True)
        self.config_manager.ensure_project_dir(target_proj.project_name)
        self.status_banner_label.setText(
            f"✓ Active recording project set to '{target_proj.project_name}/'."
        )
        self.status_banner_label.setStyleSheet("color: #7EE787; font-weight: bold;")
        self.refresh_all(select_project=target_proj.project_name)

    def _on_cleanup_empty_folders_clicked(self) -> None:
        if self.audio_engine.is_recording:
            return
        empty_folders = [s.project_name for s in self._project_summaries if s.is_empty]
        if not empty_folders:
            self.status_banner_label.setText("No empty project folders to clean up.")
            return

        folder_list_str = ", ".join(f"'{name}/'" for name in empty_folders)
        reply = QMessageBox.question(
            self,
            "Clean Up Empty Project Folders",
            f"Remove {len(empty_folders)} empty project folder(s) containing 0 files?\n\n"
            f"Folders: {folder_list_str}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        ok, msg, _count = self.config_manager.cleanup_empty_project_folders()
        self.status_banner_label.setText(f"✓ {msg}" if ok else f"⚠ {msg}")
        self.status_banner_label.setStyleSheet(
            "color: #7EE787; font-weight: bold;" if ok else "color: #FF6B7A; font-weight: bold;"
        )
        self.refresh_all()

    def _on_cleanup_project_folder_clicked(self) -> None:
        if self.audio_engine.is_recording:
            return
        target_proj = self.get_selected_project_summary()
        if target_proj is None or target_proj.is_empty:
            return

        proj_recs = [
            r for r in self._all_recordings if r.project_name == target_proj.project_name
        ]
        status = self.config_manager.get_storage_status()
        dlg = RecursiveProjectConfirmDialog(
            folder_summary=target_proj,
            recordings=proj_recs,
            storage_status=status,
            mode="cleanup",
            parent=self,
        )
        if not dlg.exec() or not dlg.confirmed_recursive:
            return

        self.execute_cleanup_project_folder(
            target_proj.project_name, confirm_recursive=True
        )

    def execute_cleanup_project_folder(
        self, project_name: str, confirm_recursive: bool = False
    ) -> Tuple[bool, str, int]:
        """Empty all recordings inside a project folder after recursive confirmation."""
        if (
            self.audio_engine.is_playing
            and self.audio_engine.playing_filepath is not None
            and self.audio_engine.playing_filepath.parent.name == project_name
        ):
            self.audio_engine.stop_playback()

        ok, msg, freed_bytes = self.config_manager.cleanup_project_folder(
            project_name, confirm_recursive=confirm_recursive
        )
        if ok:
            self._session_cleared_bytes += max(0, int(freed_bytes))
            self.status_banner_label.setText(f"✓ {msg}")
            self.status_banner_label.setStyleSheet("color: #7EE787; font-weight: bold;")
            self.refresh_all(select_project=project_name)
        else:
            self.status_banner_label.setText(f"⚠ {msg}")
            self.status_banner_label.setStyleSheet("color: #FF6B7A; font-weight: bold;")
        return (ok, msg, freed_bytes)

    def _on_delete_project_folder_clicked(self) -> None:
        if self.audio_engine.is_recording:
            return
        target_proj = self.get_selected_project_summary()
        if target_proj is None:
            return

        proj_recs = [
            r for r in self._all_recordings if r.project_name == target_proj.project_name
        ]
        status = self.config_manager.get_storage_status()
        dlg = RecursiveProjectConfirmDialog(
            folder_summary=target_proj,
            recordings=proj_recs,
            storage_status=status,
            mode="delete",
            parent=self,
        )
        if not dlg.exec() or not dlg.confirmed_recursive:
            return

        self.execute_delete_project_folder(
            target_proj.project_name, confirm_recursive=True
        )

    def execute_delete_project_folder(
        self, project_name: str, confirm_recursive: bool = False
    ) -> Tuple[bool, str, int]:
        """Recursively delete a project folder and its contents after 2-step confirmation."""
        if (
            self.audio_engine.is_playing
            and self.audio_engine.playing_filepath is not None
            and self.audio_engine.playing_filepath.parent.name == project_name
        ):
            self.audio_engine.stop_playback()

        ok, msg, freed_bytes = self.config_manager.delete_project_folder(
            project_name, confirm_recursive=confirm_recursive
        )
        if ok:
            self._session_cleared_bytes += max(0, int(freed_bytes))
            self.status_banner_label.setText(f"✓ {msg}")
            self.status_banner_label.setStyleSheet("color: #7EE787; font-weight: bold;")
            self.refresh_all(select_project="*")
        else:
            self.status_banner_label.setText(f"⚠ {msg}")
            self.status_banner_label.setStyleSheet("color: #FF6B7A; font-weight: bold;")
        return (ok, msg, freed_bytes)

    # -------------------------------------------------------------------------
    # Periodic UI Timer & Keyboard Shortcuts
    # -------------------------------------------------------------------------
    def _on_ui_tick(self) -> None:
        if self.audio_engine.is_playing:
            pos = self.audio_engine.play_position_sec
            dur = self.audio_engine.play_duration_sec
            p_m, p_s = divmod(int(pos), 60)
            d_m, d_s = divmod(int(dur), 60)
            fpath = self.audio_engine.playing_filepath
            rel_name = (
                f"{fpath.parent.name}/{fpath.name}" if fpath is not None else ""
            )
            state_str = "PAUSED" if self.audio_engine.is_paused else "PLAYING"
            self.playback_status_label.setText(
                f"{state_str}: {rel_name}  [{p_m:02d}:{p_s:02d} / {d_m:02d}:{d_s:02d}]"
            )
            self.playback_status_label.setStyleSheet(
                "color: #58A6FF; font-family: monospace; font-weight: bold;"
            )
        else:
            self.playback_status_label.setStyleSheet(
                "color: #8B949E; font-family: monospace;"
            )

        now_mono = time.monotonic()
        if now_mono - self._last_storage_refresh_mono >= 2.0:
            self._refresh_storage_telemetry()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.isAutoRepeat():
            return

        # If search input has focus, let typing keys go to search_edit
        if self.search_edit.hasFocus() and event.key() not in (
            Qt.Key.Key_Escape,
            Qt.Key.Key_F4,
            Qt.Key.Key_F5,
            Qt.Key.Key_F11,
        ):
            super().keyPressEvent(event)
            return

        key = event.key()
        if key in (Qt.Key.Key_P, Qt.Key.Key_Space):
            self._on_play_pause_clicked()
            return
        if key == Qt.Key.Key_S:
            self._on_stop_playback_clicked()
            return
        if key == Qt.Key.Key_Left and self.audio_engine.is_playing:
            self.audio_engine.seek_playback(self.audio_engine.play_position_sec - 3.0)
            self._on_ui_tick()
            return
        if key == Qt.Key.Key_Right and self.audio_engine.is_playing:
            self.audio_engine.seek_playback(self.audio_engine.play_position_sec + 3.0)
            self._on_ui_tick()
            return
        if key in (Qt.Key.Key_N, Qt.Key.Key_F4):
            self._on_rename_file_clicked()
            return
        if key == Qt.Key.Key_Delete:
            self._on_delete_file_clicked()
            return
        if key == Qt.Key.Key_F5:
            self.refresh_all()
            return
        if key == Qt.Key.Key_F11:
            self.toggle_folders_sidebar()
            return
        if key == Qt.Key.Key_Escape:
            self.accept()
            return

        super().keyPressEvent(event)

    def done(self, result: int) -> None:
        if hasattr(self, "_ui_timer"):
            self._ui_timer.stop()
        if self.audio_engine.is_playing:
            self.audio_engine.stop_playback()
        super().done(result)
