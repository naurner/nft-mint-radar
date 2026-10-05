#!/bin/bash
# Присылает тестовый черновик поста в Telegram, минуя дневной лимит
# предложений — чтобы проверить, реально ли публикация в X работает,
# когда обычных черновиков сегодня уже не будет. Двойной клик по файлу.
set -u
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.docker/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

if ! docker info >/dev/null 2>&1; then
  echo "Docker не запущен. Открой Docker Desktop и попробуй снова."
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

docker compose run --rm bot poster-test-draft "$@"

echo
read -r -p "Нажми Enter, чтобы закрыть..." _
