"""ConsoleTee: thread-safe sys.stdout / sys.stderr splitter that simultaneously
writes to the original console streams AND streams line-by-line to the Thinking
Log Panel (via QtBackdropManager).

Designed so all `print(...)` output from the backend server appears BOTH in the
terminal AND in the macOS-style Thinking Log card window — zero code changes
required for existing logging / print statements.

Features
--------
* **Line-buffered coalescing**: partial `write()`s are buffered until a newline
  is seen (or the buffer exceeds a soft cap).  This avoids spamming the panel
  with 1-char fragments when output is produced by a loop.
* **Bounded memory**: line_buffer has a hard cap (LINE_MAX_BYTES) so a
  malformed print without a newline cannot grow memory unboundedly.
* **Thread-safe**: single RLock protects both stream writes and internal state.
* **Backdrop manager laziness**: `get_backdrop_manager()` is only called once
  per line-flush when the manager is known to be ready (not at every write).
* **Passthrough attributes**: unknown attribute access is delegated to the
  wrapped stream so third-party libs that access .buffer / .encoding / .isatty
  keep working correctly.

Usage (run once early in server.py main(), before any print()):
    from front.console_tee import install_console_tee
    install_console_tee()
"""

from __future__ import annotations

import io
import sys
import threading
from typing import Callable, Optional, TextIO


LINE_MAX_BYTES = 16 * 1024        # Soft cap for a single line before forcing flush
_FLUSH_SEP = ('\n', '\r')         # Characters that trigger a line flush


class ConsoleTee:
    """TextIO-like wrapper that T-s original stream + thinking log append."""

    def __init__(
        self,
        underlying: TextIO,
        *,
        source_label: str,
        tag_label: str,
        append_callback: Optional[Callable[[str, str, str], None]] = None,
    ):
        """
        Parameters
        ----------
        underlying:
            Original sys.stdout / sys.stderr stream to keep writing to.
        source_label:
            Shown in the Thinking Log line prefix, e.g. "TERM" / "TERM-ERR".
        tag_label:
            Shown as #tag in the Thinking Log line, e.g. "console" / "stderr".
        append_callback:
            Optional override; when None the class falls back to calling
            `get_backdrop_manager().append_thinking_log(...)` on each flush.
        """
        self._underlying = underlying
        self._source = source_label
        self._tag = tag_label
        self._cb = append_callback

        self._lock = threading.RLock()
        self._buf: list[str] = []       # Chunks pending a newline
        self._buf_bytes = 0              # Approximate running size of _buf
        self._closed = False

        # Fallback backend: imported lazily to avoid circular import during
        # front package bootstrap (which may import modules that print).
        self._backdrop_getter = None
        self._backdrop_ready = False

    # ── Internal helpers ────────────────────────────────────────────────
    @staticmethod
    def _resolve_default_config_path():
        """与 server.py start_backdrop_window() 完全一致的绝对路径解析：
        front 包的父目录/scrcpycontrol/display_config.json
        避免 console_tee 早期调用无参 get_backdrop_manager() 导致单例 config_path=None。
        """
        try:
            import os as _os
            here = _os.path.dirname(_os.path.abspath(__file__))  # front/
            project_root = _os.path.dirname(here)                # Open-AutoGLM/
            return _os.path.join(project_root, "scrcpycontrol", "display_config.json")
        except Exception:
            return None

    def _get_backend(self):
        if self._backdrop_getter is None:
            # 和通知窗口完全一致：受 backdrop_manager 管控（同一个 Qt 子进程）
            try:
                from front import get_backdrop_manager
                self._backdrop_getter = get_backdrop_manager
            except Exception:
                try:
                    from front.qt_backdrop import get_backdrop_manager
                    self._backdrop_getter = get_backdrop_manager
                except Exception:
                    self._backdrop_getter = False  # sentinel: permanently unavailable
        if self._backdrop_getter is False:
            return None
        try:
            cfg_path = self._resolve_default_config_path()
            if cfg_path:
                mgr = self._backdrop_getter(config_path=cfg_path)
            else:
                mgr = self._backdrop_getter()
        except Exception:
            return None
        if mgr is None:
            return None
        if not getattr(mgr, 'is_running', False):
            return None
        self._backdrop_ready = True
        return mgr

    def _flush_line(self, line_text: str):
        if not line_text:
            return
        if self._cb is not None:
            try:
                self._cb(line_text, self._source, self._tag)
            except Exception:
                pass
            return
        mgr = self._get_backend() if not self._backdrop_ready else self._get_backend()
        if mgr is None:
            return
        try:
            # QtBackdropManager.append_thinking_log() schema（和通知完全同一套管控）
            mgr.append_thinking_log(
                line_text,
                source=self._source,
                tag=self._tag,
                timestamp=True,
            )
        except Exception:
            # Never allow panel errors to break real console I/O
            pass

    def _append_and_maybe_flush(self, data: str):
        """Split data on newline boundaries, flush complete lines, buffer the
        trailing partial line for a later write().
        """
        if not data:
            return

        # Fast path: single write already ends with newline and buffer empty
        if (not self._buf) and len(data) <= LINE_MAX_BYTES and data.endswith('\n'):
            self._flush_line(data.rstrip('\r\n'))
            return

        pieces = data.replace('\r\n', '\n').split('\n')
        # If data ended with '\n' then last piece is empty — all complete lines
        # Otherwise last piece is a partial to keep buffering.

        # Re-join the first element with any pre-existing buffer prefix
        if self._buf:
            prefix = ''.join(self._buf)
            pieces[0] = prefix + pieces[0]
            self._buf.clear()
            self._buf_bytes = 0

        # Emit all pieces except the final one (which may be empty if ended with \n,
        # or a partial trailing segment)
        for i in range(len(pieces) - 1):
            self._flush_line(pieces[i])

        # Handle trailing piece: buffer if non-empty, else drop empty
        tail = pieces[-1]
        if tail:
            # Guard against huge single-line without newline (e.g. a JSON blob)
            # Flush what we've accumulated so far once we cross the cap.
            tail_len = len(tail)
            if self._buf_bytes + tail_len >= LINE_MAX_BYTES:
                # Flush current buffer contents as an oversize line, then reset
                full = ''.join(self._buf) + tail
                self._flush_line(full)
                self._buf.clear()
                self._buf_bytes = 0
            else:
                self._buf.append(tail)
                self._buf_bytes += tail_len

    # ── Public TextIO-style interface (subset used by print/stdlib) ─────
    @property
    def underlying(self):
        return self._underlying

    def close(self):
        with self._lock:
            if self._closed:
                return
            if self._buf:
                try:
                    self._flush_line(''.join(self._buf))
                finally:
                    self._buf.clear()
                    self._buf_bytes = 0
            self._closed = True
            # Do NOT close the underlying original stream

    def flush(self):
        with self._lock:
            try:
                self._underlying.flush()
            except Exception:
                pass
            # Never force a buffered partial line out on flush() — stdlib
            # print(..., flush=True) still expects no partial-line artifacts.

    def isatty(self):
        try:
            return self._underlying.isatty()
        except Exception:
            return False

    @property
    def encoding(self):
        return getattr(self._underlying, 'encoding', 'utf-8') or 'utf-8'

    @property
    def errors(self):
        return getattr(self._underlying, 'errors', 'strict')

    def writable(self):
        return True

    def write(self, s: str) -> int:
        if not s:
            return 0
        with self._lock:
            if self._closed:
                return 0
            # 1) Always write to real terminal FIRST (highest priority)
            try:
                n = self._underlying.write(s)
            except Exception:
                n = len(s)  # best effort
            # 2) Then copy to thinking log, line-buffered
            try:
                self._append_and_maybe_flush(s)
            except Exception:
                pass
            return n

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def __getattr__(self, name):
        # Proxy anything else (e.g. .buffer, .mode, .reconfigure) to underlying.
        # __getattr__ is only called if normal lookup failed (so not for the
        # methods we defined above).
        if name.startswith('_'):
            raise AttributeError(name)
        return getattr(self._underlying, name)


