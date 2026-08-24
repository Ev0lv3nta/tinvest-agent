#!/usr/bin/env bash
# Раскатка кода на сервер. Запускать с рабочей машины из корня репозитория.
#
# Порядок такой, потому что однажды было иначе: архив собрался пустым, а на
# сервере к тому моменту уже снесли каталоги кода. Ничего не удаляется, пока
# новая версия не проверена на месте.
#
#   deploy/push.sh root@сервер
set -euo pipefail

TARGET="${1:?укажи цель: root@адрес}"
SSH="${SSH_CMD:-ssh}"
SCP="${SCP_CMD:-scp}"
REMOTE=/opt/tinvest-agent
STAGE=/opt/tinvest-agent.new
ARCHIVE=$(mktemp -t tinvest-deploy.XXXXXX).tar.gz

cleanup() { rm -f "$ARCHIVE"; }
trap cleanup EXIT

echo "1. Собираю архив из HEAD"
git -C "$(git rev-parse --show-toplevel)" archive --format=tar.gz -o "$ARCHIVE" HEAD
FILES=$(tar tzf "$ARCHIVE" | wc -l | tr -d ' ')
[ "$FILES" -gt 20 ] || { echo "в архиве всего $FILES файлов — что-то не так"; exit 1; }
SUM=$(shasum -a 256 "$ARCHIVE" | cut -d' ' -f1)
echo "   файлов: $FILES, sha256: ${SUM:0:16}…"

echo "2. Загружаю"
$SCP -q "$ARCHIVE" "$TARGET:/tmp/tinvest-deploy.tar.gz"

echo "3. Проверяю и раскатываю на месте"
$SSH "$TARGET" REMOTE="$REMOTE" STAGE="$STAGE" SUM="$SUM" bash -s <<'ONREMOTE'
set -euo pipefail

got=$(sha256sum /tmp/tinvest-deploy.tar.gz | cut -d' ' -f1)
[ "$got" = "$SUM" ] || { echo "   контрольная сумма не сошлась"; exit 1; }
echo "   сумма совпала"

# Распаковываем рядом и проверяем, не трогая работающую версию.
rm -rf "$STAGE"; mkdir -p "$STAGE"
tar xzf /tmp/tinvest-deploy.tar.gz -C "$STAGE"
cd "$STAGE"
python3 -c "import sys; sys.path.insert(0,'.'); from gateway import server; \
    print(f'   шлюз импортируется, инструментов: {len(server.TOOLS)}')"
python3 -m unittest discover -t . -s tests -q 2>&1 | tail -2

echo "   останавливаю супервизор"
systemctl stop tinvest-agent

PREV="$REMOTE.prev"
rm -rf "$PREV"
mv "$REMOTE" "$PREV"
mv "$STAGE" "$REMOTE"
chown -R root:root "$REMOTE"; chmod -R a+rX "$REMOTE"

if systemctl start tinvest-agent && sleep 5 && systemctl is-active --quiet tinvest-agent; then
    systemctl restart tinvest-panel
    echo "   запущено, предыдущая версия в $PREV"
else
    echo "   НЕ ЗАПУСТИЛОСЬ — откатываюсь"
    rm -rf "$REMOTE"; mv "$PREV" "$REMOTE"
    systemctl start tinvest-agent
    exit 1
fi
ONREMOTE

echo "4. Проверка снаружи"
$SSH "$TARGET" 'systemctl is-active omniroute-tunnel tinvest-panel tinvest-agent'
