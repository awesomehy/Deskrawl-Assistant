"""Bounded local diagnostics; durable activity and trade receipts live elsewhere."""
from datetime import datetime, timezone
import json
import threading
from .paths import runtime_dir
_guard = threading.Lock()
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3


def _rotate(path):
    oldest = path.with_name(path.name + '.' + str(BACKUP_COUNT))
    oldest.unlink(missing_ok=True)
    for index in range(BACKUP_COUNT - 1, 0, -1):
        source = path.with_name(path.name + '.' + str(index))
        if source.exists():
            source.replace(path.with_name(path.name + '.' + str(index + 1)))
    # An oversized pre-upgrade log is retained as a complete first backup,
    # then ages out through the same rotation policy; never truncate it here.
    path.replace(path.with_name(path.name + '.1'))


def record(event, **details):
    row = {'time': datetime.now(timezone.utc).isoformat(), 'event': event, **details}
    raw = (json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8')
    if len(raw) > MAX_BYTES:
        # A single diagnostic snapshot must not defeat the size limit. Keep a
        # visible event and its original size; this is never a business receipt.
        row = {'time': datetime.now(timezone.utc).isoformat(),
               'event': event[:200] if isinstance(event, str) else 'oversized_diagnostic',
               'details_omitted': True, 'original_bytes': len(raw)}
        raw = (json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8')
    try:
        with _guard:
            path = runtime_dir() / 'assistant-actions.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size + len(raw) > MAX_BYTES:
                _rotate(path)
            with path.open('ab') as stream:
                stream.write(raw)
        return True
    except OSError:
        return False
