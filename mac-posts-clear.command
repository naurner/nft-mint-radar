#!/bin/bash
# Списывает все висящие черновики постов в X (ничего не публикует — просто
# чистит очередь предложений). Двойной клик по файлу.
set -u
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.docker/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

if ! docker info >/dev/null 2>&1; then
  echo "Docker не запущен. Открой Docker Desktop и попробуй снова."
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

docker compose run --rm bot posts-clear-pending

echo
read -r -p "Нажми Enter, чтобы закрыть..." _
