#!/bin/bash
# Включает автопост в X и перезапускает бота. Двойной клик по файлу.
# Черновики приходят в Telegram, публикует бот только после твоего нажатия.
set -u
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.docker/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

if ! docker info >/dev/null 2>&1; then
  echo "Docker не запущен. Открой Docker Desktop и попробуй снова."
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

if docker compose run --rm bot poster-enable "$@"; then
  echo
  echo "Перезапускаю бота..."
  docker compose restart bot
  echo
  echo "Готово. Проверить: в боте /menu -> Статус, строка «автопост в X»."
else
  echo
  echo "Не включилось — причина выше."
fi

echo
read -r -p "Нажми Enter, чтобы закрыть..." _
