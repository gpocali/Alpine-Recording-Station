"""Teleprompter script engine, rich-text sanitizer, bracket-excluding lookahead tracker, and Whisper AI STT worker."""

from __future__ import annotations

import html
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.audio_engine import resample_mono_signal

logger = logging.getLogger("AlpineRecorder.PrompterEngine")

SCRIPT_FILENAME = "script.html"
MIN_PROMPTER_FONT_SIZE = 16
MAX_PROMPTER_FONT_SIZE = 96
DEFAULT_PROMPTER_FONT_SIZE = 36

MIN_LOOKAHEAD_WORDS = 3
MAX_LOOKAHEAD_WORDS = 50
DEFAULT_LOOKAHEAD_WORDS = 12

MIN_AUTOSCROLL_SPEED = 5.0
MAX_AUTOSCROLL_SPEED = 300.0
DEFAULT_AUTOSCROLL_SPEED = 45.0

WHISPER_SAMPLE_RATE = 16000

WHISPER_MODEL_OPTIONS: tuple[tuple[str, str], ...] = (
    ("tiny.en", "tiny.en — Fastest English (~75 MB, Recommended for Low-Latency CPU)"),
    ("base.en", "base.en — Balanced English Accuracy & Speed (~142 MB)"),
    ("small.en", "small.en — Higher English Accuracy (~466 MB, Heavier CPU)"),
    ("tiny", "tiny — Fastest Multilingual (~75 MB)"),
    ("base", "base — Balanced Multilingual (~142 MB)"),
)

WHISPER_MODEL_URLS: Dict[str, str] = {
    "tiny.en": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin",
    "base.en": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin",
    "small.en": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin",
    "tiny": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin",
    "base": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin",
}

WHISPER_CPP_REPO_URL = "https://github.com/ggml-org/whisper.cpp.git"

# Common short function words that should not trigger a distant jump on a lone single-word match
_COMMON_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "at",
        "is",
        "it",
        "be",
        "as",
        "by",
        "for",
        "if",
        "so",
        "no",
        "we",
        "he",
        "me",
        "my",
        "up",
        "do",
        "i",
    }
)

# ---------------------------------------------------------------------------
# Rich-Text HTML Sanitizer (Zero External Dependencies)
# ---------------------------------------------------------------------------

_ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "br",
        "div",
        "span",
        "b",
        "strong",
        "i",
        "em",
        "u",
        "s",
        "strike",
        "del",
        "ins",
        "sub",
        "sup",
        "h1",
        "h2",
        "h3",
        "h4",
        "ul",
        "ol",
        "li",
        "blockquote",
        "hr",
        "mark",
        "font",
    }
)

_VOID_TAGS: frozenset[str] = frozenset({"br", "hr"})
_SKIP_CONTENT_TAGS: frozenset[str] = frozenset(
    {"script", "style", "iframe", "object", "embed", "noscript", "template"}
)

_SAFE_COLOR_RE = re.compile(
    r"^(?:#[0-9a-fA-F]{3,8}|rgb\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*\)|"
    r"rgba\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*(?:0|1|0?\.\d+)\s*\)|"
    r"[a-zA-Z]{3,20})$"
)
_SAFE_ALIGN_RE = re.compile(r"^(?:left|right|center|justify|start|end)$", re.IGNORECASE)
_SAFE_WEIGHT_RE = re.compile(r"^(?:normal|bold|bolder|lighter|[1-9]00)$", re.IGNORECASE)
_SAFE_STYLE_VAL_RE = re.compile(r"^(?:normal|italic|oblique)$", re.IGNORECASE)
_SAFE_DECO_RE = re.compile(r"^(?:none|underline|line-through|overline)(?:\s+(?:underline|line-through|overline))*$", re.IGNORECASE)


def _sanitize_style_attr(raw_style: str) -> str:
    """Allow only safe inline typography/color CSS properties."""
    if not raw_style:
        return ""
    cleaned_parts: List[str] = []
    for decl in raw_style.split(";"):
        if ":" not in decl:
            continue
        prop, val = decl.split(":", 1)
        prop_l = prop.strip().lower()
        val_s = val.strip()
        if not val_s or "url(" in val_s.lower() or "expression(" in val_s.lower():
            continue
        if prop_l in ("color", "background-color"):
            if _SAFE_COLOR_RE.match(val_s):
                cleaned_parts.append(f"{prop_l}: {val_s}")
        elif prop_l == "text-align":
            if _SAFE_ALIGN_RE.match(val_s):
                cleaned_parts.append(f"text-align: {val_s.lower()}")
        elif prop_l == "font-weight":
            if _SAFE_WEIGHT_RE.match(val_s):
                cleaned_parts.append(f"font-weight: {val_s.lower()}")
        elif prop_l == "font-style":
            if _SAFE_STYLE_VAL_RE.match(val_s):
                cleaned_parts.append(f"font-style: {val_s.lower()}")
        elif prop_l == "text-decoration":
            if _SAFE_DECO_RE.match(val_s):
                cleaned_parts.append(f"text-decoration: {val_s.lower()}")
    return "; ".join(cleaned_parts)


