#!/bin/bash
# Проверяет GitHub на обновления и пересобирает контейнер, если код изменился.
# Запускается сам через launchd — см. mac-autopull-install.command. Плюс
# WatchPaths в плисте запускает этот же скрипт немедленно, как только
# появляется data/update_requested.json — это кнопка «Обновить с GitHub»
# в самом боте.
set -u

# launchd запускает задачи с урезанным PATH (без Homebrew/Docker Desktop),
# в отличие от интерактивного терминала, который подхватывает его из
# .zshrc/.zprofile. Без этого git тут же обновляет код, а следующая строка
# — "docker compose up -d --build" — молча падает с "docker: command not
# found", и лог всё равно пишет "готово", потому что код возврата docker
# никто не проверял. "~/.docker/bin" — то место, где реально стоит docker
# в этой установке (проверено `which docker`); остальные пути — на случай,
# если Docker когда-нибудь переставят иначе.
export PATH="$HOME/.docker/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

cd "$(dirname "$0")" || exit 1

LOG="autopull.log"
# Лог не должен расти вечно — держим последние ~2000 строк.
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 2000 ]; then
  tail -n 1000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# Запрос от кнопки «Обновить с GitHub» в боте — бот пишет этот файл в свой
# /app/data, который смонтирован сюда же как ./data (см. docker-compose.yml).
# Переименование в inflight, а не удаление, — чтобы бот, если перезапустится
# посреди обновления, всё равно знал, куда прислать результат.
REQUEST="data/update_requested.json"
INFLIGHT="data/update_inflight.json"
RESULT="data/update_result.txt"

FROM_BUTTON=0
if [ -f "$REQUEST" ]; then
  mv "$REQUEST" "$INFLIGHT"
  FROM_BUTTON=1
  echo "$(date '+%Y-%m-%d %H:%M:%S') запрос на обновление из бота (кнопка)" >> "$LOG"
else
  echo "$(date '+%Y-%m-%d %H:%M:%S') проверяю обновления..." >> "$LOG"
fi

# Результат нужен только тому, кто нажал кнопку — плановая проверка его не
# читает, писать его для неё смысла нет. Пишется во временный файл и
# переименовывается, чтобы бот не увидел файл в процессе записи.
write_result() {
  [ "$FROM_BUTTON" = 1 ] || return 0
  mkdir -p data
  printf 'status=%s\nfrom=%s\nto=%s\ndetail=%s\n' "$1" "$2" "$3" "$4" > "$RESULT.tmp" \
    && mv "$RESULT.tmp" "$RESULT"
}

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "  это не git-репозиторий — автообновление недоступно" >> "$LOG"
  write_result "error" "" "" "это не git-репозиторий"
  exit 1
fi

if ! git fetch origin master --quiet 2>>"$LOG"; then
  echo "  fetch не удался (нет сети или доступа к репозиторию)" >> "$LOG"
  write_result "error" "" "" "git fetch не удался — нет сети или доступа"
  exit 0
fi

LOCAL=$(git rev-parse --short HEAD)
REMOTE=$(git rev-parse --short origin/master)

if [ "$LOCAL" = "$REMOTE" ]; then
  if [ "$FROM_BUTTON" = 1 ]; then
    echo "  по кнопке, но и так уже на последнем коммите ($LOCAL)" >> "$LOG"
    write_result "unchanged" "$LOCAL" "$REMOTE" "уже на последнем коммите"
  else
    echo "  без изменений ($LOCAL)" >> "$LOG"
  fi
  exit 0
fi

echo "  обновление $LOCAL -> $REMOTE" >> "$LOG"
# git reset --hard, а не git pull: chmod +x на .sh/.command файлах меняет
# их режим доступа, и git иногда считает это локальным изменением, из-за
# которого pull отказывается делать fast-forward. .env и data/ в git не
# отслеживаются, поэтому reset --hard их не тронет.
if ! git reset --hard origin/master --quiet >> "$LOG" 2>&1; then
  echo "  git reset не удался, оставляю как было" >> "$LOG"
  write_result "error" "$LOCAL" "$REMOTE" "git reset --hard не удался"
  exit 1
fi

# Секреты живут в config/.env — при первом переносе их туда ещё нет.
if [ -f .env ] && [ ! -f config/.env ]; then
  cp .env config/.env
fi

echo "  пересобираю контейнер..." >> "$LOG"
if ! command -v docker >/dev/null 2>&1; then
  echo "  ОШИБКА: docker не найден в PATH ($PATH)" >> "$LOG"
  write_result "error" "$LOCAL" "$REMOTE" "docker не найден в PATH"
  exit 1
fi

# GIT_COMMIT попадает в образ (см. Dockerfile/docker-compose.yml) и виден
# в /menu → Статус — самый простой способ убедиться, что Мак реально
# обновился, не заходя по SSH.
if GIT_COMMIT="$REMOTE" docker compose up -d --build >> "$LOG" 2>&1; then
  echo "  готово" >> "$LOG"
  write_result "ok" "$LOCAL" "$REMOTE" "обновлено и пересобрано"
else
  echo "  ОШИБКА: пересборка не удалась (см. вывод docker выше)" >> "$LOG"
  write_result "error" "$LOCAL" "$REMOTE" "пересборка не удалась"
fi
