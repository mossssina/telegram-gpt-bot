#!/usr/bin/env bash

set -Eeuo pipefail

###########################################
# CONFIG
###########################################

PROJECT_DIR="/Users/anastasiiamosina/claude/telegram-gpt-bot"

REMOTE="studiosuccess-server"

REMOTE_DIR="/home/yc-user/telegram-gpt-bot"

SERVICE="studiosuccess-bot.service"

MEMORY_SERVICE="studiosuccess-memory-update.service"

MEMORY_TIMER="studiosuccess-memory-update.timer"

###########################################
# COLORS
###########################################

GREEN="\033[0;32m"
RED="\033[0;31m"
BLUE="\033[1;34m"
NC="\033[0m"

###########################################

echo ""

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Studiosuccess Production Deploy${NC}"
echo -e "${BLUE}========================================${NC}"

echo ""

cd "$PROJECT_DIR"

###########################################
# PYTHON CHECK
###########################################

echo -e "${GREEN}1. Проверка Python${NC}"

python3 -m py_compile bot.py

if compgen -G "services/*.py" > /dev/null; then
    python3 -m py_compile services/*.py
fi

if compgen -G "scripts/*.py" > /dev/null; then
    python3 -m py_compile scripts/*.py
fi

echo "OK"

###########################################
# TESTS
###########################################

if [ -d tests ] && command -v pytest >/dev/null 2>&1; then

echo ""

echo -e "${GREEN}1b. Тесты (pytest)${NC}"

python3 -m pytest tests/ -q

echo "OK"

fi

###########################################
# GIT STATUS
###########################################

echo ""

echo -e "${GREEN}2. Проверка Git${NC}"

git status --short || true

###########################################
# RSYNC
###########################################

echo ""

echo -e "${GREEN}3. Копирование файлов${NC}"

rsync -az \
--exclude=".git" \
--exclude=".venv" \
--exclude="venv" \
--exclude="__pycache__" \
--exclude=".DS_Store" \
--exclude="*.pyc" \
--exclude="logs/" \
--exclude="client_projects/" \
./ \
${REMOTE}:${REMOTE_DIR}/

echo "OK"

###########################################
# REQUIREMENTS
###########################################

echo ""

echo -e "${GREEN}4. Проверка зависимостей${NC}"

if git diff --name-only HEAD~1 HEAD 2>/dev/null | grep -q "requirements.txt"; then

echo "requirements.txt изменился"

ssh ${REMOTE} "
cd ${REMOTE_DIR}
.venv/bin/pip install -r requirements.txt
"

else

echo "requirements.txt не изменился"

fi

###########################################
# SYSTEMD
###########################################

echo ""

echo -e "${GREEN}5. Обновление systemd${NC}"

ssh ${REMOTE} "

cd ${REMOTE_DIR}

if [ -f deployment/studiosuccess-bot.service ]; then

sudo cp deployment/studiosuccess-bot.service /etc/systemd/system/

fi

if [ -f deployment/studiosuccess-memory-update.service ]; then

sudo cp deployment/studiosuccess-memory-update.service /etc/systemd/system/

fi

if [ -f deployment/studiosuccess-memory-update.timer ]; then

sudo cp deployment/studiosuccess-memory-update.timer /etc/systemd/system/

fi

sudo systemctl daemon-reload

"

echo "OK"

###########################################
# RESTART
###########################################

echo ""

echo -e "${GREEN}6. Перезапуск сервисов${NC}"

ssh ${REMOTE} "

sudo systemctl restart ${SERVICE}

if systemctl list-unit-files | grep -q ${MEMORY_TIMER}; then

sudo systemctl restart ${MEMORY_TIMER}

fi

"

echo "OK"

###########################################
# STATUS
###########################################

echo ""

echo -e "${GREEN}7. Проверка статуса${NC}"

ssh ${REMOTE} "

sudo systemctl --no-pager --full status ${SERVICE}

echo

if systemctl list-unit-files | grep -q ${MEMORY_TIMER}; then

sudo systemctl --no-pager --full status ${MEMORY_TIMER}

fi

"

###########################################
# MEMORY TEST
###########################################

echo ""

echo -e "${GREEN}8. Проверка Memory Engine${NC}"

ssh ${REMOTE} "

cd ${REMOTE_DIR}

if [ -f scripts/daily_memory_update.py ]; then

.venv/bin/python scripts/daily_memory_update.py --dry-run || true

fi

"

###########################################
# LOGS
###########################################

echo ""

echo -e "${GREEN}9. Последние логи${NC}"

ssh ${REMOTE} "

sudo journalctl -u ${SERVICE} -n 30 --no-pager

"

###########################################
# FINISH
###########################################

echo ""

echo -e "${GREEN}========================================${NC}"

echo -e "${GREEN}DEPLOY SUCCESSFUL${NC}"

echo -e "${GREEN}========================================${NC}"

echo ""

echo "Бот успешно обновлен."

echo "Memory Engine проверен."

echo "Сервисы работают."

echo ""