class _ScriptHtmlSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: List[str] = []
        self._skip_depth: int = 0
        self._open_stack: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = (tag or "").lower()
        if t in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if t not in _ALLOWED_TAGS:
            return

        safe_attrs: List[str] = []
        for k, v in attrs:
            kl = (k or "").lower()
            if v is None:
                continue
            if kl == "style":
                sanitized_style = _sanitize_style_attr(v)
                if sanitized_style:
                    safe_attrs.append(f'style="{html.escape(sanitized_style, quote=True)}"')
            elif t == "font" and kl == "color" and _SAFE_COLOR_RE.match(v.strip()):
                safe_attrs.append(f'color="{html.escape(v.strip(), quote=True)}"')

        attr_str = (" " + " ".join(safe_attrs)) if safe_attrs else ""
        if t in _VOID_TAGS:
            self._out.append(f"<{t}{attr_str}>")
        else:
            self._open_stack.append(t)
            self._out.append(f"<{t}{attr_str}>")

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = (tag or "").lower()
        if self._skip_depth > 0 or t not in _ALLOWED_TAGS:
            return
        if t in _VOID_TAGS:
            self._out.append(f"<{t}>")
        else:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        t = (tag or "").lower()
        if t in _SKIP_CONTENT_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0 or t not in _ALLOWED_TAGS or t in _VOID_TAGS:
            return
        if t in self._open_stack:
            while self._open_stack:
                top = self._open_stack.pop()
                self._out.append(f"</{top}>")
                if top == t:
                    break

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if data:
            self._out.append(html.escape(data, quote=False))

    def get_html(self) -> str:
        while self._open_stack:
            self._out.append(f"</{self._open_stack.pop()}>")
        return "".join(self._out)


def sanitize_script_html(raw_html: str) -> str:
    """Sanitize rich-text script HTML to an allowlist of safe formatting tags and styles."""
    if not raw_html:
        return ""
    parser = _ScriptHtmlSanitizer()
    try:
        parser.feed(str(raw_html))
        parser.close()
    except Exception:
        return f"<p>{html.escape(str(raw_html))}</p>"
    return parser.get_html()


