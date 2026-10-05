#!/bin/bash
# Подключение аккаунта X и проверка, что чтение работает.
set -u
cd "$(dirname "$0")" || exit 1

if [ -f .env ] && [ ! -f config/.env ]; then
  cp .env config/.env
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker не запущен. Открой Docker Desktop и попробуй снова."
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

docker compose run --rm bot twitter-add || exit 1
echo
docker compose run --rm bot twitter-test

echo
echo "Если всё работает — открой config/config.yaml, поставь"
echo "twitter.enabled: true и перезапусти бота через mac-start.command"
echo
read -r -p "Нажми Enter, чтобы закрыть..." _
