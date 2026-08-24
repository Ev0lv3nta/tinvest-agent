#!/usr/bin/env python3
"""Отправить сообщение агенту из командной строки.

Кладёт сообщение в очередь, супервизор доставит его в живую сессию в
течение нескольких секунд. Расписание агента не затрагивается.

    python3 deploy/say.py "текст сообщения"
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import journal


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    text = " ".join(sys.argv[1:])
    source = "service" if text.startswith("!") else "operator"
    message_id = journal.enqueue_message(text.lstrip("!").strip(), source)
    print(f"поставлено в очередь #{message_id} ({source})")


if __name__ == "__main__":
    main()