class _HtmlToTextParser(HTMLParser):
    _BLOCK_TAGS: frozenset[str] = frozenset(
        {"p", "div", "h1", "h2", "h3", "h4", "li", "blockquote", "hr", "br"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []
        self._skip_depth: int = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = (tag or "").lower()
        if t in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if t in ("br", "hr"):
            self._parts.append("\n")
        elif t in self._BLOCK_TAGS and self._parts and not self._parts[-1].endswith("\n"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = (tag or "").lower()
        if t in _SKIP_CONTENT_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if t in self._BLOCK_TAGS and self._parts and not self._parts[-1].endswith("\n"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if data:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def html_to_plain_text(html_content: str) -> str:
    """Convert rich script HTML to plain text preserving paragraph/line breaks."""
    if not html_content:
        return ""
    parser = _HtmlToTextParser()
    try:
        parser.feed(str(html_content))
        parser.close()
    except Exception:
        return str(html_content)
    return parser.get_text()


# ---------------------------------------------------------------------------
# Spoken-Word Tokenizer (Excludes (...), [...], {...}) & Lookahead Tracker
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*")
_OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}"}
_CLOSE_BRACKETS = frozenset(_OPEN_TO_CLOSE.values())


def normalize_word(word: str) -> str:
    """Normalize a word for speech-to-text comparison."""
    cleaned = (word or "").lower().replace("’", "'")
    cleaned = re.sub(r"[^a-z0-9']", "", cleaned)
    return cleaned.replace("'", "")


@dataclass(frozen=True)
class SpokenToken:
    """A single spoken word in the script with exact character offsets in plain text."""

    index: int
    raw: str
    normalized: str
    char_start: int
    char_end: int


def extract_spoken_tokens(plain_text: str) -> Tuple[List[SpokenToken], List[Tuple[int, int]]]:
    """Extract spoken word tokens while excluding any text inside (...), [...], or {...}.

    Returns:
        (spoken_tokens, non_spoken_spans) where ``non_spoken_spans`` is a list of
        ``(start, end)`` character offsets for bracketed script directions.
    """
    text = str(plain_text or "")
    n = len(text)
    if n == 0:
        return ([], [])

    non_spoken_spans: List[Tuple[int, int]] = []
    bracket_stack: List[str] = []
    span_start: Optional[int] = None

    for idx, ch in enumerate(text):
        if ch in _OPEN_TO_CLOSE:
            if not bracket_stack:
                span_start = idx
            bracket_stack.append(_OPEN_TO_CLOSE[ch])
        elif ch in _CLOSE_BRACKETS and bracket_stack:
            if ch == bracket_stack[-1]:
                bracket_stack.pop()
            elif ch in bracket_stack:
                # Recover gracefully from mismatched nested brackets
                while bracket_stack and bracket_stack[-1] != ch:
                    bracket_stack.pop()
                if bracket_stack:
                    bracket_stack.pop()
            if not bracket_stack and span_start is not None:
                non_spoken_spans.append((span_start, idx + 1))
                span_start = None

    # Unclosed trailing bracket still counts as non-spoken instruction
    if bracket_stack and span_start is not None:
        non_spoken_spans.append((span_start, n))

    # Build spoken segments outside non_spoken_spans
    spoken_tokens: List[SpokenToken] = []
    seg_start = 0
    for b_start, b_end in non_spoken_spans:
        if b_start > seg_start:
            _append_words_from_segment(text, seg_start, b_start, spoken_tokens)
        seg_start = max(seg_start, b_end)
    if seg_start < n:
        _append_words_from_segment(text, seg_start, n, spoken_tokens)

    return (spoken_tokens, non_spoken_spans)


def _append_words_from_segment(
    text: str, seg_start: int, seg_end: int, out: List[SpokenToken]
) -> None:
    segment = text[seg_start:seg_end]
    for match in _WORD_RE.finditer(segment):
        raw = match.group(0)
        norm = normalize_word(raw)
        if not norm:
            continue
        out.append(
            SpokenToken(
                index=len(out),
                raw=raw,
                normalized=norm,
                char_start=seg_start + match.start(),
                char_end=seg_start + match.end(),
            )
        )


def _words_similar(a: str, b: str) -> bool:
    """Return True if two normalized words match exactly or closely (for Whisper STT)."""
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 4 and len(b) >= 4:
        if a.startswith(b) or b.startswith(a):
            if abs(len(a) - len(b)) <= 2:
                return True
        if SequenceMatcher(None, a, b).ratio() >= 0.80:
            return True
    return False


@dataclass
class ScriptAnchor:
    """Captures the talent's reading position before a live script edit."""

    token_index: int = 0
    before_words: Tuple[str, ...] = ()
    at_words: Tuple[str, ...] = ()
    scroll_ratio: float = 0.0
    total_tokens: int = 0


class PrompterTracker:
    """Tracks spoken script position, bracket-excluded lookahead, STT matching, and edit re-anchoring."""

    def __init__(self, lookahead_words: int = DEFAULT_LOOKAHEAD_WORDS) -> None:
        self._lock = threading.RLock()
        self._lookahead_words: int = max(
            MIN_LOOKAHEAD_WORDS, min(MAX_LOOKAHEAD_WORDS, int(lookahead_words))
        )
        self._plain_text: str = ""
        self._tokens: List[SpokenToken] = []
        self._non_spoken_spans: List[Tuple[int, int]] = []
        self._current_index: int = 0

    @property
    def lookahead_words(self) -> int:
        with self._lock:
            return self._lookahead_words

    @lookahead_words.setter
    def lookahead_words(self, value: int) -> None:
        with self._lock:
            self._lookahead_words = max(
                MIN_LOOKAHEAD_WORDS, min(MAX_LOOKAHEAD_WORDS, int(value))
            )

    @property
    def current_index(self) -> int:
        with self._lock:
            return self._current_index

    @property
    def tokens(self) -> List[SpokenToken]:
        with self._lock:
            return list(self._tokens)

    @property
    def non_spoken_spans(self) -> List[Tuple[int, int]]:
        with self._lock:
            return list(self._non_spoken_spans)

    @property
    def total_tokens(self) -> int:
        with self._lock:
            return len(self._tokens)

    def set_text(self, plain_text: str, reset_cursor: bool = False) -> None:
        with self._lock:
            self._plain_text = str(plain_text or "")
            self._tokens, self._non_spoken_spans = extract_spoken_tokens(self._plain_text)
            if reset_cursor:
                self._current_index = 0
            elif self._tokens:
                self._current_index = max(0, min(self._current_index, len(self._tokens) - 1))
            else:
                self._current_index = 0

    def set_cursor(self, token_index: int) -> int:
        with self._lock:
            if not self._tokens:
                self._current_index = 0
                return 0
            self._current_index = max(0, min(int(token_index), len(self._tokens) - 1))
            return self._current_index

    def set_cursor_by_char_offset(self, char_offset: int) -> int:
        """Jump cursor to the spoken token at or immediately after ``char_offset``."""
        with self._lock:
            if not self._tokens:
                self._current_index = 0
                return 0
            pos = max(0, int(char_offset))
            for tok in self._tokens:
                if tok.char_end >= pos:
                    self._current_index = tok.index
                    return self._current_index
            self._current_index = len(self._tokens) - 1
            return self._current_index

    def get_current_token(self) -> Optional[SpokenToken]:
        with self._lock:
            if not self._tokens:
                return None
            idx = max(0, min(self._current_index, len(self._tokens) - 1))
            return self._tokens[idx]

    def get_lookahead_tokens(self) -> List[SpokenToken]:
        """Return only the next ``lookahead_words`` spoken tokens (excluding (...), [...], {...})."""
        with self._lock:
            if not self._tokens:
                return []
            start = max(0, min(self._current_index, len(self._tokens)))
            end = min(len(self._tokens), start + self._lookahead_words)
            return list(self._tokens[start:end])

    def get_lookahead_prompt(self) -> str:
        """Build a clean Whisper initial_prompt string containing only spoken lookahead words."""
        return " ".join(tok.raw for tok in self.get_lookahead_tokens())

    def match_transcript(self, transcript: str) -> Optional[int]:
        """Match recognized speech against the spoken lookahead window.

        Returns the new ``current_index`` if the cursor advanced, or ``None`` if no match.
        """
        raw_words = [normalize_word(m.group(0)) for m in _WORD_RE.finditer(transcript or "")]
        heard = [w for w in raw_words if w]
        if not heard:
            return None

        with self._lock:
            if not self._tokens or self._current_index >= len(self._tokens):
                return None

            win_start = self._current_index
            win_end = min(len(self._tokens), win_start + self._lookahead_words)
            window = self._tokens[win_start:win_end]
            if not window:
                return None

            # Use the tail of the recognized chunk (most recently spoken words)
            tail = heard[-10:]
            best_advance_idx: Optional[int] = None
            best_score: float = 0.0

            # 1. Prefer multi-word phrase matches (2+ consecutive words in lookahead)
            if len(tail) >= 2:
                for w_off in range(len(window)):
                    for h_idx in range(len(tail)):
                        if not _words_similar(tail[h_idx], window[w_off].normalized):
                            continue
                        run_len = 1
                        while (
                            h_idx + run_len < len(tail)
                            and w_off + run_len < len(window)
                            and _words_similar(
                                tail[h_idx + run_len],
                                window[w_off + run_len].normalized,
                            )
                        ):
                            run_len += 1
                        if run_len >= 2:
                            matched_end = win_start + w_off + run_len - 1
                            # Favor longer runs and matches closer to the end of heard speech
                            recency_bonus = (h_idx + run_len) / float(len(tail))
                            distance_penalty = w_off * 0.05
                            score = (run_len * 2.0) + recency_bonus - distance_penalty
                            if score > best_score:
                                best_score = score
                                best_advance_idx = min(len(self._tokens) - 1, matched_end + 1)

            # 2. Single-word match fallback (allow immediate next words or distinctive content words)
            if best_advance_idx is None:
                for h_word in reversed(tail[-4:]):
                    for w_off, tok in enumerate(window):
                        if not _words_similar(h_word, tok.normalized):
                            continue
                        # Avoid jumping far ahead on tiny stop-words like "the", "a", "to"
                        if (
                            tok.normalized in _COMMON_STOP_WORDS
                            or len(tok.normalized) <= 2
                        ) and w_off > 1:
                            continue
                        best_advance_idx = min(len(self._tokens) - 1, win_start + w_off + 1)
                        break
                    if best_advance_idx is not None:
                        break

            if best_advance_idx is not None and best_advance_idx > self._current_index:
                self._current_index = best_advance_idx
                return self._current_index
            return None

    def capture_anchor(self, scroll_ratio: float = 0.0) -> ScriptAnchor:
        """Snapshot current reading position and surrounding spoken words before an edit."""
        with self._lock:
            idx = self._current_index
            before_start = max(0, idx - 3)
            after_end = min(len(self._tokens), idx + 5)
            before = tuple(t.normalized for t in self._tokens[before_start:idx])
            at = tuple(t.normalized for t in self._tokens[idx:after_end])
            return ScriptAnchor(
                token_index=idx,
                before_words=before,
                at_words=at,
                scroll_ratio=max(0.0, min(1.0, float(scroll_ratio))),
                total_tokens=len(self._tokens),
            )

    def update_text_preserving_position(
        self, new_plain_text: str, anchor: Optional[ScriptAnchor] = None
    ) -> Tuple[int, float]:
        """Replace script text and re-anchor cursor so edits never lose the talent's position.

        Returns ``(new_token_index, fallback_scroll_ratio)``.
        """
        with self._lock:
            snap = anchor if anchor is not None else self.capture_anchor()
            self._plain_text = str(new_plain_text or "")
            self._tokens, self._non_spoken_spans = extract_spoken_tokens(self._plain_text)

            if not self._tokens:
                self._current_index = 0
                return (0, snap.scroll_ratio)

            new_norms = [t.normalized for t in self._tokens]
            best_idx: Optional[int] = None
            best_key: Tuple[int, int] = (-1, 10**9)  # (match_length, -distance)

            # Try matching the exact phrase starting at the old cursor (3..5 words, then 2 words)
            for phrase_len in (min(5, len(snap.at_words)), min(3, len(snap.at_words)), min(2, len(snap.at_words))):
                if phrase_len < 2:
                    continue
                target = list(snap.at_words[:phrase_len])
                for i in range(0, len(new_norms) - phrase_len + 1):
                    if new_norms[i : i + phrase_len] == target:
                        dist = abs(i - snap.token_index)
                        cand_key = (phrase_len, -dist)
                        if cand_key > best_key:
                            best_key = cand_key
                            best_idx = i
                if best_idx is not None:
                    break

            # Fallback: match the words immediately preceding the old cursor
            if best_idx is None and len(snap.before_words) >= 2:
                target = list(snap.before_words[-2:])
                for i in range(0, len(new_norms) - 2 + 1):
                    if new_norms[i : i + 2] == target:
                        cand_pos = min(len(self._tokens) - 1, i + 2)
                        dist = abs(cand_pos - snap.token_index)
                        cand_key = (2, -dist)
                        if cand_key > best_key:
                            best_key = cand_key
                            best_idx = cand_pos

            # Fallback: proportional position by token count / scroll ratio
            if best_idx is None:
                if snap.total_tokens > 0:
                    ratio = snap.token_index / float(max(1, snap.total_tokens))
                else:
                    ratio = snap.scroll_ratio
                best_idx = int(round(ratio * max(0, len(self._tokens) - 1)))

            self._current_index = max(0, min(best_idx, len(self._tokens) - 1))
            return (self._current_index, snap.scroll_ratio)


# ---------------------------------------------------------------------------
# Pluggable Whisper AI Speech-to-Text Backend & Background Worker
# ---------------------------------------------------------------------------

def get_whisper_dir(storage_dir: Optional[Path] = None) -> Path:
    """Return the persistent directory on /storage for Whisper models and whisper-cli."""
    base = Path(storage_dir) if storage_dir is not None else Path("/storage")
    return base / ".alpine-recorder" / "whisper"


def _find_whisper_cli_binary(
    custom_binary: str = "", storage_dir: Optional[Path] = None
) -> Optional[str]:
    candidates: List[str] = []
    if custom_binary and str(custom_binary).strip():
        candidates.append(str(custom_binary).strip())
    if storage_dir is not None:
        candidates.append(str(get_whisper_dir(storage_dir) / "bin" / "whisper-cli"))
    candidates.extend(
        [
            "/storage/.alpine-recorder/whisper/bin/whisper-cli",
            "/opt/alpine-recorder/bin/whisper-cli",
            "whisper-cli",
            "whisper-cpp",
            "/usr/local/bin/whisper-cli",
            "/usr/bin/whisper-cli",
        ]
    )
    for cand in candidates:
        p = Path(cand)
        if p.is_file() and os.access(str(p), os.X_OK):
            return str(p)
        resolved = shutil.which(cand)
        if resolved:
            return resolved
    return None


def _find_ggml_model_file(model_name: str, storage_dir: Optional[Path] = None) -> Optional[Path]:
    raw = (model_name or "tiny.en").strip()
    if raw:
        direct = Path(raw)
        if direct.is_file():
            return direct
    fname = raw if raw.endswith(".bin") else f"ggml-{raw}.bin"
    search_dirs: List[Path] = []
    if storage_dir is not None:
        search_dirs.append(Path(storage_dir) / ".alpine-recorder" / "whisper")
        search_dirs.append(Path(storage_dir) / "whisper")
    search_dirs.extend(
        [
            Path("/storage/.alpine-recorder/whisper"),
            Path("/opt/alpine-recorder/whisper"),
            Path("/usr/share/whisper"),
            Path.home() / ".cache" / "whisper",
        ]
    )
    for d in search_dirs:
        cand = d / fname
        if cand.is_file():
            return cand
    return None


def detect_whisper_backend(
    model_name: str = "tiny.en",
    custom_binary: str = "",
    storage_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Probe available local Whisper AI backends without blocking or crashing."""
    try:
        import faster_whisper  # type: ignore[import-not-found]  # noqa: F401

        return {
            "available": True,
            "backend": "faster-whisper",
            "detail": f"faster-whisper (Python CTranslate2, model: {model_name})",
        }
    except Exception:
        pass

    try:
        import pywhispercpp  # type: ignore[import-not-found]  # noqa: F401

        return {
            "available": True,
            "backend": "pywhispercpp",
            "detail": f"pywhispercpp (whisper.cpp Python bindings, model: {model_name})",
        }
    except Exception:
        pass

    cli_bin = _find_whisper_cli_binary(custom_binary, storage_dir=storage_dir)
    if cli_bin:
        ggml = _find_ggml_model_file(model_name, storage_dir=storage_dir)
        if ggml is not None:
            return {
                "available": True,
                "backend": "whisper-cli",
                "binary": cli_bin,
                "model_path": str(ggml),
                "detail": f"whisper.cpp CLI ({Path(cli_bin).name}, {ggml.name})",
            }
        return {
            "available": False,
            "backend": "whisper-cli",
            "binary": cli_bin,
            "detail": (
                f"whisper-cli found ({cli_bin}), but GGML model 'ggml-{model_name}.bin' "
                "was not found in /storage/.alpine-recorder/whisper/"
            ),
        }

    try:
        import whisper  # type: ignore[import-not-found]  # noqa: F401

        return {
            "available": True,
            "backend": "openai-whisper",
            "detail": f"openai-whisper (Python, model: {model_name})",
        }
    except Exception:
        pass

    return {
        "available": False,
        "backend": "none",
        "detail": (
            "No local Whisper backend detected. Install 'faster-whisper', 'pywhispercpp', "
            "or place 'whisper-cli' + 'ggml-tiny.en.bin' in /storage/.alpine-recorder/whisper/."
        ),
    }


class WhisperSpeechWorker:
    """Background low-latency Whisper STT worker gated by mic mute and Talkback state."""

    def __init__(
        self,
        tracker: PrompterTracker,
        on_advance: Optional[Callable[[int, str], None]] = None,
        on_status: Optional[Callable[[str, str], None]] = None,
        model_name: str = "tiny.en",
        custom_binary: str = "",
        storage_dir: Optional[Path] = None,
        transcribe_fn: Optional[Callable[[np.ndarray, str], str]] = None,
    ) -> None:
        self.tracker = tracker
        self.on_advance = on_advance
        self.on_status = on_status
        self.model_name = (model_name or "tiny.en").strip() or "tiny.en"
        self.custom_binary = (custom_binary or "").strip()
        self.storage_dir = storage_dir
        self._custom_transcribe_fn = transcribe_fn

        self._lock = threading.Lock()
        self._running = threading.Event()
        self._enabled = False
        self._talkback_active = False
        self._thread: Optional[threading.Thread] = None

        # Rolling 2.0-second 16 kHz mono buffer
        self._max_buf_samples = int(WHISPER_SAMPLE_RATE * 2.0)
        self._audio_chunks: List[np.ndarray] = []
        self._buffered_samples: int = 0
        self._new_samples_since_infer: int = 0

        self._backend_name: str = "none"
        self._model_obj: Any = None
        self._cli_binary: Optional[str] = None
        self._cli_model_path: Optional[Path] = None
        self._status_code: str = "idle"
        self._status_message: str = "AI Follow Off"

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    @property
    def status(self) -> Tuple[str, str]:
        return (self._status_code, self._status_message)

    def _emit_status(self, code: str, message: str) -> None:
        self._status_code = code
        self._status_message = message
        if self.on_status is not None:
            try:
                self.on_status(code, message)
            except Exception:
                pass

    def set_talkback_active(self, active: bool) -> None:
        with self._lock:
            self._talkback_active = bool(active)
            if self._talkback_active:
                self._audio_chunks.clear()
                self._buffered_samples = 0
                self._new_samples_since_infer = 0

    def push_audio(
        self, mono_samples: np.ndarray, sample_rate: int, is_muted: bool
    ) -> None:
        """Feed live microphone audio from AudioEngine (non-blocking)."""
        if not self._running.is_set() or is_muted:
            return
        if mono_samples is None or mono_samples.size == 0:
            return
        with self._lock:
            if self._talkback_active:
                return
        resampled = resample_mono_signal(
            mono_samples.astype(np.float32, copy=False),
            int(sample_rate),
            WHISPER_SAMPLE_RATE,
        )
        if resampled.size == 0:
            return
        with self._lock:
            self._audio_chunks.append(resampled)
            self._buffered_samples += int(resampled.size)
            self._new_samples_since_infer += int(resampled.size)
            while self._buffered_samples > self._max_buf_samples and len(self._audio_chunks) > 1:
                dropped = self._audio_chunks.pop(0)
                self._buffered_samples -= int(dropped.size)

    def start(self) -> bool:
        if self._running.is_set():
            return True
        if self._custom_transcribe_fn is None:
            info = detect_whisper_backend(
                model_name=self.model_name,
                custom_binary=self.custom_binary,
                storage_dir=self.storage_dir,
            )
            if not info.get("available"):
                self._emit_status("unavailable", str(info.get("detail") or "Whisper unavailable"))
                return False
            self._backend_name = str(info["backend"])
            self._cli_binary = info.get("binary")
            self._cli_model_path = Path(info["model_path"]) if info.get("model_path") else None
        else:
            self._backend_name = "custom"

        self._running.set()
        self._emit_status("loading", f"Loading Whisper ({self._backend_name})...")
        self._thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="PrompterWhisperSTT"
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running.clear()
        t = self._thread
        self._thread = None
        if t is not None and t.is_alive():
            t.join(timeout=1.5)
        with self._lock:
            self._audio_chunks.clear()
            self._buffered_samples = 0
            self._new_samples_since_infer = 0
        self._emit_status("idle", "AI Follow Off")

    def _load_backend_model(self) -> bool:
        if self._custom_transcribe_fn is not None:
            return True
        try:
            if self._backend_name == "faster-whisper":
                from faster_whisper import WhisperModel  # type: ignore[import-not-found]

                self._model_obj = WhisperModel(
                    self.model_name, device="cpu", compute_type="int8"
                )
                return True
            if self._backend_name == "pywhispercpp":
                from pywhispercpp.model import Model  # type: ignore[import-not-found]

                self._model_obj = Model(self.model_name, print_realtime=False, print_progress=False)
                return True
            if self._backend_name == "whisper-cli":
                return bool(self._cli_binary and self._cli_model_path and self._cli_model_path.is_file())
            if self._backend_name == "openai-whisper":
                import whisper  # type: ignore[import-not-found]

                self._model_obj = whisper.load_model(self.model_name)
                return True
        except Exception as exc:
            logger.warning("Failed to initialize Whisper backend %s: %s", self._backend_name, exc)
            self._emit_status("error", f"Whisper load failed: {exc}")
            return False
        return False

    def _transcribe_buffer(self, audio_16k: np.ndarray, prompt: str) -> str:
        if self._custom_transcribe_fn is not None:
            return str(self._custom_transcribe_fn(audio_16k, prompt) or "")

        if self._backend_name == "faster-whisper" and self._model_obj is not None:
            segments, _info = self._model_obj.transcribe(
                audio_16k,
                language="en" if self.model_name.endswith(".en") else None,
                initial_prompt=prompt or None,
                beam_size=1,
                vad_filter=False,
                condition_on_previous_text=False,
            )
            return " ".join(seg.text.strip() for seg in segments if getattr(seg, "text", ""))

        if self._backend_name == "pywhispercpp" and self._model_obj is not None:
            segments = self._model_obj.transcribe(audio_16k, initial_prompt=prompt or "")
            return " ".join(getattr(seg, "text", "").strip() for seg in segments)

        if self._backend_name == "whisper-cli" and self._cli_binary and self._cli_model_path:
            return self._transcribe_via_cli(audio_16k, prompt)

        if self._backend_name == "openai-whisper" and self._model_obj is not None:
            res = self._model_obj.transcribe(
                audio_16k,
                fp16=False,
                initial_prompt=prompt or None,
                condition_on_previous_text=False,
            )
            return str(res.get("text") or "")

        return ""

    def _transcribe_via_cli(self, audio_16k: np.ndarray, prompt: str) -> str:
        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                tmp_path = tf.name
            pcm16 = (np.clip(audio_16k, -1.0, 1.0) * 32767.0).astype("<i2")
            with wave.open(tmp_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(WHISPER_SAMPLE_RATE)
                wf.writeframes(pcm16.tobytes())

            cmd = [
                str(self._cli_binary),
                "-m",
                str(self._cli_model_path),
                "-f",
                tmp_path,
                "-nt",
                "-np",
                "-t",
                "2",
            ]
            if prompt:
                cmd.extend(["--prompt", prompt[:240]])
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=4.0,
                check=False,
            )
            return (proc.stdout or "").strip()
        except Exception as exc:
            logger.debug("whisper-cli invocation failed: %s", exc)
            return ""
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _worker_loop(self) -> None:
        if not self._load_backend_model():
            self._running.clear()
            return
        self._emit_status("listening", f"AI Listening ({self._backend_name})")

        min_new_samples = int(WHISPER_SAMPLE_RATE * 0.45)
        min_window_samples = int(WHISPER_SAMPLE_RATE * 0.50)

        while self._running.is_set():
            time.sleep(0.12)
            with self._lock:
                if (
                    self._talkback_active
                    or self._new_samples_since_infer < min_new_samples
                    or self._buffered_samples < min_window_samples
                ):
                    continue
                audio_window = np.concatenate(self._audio_chunks)
                if audio_window.size > self._max_buf_samples:
                    audio_window = audio_window[-self._max_buf_samples :]
                self._new_samples_since_infer = 0

            # Simple energy VAD to avoid burning CPU on silence
            rms = float(np.sqrt(np.mean(audio_window * audio_window)))
            if rms < 0.008:
                continue

            prompt = self.tracker.get_lookahead_prompt()
            if not prompt:
                continue

            try:
                transcript = self._transcribe_buffer(audio_window, prompt)
            except Exception as exc:
                logger.debug("Whisper transcription error: %s", exc)
                continue

            if not transcript:
                continue

            new_idx = self.tracker.match_transcript(transcript)
            if new_idx is not None and self.on_advance is not None:
                try:
                    self.on_advance(new_idx, transcript)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Whisper AI Model & Engine Installer (For Settings UI, install.sh & CLI)
# ---------------------------------------------------------------------------

def download_whisper_model(
    model_name: str = "tiny.en",
    storage_dir: Optional[Path] = None,
    on_progress: Optional[Callable[[int, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    url_override: Optional[str] = None,
) -> Tuple[bool, str, Optional[Path]]:
    """Download a GGML Whisper model into persistent /storage/.alpine-recorder/whisper/."""
    import urllib.request

    clean_model = (model_name or "tiny.en").strip() or "tiny.en"
    url = url_override or WHISPER_MODEL_URLS.get(clean_model)
    if not url:
        return (False, f"Unknown Whisper model '{clean_model}'.", None)

    whisper_dir = get_whisper_dir(storage_dir)
    try:
        whisper_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return (False, f"Could not create whisper directory {whisper_dir}: {exc}", None)

    fname = clean_model if clean_model.endswith(".bin") else f"ggml-{clean_model}.bin"
    dest_path = whisper_dir / fname
    if dest_path.is_file() and dest_path.stat().st_size > 1024 * 1024:
        if on_progress:
            on_progress(100, f"Model '{fname}' is already installed.")
        return (True, f"Model '{fname}' already installed at {dest_path}.", dest_path)

    tmp_path = whisper_dir / f".{fname}.part"
    if on_progress:
        on_progress(5, f"Downloading {fname}...")

    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Alpine-Recording-Station/1.0"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            total_hdr = resp.headers.get("Content-Length")
            total_bytes = int(total_hdr) if total_hdr and total_hdr.isdigit() else 0
            downloaded = 0
            with tmp_path.open("wb") as out_f:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        tmp_path.unlink(missing_ok=True)
                        return (False, "Download cancelled.", None)
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out_f.write(chunk)
                    downloaded += len(chunk)
                    if on_progress:
                        if total_bytes > 0:
                            pct = min(99, max(5, int(round((downloaded / total_bytes) * 100))))
                            dl_mb = downloaded / (1024.0 * 1024.0)
                            tot_mb = total_bytes / (1024.0 * 1024.0)
                            on_progress(pct, f"Downloading {fname}: {dl_mb:.1f} / {tot_mb:.1f} MB ({pct}%)")
                        else:
                            dl_mb = downloaded / (1024.0 * 1024.0)
                            on_progress(50, f"Downloading {fname}: {dl_mb:.1f} MB...")
                out_f.flush()
                if hasattr(os, "fsync"):
                    try:
                        os.fsync(out_f.fileno())
                    except OSError:
                        pass
        tmp_path.replace(dest_path)
        if on_progress:
            on_progress(100, f"Downloaded {fname} successfully.")
        return (True, f"Downloaded {fname} to {dest_path}.", dest_path)
    except Exception as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return (False, f"Failed to download {fname}: {exc}", None)


def install_whisper_cli_binary(
    storage_dir: Optional[Path] = None,
    custom_binary: str = "",
    on_progress: Optional[Callable[[int, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Tuple[bool, str, Optional[Path]]:
    """Ensure a standalone whisper-cli binary is installed (persisted on /storage)."""
    existing = _find_whisper_cli_binary(custom_binary, storage_dir=storage_dir)
    if existing:
        if on_progress:
            on_progress(100, f"Found existing whisper-cli at {existing}.")
        return (True, f"Found whisper-cli at {existing}.", Path(existing))

    if os.name != "posix":
        return (
            False,
            "Automatic whisper-cli compilation is supported on Alpine Linux. "
            "On Windows/macOS, install 'faster-whisper' via pip or place whisper-cli on PATH.",
            None,
        )

    whisper_bin_dir = get_whisper_dir(storage_dir) / "bin"
    whisper_bin_dir.mkdir(parents=True, exist_ok=True)
    dest_bin = whisper_bin_dir / "whisper-cli"

    apk_bin = shutil.which("apk")
    if apk_bin:
        if on_progress:
            on_progress(10, "Checking Alpine package repository for whisper.cpp...")
        for pkg in ("whisper.cpp", "whisper-cpp"):
            proc = subprocess.run(
                [apk_bin, "add", pkg],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if proc.returncode == 0:
                found = _find_whisper_cli_binary(custom_binary, storage_dir=storage_dir)
                if found:
                    if on_progress:
                        on_progress(100, f"Installed {pkg} via apk ({found}).")
                    return (True, f"Installed {pkg} via apk.", Path(found))

        if on_progress:
            on_progress(18, "Installing Alpine C++ build tools (git, cmake, make, g++)...")
        subprocess.run(
            [apk_bin, "add", "git", "cmake", "make", "g++", "linux-headers"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    git_bin = shutil.which("git")
    cmake_bin = shutil.which("cmake")
    if not git_bin or not cmake_bin:
        return (
            False,
            "Missing git or cmake to build whisper.cpp. Run 'apk add git cmake make g++' and retry.",
            None,
        )

    tmp_build = Path(tempfile.mkdtemp(prefix="whisper_build_"))
    try:
        if cancel_event is not None and cancel_event.is_set():
            return (False, "Installation cancelled.", None)
        if on_progress:
            on_progress(30, "Cloning whisper.cpp source...")
        src_dir = tmp_build / "whisper.cpp"
        clone_proc = subprocess.run(
            [git_bin, "clone", "--depth", "1", WHISPER_CPP_REPO_URL, str(src_dir)],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if clone_proc.returncode != 0:
            return (
                False,
                f"git clone whisper.cpp failed: {(clone_proc.stderr or '').strip()[:200]}",
                None,
            )

        if cancel_event is not None and cancel_event.is_set():
            return (False, "Installation cancelled.", None)

        if on_progress:
            on_progress(50, "Configuring static whisper-cli build (CMake)...")
        build_dir = src_dir / "build"
        cfg_proc = subprocess.run(
            [
                cmake_bin,
                "-B",
                str(build_dir),
                "-S",
                str(src_dir),
                "-DCMAKE_BUILD_TYPE=Release",
                "-DBUILD_SHARED_LIBS=OFF",
                "-DWHISPER_BUILD_TESTS=OFF",
                "-DWHISPER_BUILD_EXAMPLES=ON",
                "-DGGML_NATIVE=OFF",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if cfg_proc.returncode != 0:
            return (
                False,
                f"CMake configure failed: {(cfg_proc.stderr or '').strip()[:200]}",
                None,
            )

        if cancel_event is not None and cancel_event.is_set():
            return (False, "Installation cancelled.", None)

        if on_progress:
            on_progress(70, "Compiling whisper-cli (this may take 1–2 minutes)...")
        jobs = str(max(1, min(4, os.cpu_count() or 2)))
        bld_proc = subprocess.run(
            [cmake_bin, "--build", str(build_dir), "--target", "whisper-cli", "-j", jobs],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if bld_proc.returncode != 0:
            return (
                False,
                f"whisper-cli build failed: {(bld_proc.stderr or '').strip()[:200]}",
                None,
            )

        built_candidates = [
            build_dir / "bin" / "whisper-cli",
            build_dir / "whisper-cli",
        ]
        built_bin = next((p for p in built_candidates if p.is_file()), None)
        if built_bin is None:
            return (False, "Build succeeded but whisper-cli binary was not found.", None)

        shutil.copy2(str(built_bin), str(dest_bin))
        os.chmod(dest_bin, 0o755)
        if on_progress:
            on_progress(100, f"Installed whisper-cli to {dest_bin}.")
        return (True, f"Installed whisper-cli to {dest_bin}.", dest_bin)
    except Exception as exc:
        return (False, f"whisper-cli installation error: {exc}", None)
    finally:
        shutil.rmtree(tmp_build, ignore_errors=True)


def install_whisper_all(
    model_name: str = "tiny.en",
    storage_dir: Optional[Path] = None,
    custom_binary: str = "",
    on_progress: Optional[Callable[[int, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Tuple[bool, str]:
    """Install/verify both the Whisper engine (if needed) and the selected GGML model."""
    info = detect_whisper_backend(
        model_name=model_name,
        custom_binary=custom_binary,
        storage_dir=storage_dir,
    )
    if info.get("available"):
        if on_progress:
            on_progress(100, f"Whisper AI already ready: {info.get('detail')}")
        return (True, f"Whisper AI ready: {info.get('detail')}")

    # Step 1: Download the GGML model first (works cross-platform and fast to verify network)
    def _dl_prog(pct: int, text: str) -> None:
        if on_progress:
            on_progress(int(pct * 0.5), text)

    ok_model, msg_model, _model_path = download_whisper_model(
        model_name=model_name,
        storage_dir=storage_dir,
        on_progress=_dl_prog,
        cancel_event=cancel_event,
    )
    if not ok_model:
        return (False, msg_model)

    # Step 2: Ensure whisper-cli binary is installed (unless a Python backend exists)
    info_after_dl = detect_whisper_backend(
        model_name=model_name,
        custom_binary=custom_binary,
        storage_dir=storage_dir,
    )
    if info_after_dl.get("available"):
        if on_progress:
            on_progress(100, f"Whisper AI ready: {info_after_dl.get('detail')}")
        return (True, f"Whisper AI ready: {info_after_dl.get('detail')}")

    def _bin_prog(pct: int, text: str) -> None:
        if on_progress:
            on_progress(50 + int(pct * 0.5), text)

    ok_bin, msg_bin, _bin_path = install_whisper_cli_binary(
        storage_dir=storage_dir,
        custom_binary=custom_binary,
        on_progress=_bin_prog,
        cancel_event=cancel_event,
    )
    if not ok_bin:
        return (False, f"Model downloaded, but engine setup incomplete: {msg_bin}")

    if on_progress:
        on_progress(100, f"Whisper AI ({model_name}) installed and ready.")
    return (True, f"Installed Whisper AI ({model_name}) to {get_whisper_dir(storage_dir)}.")


def run_whisper_installer_cli(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint: python3 -m app.prompter_engine install-whisper [--model tiny.en]."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="alpine-recorder install-whisper",
        description="Install whisper.cpp engine and GGML model onto persistent /storage.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="install-whisper",
        choices=["install-whisper", "status"],
    )
    parser.add_argument(
        "--model",
        default="tiny.en",
        help="Whisper model to download (tiny.en, base.en, small.en, tiny, base).",
    )
    parser.add_argument(
        "--storage-dir",
        default=os.environ.get("ALPINE_RECORDER_STORAGE", "/storage"),
        help="Persistent storage directory (default: /storage).",
    )
    args = parser.parse_args(argv)
    storage_p = Path(args.storage_dir)

    if args.command == "status":
        info = detect_whisper_backend(model_name=args.model, storage_dir=storage_p)
        print(f"Available: {info.get('available')}")
        print(f"Backend:   {info.get('backend')}")
        print(f"Detail:    {info.get('detail')}")
        return 0 if info.get("available") else 1

    last_msg = [""]

    def _cb(pct: int, msg: str) -> None:
        if msg != last_msg[0]:
            print(f"[{pct:3d}%] {msg}", flush=True)
            last_msg[0] = msg

    ok, detail = install_whisper_all(
        model_name=args.model,
        storage_dir=storage_p,
        on_progress=_cb,
    )
    if ok:
        print(f"[OK] {detail}")
        return 0
    print(f"[FAIL] {detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(run_whisper_installer_cli())
