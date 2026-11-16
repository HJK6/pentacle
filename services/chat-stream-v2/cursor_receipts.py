"""Receipt grammar shared by the Cursor REPL and submission predicates."""
import re

_RECEIPT = re.compile(r'^» #(\d+) (.*)$', re.MULTILINE)


def receipt_key(message: str) -> str:
    first = next((line for line in message.strip().splitlines() if line.strip()), '')
    return ' '.join(first.split())[:120].rstrip()


def receipts(pane: str) -> list[tuple[int, str]]:
    return [(int(n), key.rstrip()) for n, key in _RECEIPT.findall(pane)]


def high_water(pane: str) -> int:
    return max((n for n, _ in receipts(pane)), default=0)
