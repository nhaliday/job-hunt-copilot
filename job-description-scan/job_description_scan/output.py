import json
from pathlib import Path
from types import TracebackType
from typing import IO


class JsonlWriter:
    """Streaming JSONL writer.

    Opens on enter, flushes after each `write()` so a running file size or
    `tail -f` serves as live progress and an aborted run still leaves usable
    partial output. Closes on exit.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.count = 0
        self._f: IO[str] | None = None

    def __enter__(self) -> "JsonlWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("w", encoding="utf-8")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None

    def write(self, row: dict) -> None:
        assert self._f is not None, "JsonlWriter not entered"
        self._f.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._f.flush()
        self.count += 1
