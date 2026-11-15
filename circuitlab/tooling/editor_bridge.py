"""Receive explicit reload requests from the IDE over its private stdin pipe."""

import json
import codecs
import os
from pathlib import Path
from threading import Thread

from PySide6.QtCore import QObject, Signal


class EditorBridge(QObject):
    requested = Signal(str)
    refreshed = Signal(str)

    def start(self, stream):
        Thread(target=self.read_requests, args=(stream,), daemon=True).start()

    def read_requests(self, stream):
        # Never touch widgets here: the receiver runs on the Qt event thread.
        try:
            for line in request_lines(stream):
                try:
                    request = json.loads(line)
                    file = request.get("file") if isinstance(request, dict) else None
                    if isinstance(file, str) and Path(file).is_absolute():
                        signal = self.refreshed if request.get("activate") is False else self.requested
                        signal.emit(file)
                except (ValueError, RuntimeError):
                    continue
        except (OSError, UnicodeError):
            pass


def request_lines(stream):
    """Avoid holding Python's buffered stdin lock while a daemon waits at exit."""
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError):
        yield from stream
        return
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    while True:
        chunk = os.read(descriptor, 4096)
        if not chunk:
            return
        pending += decoder.decode(chunk)
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            yield line
