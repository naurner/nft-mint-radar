#!/bin/bash
# Запуск бота на Mac через Docker. Двойной клик по файлу.
set -u
cd "$(dirname "$0")" || exit 1

echo "WL Parser"
echo

if ! command -v docker >/dev/null 2>&1; then
  cat <<'EOF'
  Docker не установлен.

  1. Скачай Docker Desktop: https://www.docker.com/products/docker-desktop/
     Выбери версию для Apple Silicon (Mac с чипом M1/M2/M3).
  2. Установи и запусти его — в верхней строке появится значок кита.
  3. Запусти этот файл ещё раз.

EOF
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "  Docker установлен, но не запущен."
  echo "  Открой Docker Desktop, дождись значка кита в верхней строке,"
  echo "  затем запусти этот файл снова."
  echo
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

mkdir -p config data

# Внутри контейнера примонтированы только config/ и data/, поэтому секреты
# должны лежать в config/.env. На Windows бот читает .env из корня проекта —
# переносим, чтобы перенос папки между машинами не требовал ручных действий.
if [ -f .env ] && [ ! -f config/.env ]; then
  cp .env config/.env
  echo "Секреты перенесены в config/.env — там их видит контейнер."
  echo
fi

# Останавливаем прошлый запуск. Без этого второй экземпляр конфликтует
# с первым: Telegram отдаёт сообщения только одному боту с этим токеном.
docker compose down --remove-orphans >/dev/null 2>&1
# Остаток от ранних версий, где контейнеру задавалось жёсткое имя.
docker rm -f wlparser >/dev/null 2>&1

# Первый запуск: собираем образ и проходим настройку в интерактивном режиме.
if [ ! -f config/config.yaml ]; then
  echo "Первый запуск: собираю контейнер, это займёт пару минут..."
  echo
  if ! docker compose build; then
    echo
    echo "  Сборка не удалась. Проверь интернет и попробуй снова."
    read -r -p "Нажми Enter, чтобы закрыть..." _
    exit 1
  fi
  echo
  docker compose run --rm bot setup || exit 1
  echo
fi

echo "Запускаю бота. Он продолжит работать в фоне, даже если закрыть это окно."
echo "Остановить: mac-stop.command"
echo

# Виден потом в /menu → Статус в самом боте — так можно свериться, какой
# код реально запущен, не заходя по SSH. "unknown", если это не git-клон.
export GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

if ! docker compose up -d --build; then
  echo
  echo "  Запустить не удалось. Покажи вывод выше — по нему видно причину."
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

echo
echo "Работает. Показываю логи — закрыть окно можно в любой момент,"
echo "бот от этого не остановится. Выйти из логов: Ctrl+C"
echo
docker compose logs -f
