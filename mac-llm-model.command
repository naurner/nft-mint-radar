#!/bin/bash
# Задаёт модель Ollama для ИИ (тексты постов + отсев спама в алертах) и
# перезапускает бота. Двойной клик спросит имя модели; из терминала можно
# сразу с аргументом: ./mac-llm-model.command qwen2.5:3b
set -u
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.docker/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

MODEL="${1:-}"
if [ -z "$MODEL" ]; then
  echo "Модели, скачанные в Ollama, можно посмотреть командой: ollama list"
  read -r -p "Имя модели (например qwen2.5:3b): " MODEL
fi

if [ -z "$MODEL" ]; then
  echo "Имя модели не указано — ничего не делаю."
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker не запущен. Открой Docker Desktop и попробуй снова."
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

if docker compose run --rm bot llm-model "$MODEL"; then
  echo
  echo "Перезапускаю бота..."
  docker compose restart bot
  echo
  echo "Готово. Проверить: в боте /menu -> Статус, строка «локальный ИИ»."
else
  echo
  echo "Не получилось — причина выше."
fi

echo
read -r -p "Нажми Enter, чтобы закрыть..." _
