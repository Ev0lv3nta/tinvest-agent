#!/usr/bin/env bash
# Раскатка стенда. Запускать от root на сервере.
set -euo pipefail

REPO=/opt/tinvest-agent
WORK=/home/agent/work
STATE=/home/agent/state

id agent >/dev/null 2>&1 || useradd -m -s /bin/bash agent

# Код шлюза лежит отдельно от рабочей директории агента и принадлежит root:
# агент правит свои заметки и скиллы, но не ограничители и не журнал.
mkdir -p "$REPO" "$WORK" "$STATE"
chown -R root:root "$REPO"
chmod -R a+rX "$REPO"
chown -R agent:agent "$WORK" "$STATE"

# Инструкции и скиллы — в рабочую директорию, там агент может их править.
install -o agent -g agent -m 644 "$REPO/AGENTS.md" "$WORK/AGENTS.md"
mkdir -p "$WORK/.agents/skills" "$WORK/notes/reports"
cp -r "$REPO/.agents/skills/." "$WORK/.agents/skills/"
chown -R agent:agent "$WORK"

# Git в рабочей директории: правки агента в собственных инструкциях должны
# быть видны в истории.
if [ ! -d "$WORK/.git" ]; then
    su agent -c "cd $WORK && git init -q -b main \
        && git config user.name 'trading-agent' \
        && git config user.email 'agent@localhost' \
        && git add -A && git commit -q -m 'Исходные инструкции и скиллы'"
fi

install -o agent -g agent -m 600 "$REPO/deploy/codex-config.toml" /home/agent/.codex/config.toml
install -m 644 "$REPO/deploy/systemd/tinvest-agent.service" /etc/systemd/system/
systemctl daemon-reload

echo "Готово. Запуск: systemctl start tinvest-agent"