# ──────────────────────────── Public entry point ────────────────────────────

_orig_stdout: Optional[TextIO] = None
_orig_stderr: Optional[TextIO] = None
_installed = False


def install_console_tee(
    *,
    stdout_source: str = "TERM",
    stdout_tag: str = "console",
    stderr_source: str = "TERM-ERR",
    stderr_tag: str = "stderr",
    append_callback: Optional[Callable[[str, str, str], None]] = None,
    force_reinstall: bool = False,
) -> bool:
    """Replace sys.stdout / sys.stderr with ConsoleTee wrappers.

    Idempotent: subsequent calls after the first are no-ops unless
    `force_reinstall=True`.

    Returns True if wrappers were freshly installed.
    """
    global _orig_stdout, _orig_stderr, _installed
    if _installed and not force_reinstall:
        return False

    if _orig_stdout is None:
        _orig_stdout = sys.stdout
    if _orig_stderr is None:
        _orig_stderr = sys.stderr

    stdout_tee = ConsoleTee(
        _orig_stdout,
        source_label=stdout_source,
        tag_label=stdout_tag,
        append_callback=append_callback,
    )
    stderr_tee = ConsoleTee(
        _orig_stderr,
        source_label=stderr_source,
        tag_label=stderr_tag,
        append_callback=append_callback,
    )

    sys.stdout = stdout_tee  # type: ignore[assignment]
    sys.stderr = stderr_tee  # type: ignore[assignment]

    # Also patch io stdout/stderr references if present
    try:
        if getattr(sys, '__stdout__', None) is None:
            pass  # leave alone
    except Exception:
        pass

    _installed = True
    return True


def uninstall_console_tee() -> None:
    """Restore original sys.stdout / sys.stderr.  Useful for tests."""
    global _orig_stdout, _orig_stderr, _installed
    if _orig_stdout is not None:
        sys.stdout = _orig_stdout
    if _orig_stderr is not None:
        sys.stderr = _orig_stderr
    _installed = False
