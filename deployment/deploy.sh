#!/usr/bin/env bash

set -Eeuo pipefail

###########################################
# CONFIG
###########################################

PROJECT_DIR="/Users/anastasiiamosina/tg bots/telegram-gpt-bot_ss"

REMOTE="studiosuccess-server"

REMOTE_DIR="/home/telegram-gpt-bot"

SERVICE="studiosuccess-bot.service"

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
# GIT PUSH
###########################################

echo ""

echo -e "${GREEN}2. Push на GitHub${NC}"

git push

echo "OK"

###########################################
# GIT PULL ON SERVER
###########################################

echo ""

echo -e "${GREEN}3. Pull на сервере${NC}"

ssh ${REMOTE} "cd ${REMOTE_DIR} && git pull"

echo "OK"

###########################################
# REQUIREMENTS
###########################################

echo ""

echo -e "${GREEN}4. Проверка зависимостей${NC}"

if git diff --name-only HEAD~1 HEAD 2>/dev/null | grep -q "requirements.txt"; then

echo "requirements.txt изменился — устанавливаю"

ssh ${REMOTE} "cd ${REMOTE_DIR} && .venv/bin/pip install -r requirements.txt"

else

echo "requirements.txt не изменился"

fi

###########################################
# RESTART
###########################################

echo ""

echo -e "${GREEN}5. Перезапуск сервиса${NC}"

ssh ${REMOTE} "systemctl restart ${SERVICE}"

echo "OK"

###########################################
# STATUS
###########################################

echo ""

echo -e "${GREEN}6. Проверка статуса${NC}"

ssh ${REMOTE} "systemctl --no-pager --full status ${SERVICE}"

###########################################
# LOGS
###########################################

echo ""

echo -e "${GREEN}7. Последние логи${NC}"

ssh ${REMOTE} "journalctl -u ${SERVICE} -n 30 --no-pager"

###########################################
# FINISH
###########################################

echo ""

echo -e "${GREEN}========================================${NC}"

echo -e "${GREEN}DEPLOY SUCCESSFUL${NC}"

echo -e "${GREEN}========================================${NC}"

echo ""
