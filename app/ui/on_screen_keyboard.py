"""Touchscreen On-Screen Keyboard (OSK) dialog and input event controller."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.ui.styles import build_main_stylesheet, compute_scale_factor

# Standard QWERTY rows: each entry is (lower_char, shifted_char)
ROW_1_ALPHA: Tuple[Tuple[str, str], ...] = (
    ("`", "~"),
    ("1", "!"),
    ("2", "@"),
    ("3", "#"),
    ("4", "$"),
    ("5", "%"),
    ("6", "^"),
    ("7", "&"),
    ("8", "*"),
    ("9", "("),
    ("0", ")"),
    ("-", "_"),
    ("=", "+"),
)

ROW_2_ALPHA: Tuple[Tuple[str, str], ...] = (
    ("q", "Q"),
    ("w", "W"),
    ("e", "E"),
    ("r", "R"),
    ("t", "T"),
    ("y", "Y"),
    ("u", "U"),
    ("i", "I"),
    ("o", "O"),
    ("p", "P"),
    ("[", "{"),
    ("]", "}"),
    ("\\", "|"),
)

ROW_3_ALPHA: Tuple[Tuple[str, str], ...] = (
    ("a", "A"),
    ("s", "S"),
    ("d", "D"),
    ("f", "F"),
    ("g", "G"),
    ("h", "H"),
    ("j", "J"),
    ("k", "K"),
    ("l", "L"),
    (";", ":"),
    ("'", '"'),
)

ROW_4_ALPHA: Tuple[Tuple[str, str], ...] = (
    ("z", "Z"),
    ("x", "X"),
    ("c", "C"),
    ("v", "V"),
    ("b", "B"),
    ("n", "N"),
    ("m", "M"),
    (",", "<"),
    (".", ">"),
    ("/", "?"),
    ("_", "_"),
    ("-", "-"),
)

# Symbols / Numeric layer rows: each entry is (primary_symbol, shifted_symbol)
ROW_1_SYM: Tuple[Tuple[str, str], ...] = (
    ("1", "!"),
    ("2", "@"),
    ("3", "#"),
    ("4", "$"),
    ("5", "%"),
    ("6", "^"),
    ("7", "&"),
    ("8", "*"),
    ("9", "("),
    ("0", ")"),
    ("_", "_"),
    ("-", "-"),
    ("+", "="),
)

ROW_2_SYM: Tuple[Tuple[str, str], ...] = (
    ("!", "!"),
    ("@", "@"),
    ("#", "#"),
    ("$", "$"),
    ("%", "%"),
    ("^", "^"),
    ("&", "&"),
    ("*", "*"),
    ("(", "("),
    (")", ")"),
    ("[", "{"),
    ("]", "}"),
    ("\\", "|"),
)

ROW_3_SYM: Tuple[Tuple[str, str], ...] = (
    ("~", "~"),
    ("`", "`"),
    ("|", "|"),
    ("{", "{"),
    ("}", "}"),
    (";", ";"),
    (":", ":"),
    ("'", "'"),
    ('"', '"'),
    ("<", "<"),
    (">", ">"),
)

ROW_4_SYM: Tuple[Tuple[str, str], ...] = (
    (",", ","),
    (".", "."),
    ("/", "/"),
    ("?", "?"),
    ("=", "="),
    ("+", "+"),
    ("*", "*"),
    ("&", "&"),
    ("%", "%"),
    ("#", "#"),
    ("_", "_"),
    ("-", "-"),
)


@dataclass
class OskFieldSpec:
    """Metadata describing a QLineEdit managed by the On-Screen Keyboard."""

    line_edit: QLineEdit
    title: str = "Text Input"
    hint: str = ""
    group: Optional[str] = None


class _CharKeyButton(QPushButton):
    """Touch key button storing its normal/shifted/symbol character mappings."""

    def __init__(
        self,
        alpha_pair: Tuple[str, str],
        sym_pair: Tuple[str, str],
        on_char: Callable[[str], None],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.alpha_pair = alpha_pair
        self.sym_pair = sym_pair
        self._current_char = alpha_pair[0]
        self._on_char = on_char

        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._handle_clicked)
        self.update_key_face(shift=False, caps=False, symbols=False)

    @property
    def current_char(self) -> str:
        return self._current_char

    def update_key_face(self, shift: bool, caps: bool, symbols: bool) -> None:
        if symbols:
            base, shifted = self.sym_pair
            ch = shifted if shift else base
        else:
            base, shifted = self.alpha_pair
            if base.isalpha():
                upper = shift ^ caps
                ch = shifted if upper else base
            else:
                ch = shifted if shift else base

        self._current_char = ch
        # Escape '&' for Qt button mnemonic display
        self.setText("&&" if ch == "&" else ch)

    def _handle_clicked(self) -> None:
        self._on_char(self._current_char)


class OnScreenKeyboardDialog(QDialog):
    """Fullscreen-friendly touch On-Screen Keyboard dialog for QLineEdit inputs."""

    def __init__(
        self,
        target: QLineEdit,
        title: str = "On-Screen Keyboard",
        hint: str = "",
        sibling_fields: Optional[Sequence[OskFieldSpec]] = None,
        live_update: bool = True,
        on_committed: Optional[Callable[[QLineEdit, str], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent or target.window())
        self._target: QLineEdit = target
        self._title: str = title
        self._hint: str = hint
        self._sibling_fields: List[OskFieldSpec] = list(sibling_fields or [])
        self._live_update: bool = live_update
        self._on_committed = on_committed

        self._initial_text: str = target.text()
        self._shift_active: bool = False
        self._caps_lock: bool = False
        self._symbols_mode: bool = False
        self._password_revealed: bool = False
        self._char_buttons: List[_CharKeyButton] = []

        self.setWindowTitle(f"On-Screen Keyboard — {title}")
        self.setModal(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        # Size responsively to parent window / screen
        parent_win = parent or target.window()
        if parent_win is not None and parent_win.width() >= 480:
            pw, ph = parent_win.width(), parent_win.height()
            self._scale = compute_scale_factor(pw, ph)
            dlg_w = max(620, min(pw - 24, int(round(pw * 0.94))))
            dlg_h = max(380, min(ph - 20, int(round(ph * 0.78))))
            self.resize(dlg_w, dlg_h)
        else:
            self._scale = 1.0
            self.resize(860, 460)

        self._build_ui()
        self._load_target_into_display(self._target, self._title, self._hint)
        self._apply_styles()

    @property
    def target_line_edit(self) -> QLineEdit:
        return self._target

    @property
    def current_text(self) -> str:
        return self.input_edit.text()

    # -------------------------------------------------------------------------
    # UI Construction
    # -------------------------------------------------------------------------
    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(14, 12, 14, 12)
        root_layout.setSpacing(8)

        # 1. Header & Input Preview Frame
        top_frame = QFrame(self)
        top_frame.setObjectName("OskHeaderFrame")
        top_layout = QVBoxLayout(top_frame)
        top_layout.setContentsMargins(12, 8, 12, 10)
        top_layout.setSpacing(6)

        header_row = QHBoxLayout()
        self.title_label = QLabel()
        self.title_label.setObjectName("HeaderTitle")
        header_row.addWidget(self.title_label)

        header_row.addStretch(1)

        self.hint_label = QLabel()
        self.hint_label.setStyleSheet("color: #8B949E;")
        header_row.addWidget(self.hint_label)
        top_layout.addLayout(header_row)

        input_row = QHBoxLayout()
        input_row.setSpacing(8)

        self.input_edit = QLineEdit()
        self.input_edit.setProperty("skip_osk", True)
        self.input_edit.textChanged.connect(self._on_display_text_changed)
        input_row.addWidget(self.input_edit, stretch=1)

        self.show_pw_btn = QPushButton("👁 Show")
        self.show_pw_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.show_pw_btn.setToolTip("Toggle password visibility while typing")
        self.show_pw_btn.clicked.connect(self.toggle_password_visibility)
        input_row.addWidget(self.show_pw_btn)

        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.select_all_btn.setToolTip("Select all text in the field")
        self.select_all_btn.clicked.connect(self.input_edit.selectAll)
        input_row.addWidget(self.select_all_btn)

        self.clear_btn = QPushButton("✕ Clear")
        self.clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clear_btn.setToolTip("Clear all text in this input field")
        self.clear_btn.clicked.connect(self.press_clear)
        input_row.addWidget(self.clear_btn)

        top_layout.addLayout(input_row)
        root_layout.addWidget(top_frame)

        # 2. Keyboard Keys Deck
        keys_frame = QFrame(self)
        keys_frame.setObjectName("OskKeysFrame")
        keys_layout = QVBoxLayout(keys_frame)
        keys_layout.setContentsMargins(10, 10, 10, 10)
        keys_layout.setSpacing(6)

        # Row 1: Numbers / Symbols + Backspace
        row1 = QHBoxLayout()
        row1.setSpacing(5)
        for a_pair, s_pair in zip(ROW_1_ALPHA, ROW_1_SYM):
            btn = _CharKeyButton(a_pair, s_pair, self.press_key, self)
            self._char_buttons.append(btn)
            row1.addWidget(btn, stretch=2)

        self.backspace_btn = self._make_action_button(
            "⌫ Back", self.press_backspace, tooltip="Delete character before cursor [Backspace]"
        )
        self.backspace_btn.setAutoRepeat(True)
        self.backspace_btn.setAutoRepeatDelay(350)
        self.backspace_btn.setAutoRepeatInterval(65)
        row1.addWidget(self.backspace_btn, stretch=3)
        keys_layout.addLayout(row1, stretch=1)

        # Row 2: Top QWERTY row
        row2 = QHBoxLayout()
        row2.setSpacing(5)
        for a_pair, s_pair in zip(ROW_2_ALPHA, ROW_2_SYM):
            btn = _CharKeyButton(a_pair, s_pair, self.press_key, self)
            self._char_buttons.append(btn)
            row2.addWidget(btn, stretch=2)
        keys_layout.addLayout(row2, stretch=1)

        # Row 3: Caps Lock + Home QWERTY row + Enter/Done
        row3 = QHBoxLayout()
        row3.setSpacing(5)
        self.caps_btn = self._make_action_button(
            "⇪ Caps", self.toggle_caps_lock, tooltip="Toggle Caps Lock"
        )
        row3.addWidget(self.caps_btn, stretch=3)

        for a_pair, s_pair in zip(ROW_3_ALPHA, ROW_3_SYM):
            btn = _CharKeyButton(a_pair, s_pair, self.press_key, self)
            self._char_buttons.append(btn)
            row3.addWidget(btn, stretch=2)

        self.enter_key_btn = self._make_action_button(
            "↵ Enter", self.accept, tooltip="Apply text and close keyboard [Enter]"
        )
        row3.addWidget(self.enter_key_btn, stretch=3)
        keys_layout.addLayout(row3, stretch=1)

        # Row 4: Shift + Bottom QWERTY row + Right Shift
        row4 = QHBoxLayout()
        row4.setSpacing(5)
        self.shift_left_btn = self._make_action_button(
            "⇧ Shift", self.toggle_shift, tooltip="One-shot Shift for uppercase or top symbols"
        )
        row4.addWidget(self.shift_left_btn, stretch=3)

        for a_pair, s_pair in zip(ROW_4_ALPHA, ROW_4_SYM):
            btn = _CharKeyButton(a_pair, s_pair, self.press_key, self)
            self._char_buttons.append(btn)
            row4.addWidget(btn, stretch=2)

        self.shift_right_btn = self._make_action_button(
            "⇧ Shift", self.toggle_shift, tooltip="One-shot Shift for uppercase or top symbols"
        )
        row4.addWidget(self.shift_right_btn, stretch=3)
        keys_layout.addLayout(row4, stretch=1)

        # Row 5: Symbols toggle, Cursor arrows, Spacebar, Next Field, Cancel, Done
        row5 = QHBoxLayout()
        row5.setSpacing(6)

        self.sym_btn = self._make_action_button(
            "?123", self.toggle_symbols, tooltip="Switch between letters and symbols"
        )
        row5.addWidget(self.sym_btn, stretch=2)

        self.left_arrow_btn = self._make_action_button(
            "◀", self.move_cursor_left, tooltip="Move cursor left"
        )
        self.left_arrow_btn.setAutoRepeat(True)
        row5.addWidget(self.left_arrow_btn, stretch=1)

        self.right_arrow_btn = self._make_action_button(
            "▶", self.move_cursor_right, tooltip="Move cursor right"
        )
        self.right_arrow_btn.setAutoRepeat(True)
        row5.addWidget(self.right_arrow_btn, stretch=1)

        self.space_btn = self._make_action_button(
            "SPACE", lambda: self.press_key(" "), tooltip="Insert space"
        )
        row5.addWidget(self.space_btn, stretch=5)

        self.next_field_btn = self._make_action_button(
            "Next Field ⇥",
            self.switch_to_next_field,
            tooltip="Save this field and advance to the next input field",
        )
        row5.addWidget(self.next_field_btn, stretch=2)

        self.cancel_btn = self._make_action_button(
            "Cancel", self.reject, tooltip="Discard changes to this field and close [Esc]"
        )
        row5.addWidget(self.cancel_btn, stretch=2)

        self.done_btn = self._make_action_button(
            "✓ Done", self.accept, tooltip="Apply text and close on-screen keyboard [Enter]"
        )
        row5.addWidget(self.done_btn, stretch=2)

        keys_layout.addLayout(row5, stretch=1)
        root_layout.addWidget(keys_frame, stretch=1)

    def _make_action_button(
        self, label: str, slot: Callable[[], None], tooltip: str = ""
    ) -> QPushButton:
        btn = QPushButton(label, self)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        if tooltip:
            btn.setToolTip(tooltip)
        btn.clicked.connect(slot)
        return btn

    def _load_target_into_display(
        self, target: QLineEdit, title: str, hint: str
    ) -> None:
        self._target = target
        self._title = title
        self._hint = hint
        self._initial_text = target.text()
        self._password_revealed = False

        self.setWindowTitle(f"On-Screen Keyboard — {title}")
        self.title_label.setText(f"⌨  {title}")
        self.hint_label.setText(hint or target.placeholderText() or "")

        self.input_edit.blockSignals(True)
        self.input_edit.setPlaceholderText(target.placeholderText())
        self.input_edit.setMaxLength(target.maxLength())
        self.input_edit.setEchoMode(target.echoMode())
        self.input_edit.setText(self._initial_text)
        self.input_edit.setCursorPosition(len(self._initial_text))
        self.input_edit.blockSignals(False)

        is_pw = target.echoMode() == QLineEdit.EchoMode.Password
        self.show_pw_btn.setVisible(is_pw)
        self.show_pw_btn.setText("👁 Show")

        has_siblings = len(self._sibling_fields) > 1
        self.next_field_btn.setVisible(has_siblings)
        self.input_edit.setFocus()

    def _apply_styles(self) -> None:
        base_style = build_main_stylesheet(self._scale)
        key_font_px = max(13, int(round(17 * self._scale)))
        input_font_px = max(15, int(round(20 * self._scale)))
        key_min_h = max(36, int(round(46 * self._scale)))

        extra_style = f"""
        QFrame#OskHeaderFrame {{
            background-color: #161B22;
            border: 1px solid #30363D;
            border-radius: 8px;
        }}
        QFrame#OskKeysFrame {{
            background-color: #0D1014;
            border: 1px solid #21262D;
            border-radius: 10px;
        }}
        QFrame#OskHeaderFrame QLineEdit {{
            font-size: {input_font_px}px;
            font-weight: bold;
            padding: 8px 12px;
            background-color: #090C10;
            border: 2px solid #388BFD;
            color: #F0F6FC;
        }}
        QFrame#OskKeysFrame QPushButton {{
            font-size: {key_font_px}px;
            font-weight: bold;
            min-height: {key_min_h}px;
            padding: 4px 6px;
            background-color: #21262D;
            color: #F0F6FC;
            border: 1px solid #30363D;
            border-radius: 6px;
        }}
        QFrame#OskKeysFrame QPushButton:hover {{
            background-color: #30363D;
            border-color: #58A6FF;
        }}
        QFrame#OskKeysFrame QPushButton:pressed {{
            background-color: #1F6FEB;
            color: #FFFFFF;
        }}
        """
        self.setStyleSheet(base_style + extra_style)
        self._refresh_modifier_styles()

    def _refresh_modifier_styles(self) -> None:
        for btn in self._char_buttons:
            btn.update_key_face(
                shift=self._shift_active,
                caps=self._caps_lock,
                symbols=self._symbols_mode,
            )

        active_mod_style = (
            "background-color: #1F6FEB; color: #FFFFFF; border: 2px solid #58A6FF;"
        )
        inactive_mod_style = (
            "background-color: #161B22; color: #8B949E; border: 1px solid #30363D;"
        )

        self.shift_left_btn.setStyleSheet(
            active_mod_style if self._shift_active else inactive_mod_style
        )
        self.shift_right_btn.setStyleSheet(
            active_mod_style if self._shift_active else inactive_mod_style
        )
        self.caps_btn.setStyleSheet(
            active_mod_style if self._caps_lock else inactive_mod_style
        )
        self.sym_btn.setText("ABC" if self._symbols_mode else "?123")
        self.sym_btn.setStyleSheet(
            active_mod_style if self._symbols_mode else inactive_mod_style
        )

        self.backspace_btn.setStyleSheet(
            "background-color: #2D2024; color: #FF7B72; border: 1px solid #6E3630;"
        )
        self.cancel_btn.setStyleSheet(
            "background-color: #2D2024; color: #FF7B72; border: 1px solid #6E3630;"
        )
        self.enter_key_btn.setStyleSheet(
            "background-color: #1F6FEB; color: #FFFFFF; border: 1px solid #58A6FF;"
        )
        self.done_btn.setStyleSheet(
            "background-color: #1F883D; color: #FFFFFF; border: 2px solid #3FB950;"
        )
        self.next_field_btn.setStyleSheet(
            "background-color: #1F3A5F; color: #58A6FF; border: 1px solid #388BFD;"
        )

    # -------------------------------------------------------------------------
    # Key Actions
    # -------------------------------------------------------------------------
    def press_key(self, text: str) -> None:
        """Insert character(s) at the current cursor position in the input display."""
        self.input_edit.insert(text)
        if self._shift_active:
            self._shift_active = False
            self._refresh_modifier_styles()

    def press_backspace(self) -> None:
        """Delete selected text or the character immediately preceding the cursor."""
        self.input_edit.backspace()

    def press_clear(self) -> None:
        """Clear all text in the input display."""
        self.input_edit.clear()

    def move_cursor_left(self) -> None:
        pos = self.input_edit.cursorPosition()
        if pos > 0:
            self.input_edit.setCursorPosition(pos - 1)
        else:
            self.input_edit.deselect()

    def move_cursor_right(self) -> None:
        pos = self.input_edit.cursorPosition()
        if pos < len(self.input_edit.text()):
            self.input_edit.setCursorPosition(pos + 1)
        else:
            self.input_edit.deselect()

    def toggle_shift(self) -> None:
        self._shift_active = not self._shift_active
        self._refresh_modifier_styles()

    def toggle_caps_lock(self) -> None:
        self._caps_lock = not self._caps_lock
        self._refresh_modifier_styles()

    def toggle_symbols(self) -> None:
        self._symbols_mode = not self._symbols_mode
        self._shift_active = False
        self._refresh_modifier_styles()

    def toggle_password_visibility(self) -> None:
        if self._target.echoMode() != QLineEdit.EchoMode.Password:
            return
        self._password_revealed = not self._password_revealed
        if self._password_revealed:
            self.input_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self.show_pw_btn.setText("🙈 Hide")
        else:
            self.input_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.show_pw_btn.setText("👁 Show")

    def switch_to_next_field(self) -> None:
        """Commit current field text and advance to the next sibling QLineEdit."""
        self._commit_current_target()
        if len(self._sibling_fields) <= 1:
            return

        curr_idx = -1
        for idx, spec in enumerate(self._sibling_fields):
            if spec.line_edit is self._target:
                curr_idx = idx
                break

        next_idx = (curr_idx + 1) % len(self._sibling_fields)
        next_spec = self._sibling_fields[next_idx]
        self._load_target_into_display(
            next_spec.line_edit, next_spec.title, next_spec.hint
        )

    def _on_display_text_changed(self, new_text: str) -> None:
        if self._live_update:
            self._target.setText(new_text)

    def _commit_current_target(self) -> None:
        final_text = self.input_edit.text()
        if self._target.text() != final_text:
            self._target.setText(final_text)
        if self._on_committed is not None:
            self._on_committed(self._target, final_text)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    def accept(self) -> None:
        self._commit_current_target()
        super().accept()

    def reject(self) -> None:
        if self._target.text() != self._initial_text:
            self._target.setText(self._initial_text)
        super().reject()


class OnScreenKeyboardController(QObject):
    """Event filter that opens OnScreenKeyboardDialog when an enabled QLineEdit is tapped."""

    def __init__(
        self,
        is_enabled: Callable[[], bool],
        on_committed: Optional[Callable[[QLineEdit, str], None]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._is_enabled = is_enabled
        self._on_committed = on_committed
        self._specs: Dict[QLineEdit, OskFieldSpec] = {}
        self._groups: Dict[str, List[OskFieldSpec]] = {}
        self._active_dialog: Optional[OnScreenKeyboardDialog] = None
        self._opening_scheduled: bool = False

    @property
    def is_enabled(self) -> bool:
        return bool(self._is_enabled())

    @property
    def active_dialog(self) -> Optional[OnScreenKeyboardDialog]:
        return self._active_dialog

    def register_line_edit(
        self,
        line_edit: QLineEdit,
        title: str = "Text Input",
        hint: str = "",
        group: Optional[str] = None,
    ) -> None:
        """Register a QLineEdit so tapping/clicking it opens the On-Screen Keyboard when enabled."""
        spec = OskFieldSpec(
            line_edit=line_edit,
            title=title,
            hint=hint,
            group=group,
        )
        self._specs[line_edit] = spec
        if group:
            grp_list = self._groups.setdefault(group, [])
            if all(s.line_edit is not line_edit for s in grp_list):
                grp_list.append(spec)
        line_edit.installEventFilter(self)

    def attach_to_container(self, container: QWidget) -> None:
        """Install event filter on all QLineEdit children inside container."""
        for edit in container.findChildren(QLineEdit):
            if edit.property("skip_osk"):
                continue
            if edit not in self._specs:
                title = edit.accessibleName() or edit.placeholderText() or "Text Input"
                self.register_line_edit(edit, title=title)
            else:
                edit.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(watched, QLineEdit) and not watched.property("skip_osk"):
            if (
                event.type() == QEvent.Type.MouseButtonPress
                and watched.isEnabled()
                and not watched.isReadOnly()
                and self.is_enabled
            ):
                # Schedule opening on next event loop tick so mouse press completes cleanly
                if self._active_dialog is None and not self._opening_scheduled:
                    self._opening_scheduled = True
                    QTimer.singleShot(0, lambda w=watched: self._open_scheduled(w))
                return True
        return super().eventFilter(watched, event)

    def _open_scheduled(self, line_edit: QLineEdit) -> None:
        self._opening_scheduled = False
        if not self.is_enabled or not line_edit.isEnabled() or line_edit.isReadOnly():
            return
        self.open_for_line_edit(line_edit)

    def open_for_line_edit(self, line_edit: QLineEdit) -> int:
        """Open the modal On-Screen Keyboard for the given QLineEdit and return its QDialog result."""
        if self._active_dialog is not None:
            return int(QDialog.DialogCode.Rejected)

        spec = self._specs.get(line_edit)
        title = spec.title if spec else (line_edit.placeholderText() or "Text Input")
        hint = spec.hint if spec else ""
        siblings: List[OskFieldSpec] = []
        if spec and spec.group and spec.group in self._groups:
            siblings = [
                s
                for s in self._groups[spec.group]
                if s.line_edit.isEnabled() and not s.line_edit.isReadOnly()
            ]

        dlg = OnScreenKeyboardDialog(
            target=line_edit,
            title=title,
            hint=hint,
            sibling_fields=siblings,
            live_update=True,
            on_committed=self._on_committed,
            parent=line_edit.window(),
        )
        self._active_dialog = dlg
        try:
            return dlg.exec()
        finally:
            self._active_dialog = None
