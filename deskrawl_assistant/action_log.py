"""Local operation diagnostics, independent of game inputs."""
from datetime import datetime, timezone
import json
import threading
from .paths import runtime_dir
_guard = threading.Lock()


def record(event, **details):
    row = {'time': datetime.now(timezone.utc).isoformat(), 'event': event, **details}
    try:
        with _guard:
            path = runtime_dir() / 'assistant-actions.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    except OSError:
        pass
