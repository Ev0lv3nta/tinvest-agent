#!/usr/bin/env bash
# Раскатка стенда. Запускать от root на сервере. Идемпотентна: повторный
# прогон не затирает то, что принадлежит агенту.
set -euo pipefail

REPO=/opt/tinvest-agent
WORK=/home/agent/work
STATE=/home/agent/state
DATA=/home/agent/data

id agent >/dev/null 2>&1 || useradd -m -s /bin/bash agent

# Код шлюза лежит отдельно от рабочей директории агента и принадлежит root:
# агент правит свои заметки и скиллы, но не ограничители.
mkdir -p "$REPO" "$WORK" "$STATE" "$DATA" /home/agent/.codex
chown -R root:root "$REPO"
chmod -R a+rX "$REPO"
chown -R agent:agent "$WORK" "$STATE" "$DATA" /home/agent/.codex

# Инструкции и скиллы кладутся только при первой установке: агенту явно
# разрешено их править, и повторная раскатка не должна молча откатывать
# его работу.
mkdir -p "$WORK/.agents/skills" "$WORK/notes/reports"
[ -f "$WORK/AGENTS.md" ] || install -o agent -g agent -m 644 "$REPO/AGENTS.md" "$WORK/AGENTS.md"
for skill in "$REPO"/.agents/skills/*/; do
    name=$(basename "$skill")
    [ -d "$WORK/.agents/skills/$name" ] || cp -r "$skill" "$WORK/.agents/skills/$name"
done
chown -R agent:agent "$WORK"

# Канонические копии — отдельно и только на чтение: по ним видно, что
# именно агент у себя изменил.
rm -rf "$WORK/.agents/reference"
mkdir -p "$WORK/.agents/reference"
cp "$REPO/AGENTS.md" "$WORK/.agents/reference/AGENTS.md"
cp -r "$REPO/.agents/skills" "$WORK/.agents/reference/skills"
chown -R root:root "$WORK/.agents/reference"
chmod -R a=rX,u=rwX "$WORK/.agents/reference"

# Git в рабочей директории: правки агента в собственных инструкциях должны
# быть видны в истории.
if [ ! -d "$WORK/.git" ]; then
    su agent -c "cd $WORK && git init -q -b main \
        && git config user.name 'trading-agent' \
        && git config user.email 'agent@localhost' \
        && git add -A && git commit -q -m 'Исходные инструкции и скиллы'"
fi

install -D -o agent -g agent -m 600 "$REPO/deploy/codex-config.toml" \
    /home/agent/.codex/config.toml

for unit in tinvest-agent tinvest-panel omniroute-tunnel tinvest-backup; do
    install -D -m 644 "$REPO/deploy/systemd/$unit.service" \
        "/etc/systemd/system/$unit.service"
done
install -D -m 644 "$REPO/deploy/systemd/tinvest-backup.timer" \
    /etc/systemd/system/tinvest-backup.timer
systemctl daemon-reload
systemctl enable omniroute-tunnel tinvest-panel tinvest-agent >/dev/null
systemctl enable --now tinvest-backup.timer >/dev/null

echo "Проверка перед запуском:"
command -v codex >/dev/null && codex --version || echo "  codex не найден в PATH"
python3 -c "import sys; sys.path.insert(0,'$REPO'); from gateway import server; \
    print(f'  MCP-шлюз импортируется, инструментов: {len(server.TOOLS)}')"
python3 -m unittest discover -t "$REPO" -s "$REPO/tests" -q 2>&1 | tail -3

echo "Готово. Запуск: systemctl start omniroute-tunnel tinvest-panel tinvest-agent"
echo "Снимки журнала: /var/backups/tinvest-agent (root-only, раз в час)"
