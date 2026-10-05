#!/bin/bash
# Останавливает бота. Найденное и настройки сохраняются.
set -u
cd "$(dirname "$0")" || exit 1

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker не установлен — останавливать нечего."
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 0
fi

echo "Останавливаю бота..."
docker compose down --remove-orphans
# Остаток от ранних версий, где контейнеру задавалось жёсткое имя.
docker rm -f wlparser >/dev/null 2>&1

echo
echo "Остановлен. База находок и настройки на месте — запустить снова"
echo "можно файлом mac-start.command"
echo
read -r -p "Нажми Enter, чтобы закрыть..." _
