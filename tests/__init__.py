"""Тесты ядра безопасности.

Окружение выставляется до импорта gateway: конфигурация читает пути при
импорте, поэтому здесь оно должно оказаться раньше всего остального.
"""

import os
import tempfile
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="tinvest-tests-"))
os.environ["AGENT_DB"] = str(_ROOT / "agent.db")
os.environ["AGENT_DATA_DIR"] = str(_ROOT / "data")
os.environ["AGENT_WORKDIR"] = str(_ROOT / "work")
os.environ["RESEARCH_DB"] = str(_ROOT / "research.db")
(_ROOT / "work" / "notes" / "reports").mkdir(parents=True, exist_ok=True)

TEST_ROOT = _ROOT
