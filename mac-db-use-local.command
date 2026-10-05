#!/bin/bash
# Переключает базу с общей (Turso) на локальный файл и перезапускает бота.
# Нужно, если бесплатный лимит Turso кончился. Двойной клик по файлу.
# Пробует сперва скопировать историю из Turso в локальный файл; если Turso
# недоступна даже для чтения, просто переключается на пустую базу.
set -u
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.docker/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

if ! docker info >/dev/null 2>&1; then
  echo "Docker не запущен. Открой Docker Desktop и попробуй снова."
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

if docker compose run --rm bot db-use-local; then
  echo
  echo "Перезапускаю бота..."
  docker compose restart bot
  echo
  echo "Готово. Проверить: в боте /menu -> Статус."
else
  echo
  echo "Не получилось — причина выше."
fi

echo
read -r -p "Нажми Enter, чтобы закрыть..." _
