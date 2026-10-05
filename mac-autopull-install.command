#!/bin/bash
# Включает автообновление: раз в 5 минут Мак сам проверяет GitHub
# и пересобирает контейнер, если код изменился. Плюс мгновенный отклик
# на кнопку "Обновить с GitHub" в самом боте — launchd сам следит за
# файлом-флагом (WatchPaths в плисте) и запускает проверку сразу же,
# без ожидания планового пятиминутного цикла.
set -u
cd "$(dirname "$0")" || exit 1

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Это не git-репозиторий."
  echo "Автообновление работает только на папке, склонированной через git clone —"
  echo "той, что распакована из .zip, оно работать не будет."
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "У репозитория не задан адрес origin — автообновлять неоткуда."
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi

DIR="$(pwd)"
PLIST_NAME="com.wlparser.autopull.plist"
DEST="$HOME/Library/LaunchAgents/$PLIST_NAME"

mkdir -p "$HOME/Library/LaunchAgents" data
sed "s|__DIR__|$DIR|g" "$PLIST_NAME" > "$DEST"

launchctl unload "$DEST" >/dev/null 2>&1
launchctl load "$DEST"

echo "Автообновление включено."
echo "Проверка раз в 5 минут: $(git remote get-url origin)"
echo "Плюс мгновенный отклик на кнопку «Обновить с GitHub» в самом боте — launchd следит"
echo "за файлом-флагом и запускает проверку сразу же, не дожидаясь планового цикла."
echo "Лог: $DIR/autopull.log"
echo
echo "Выключить: mac-autopull-uninstall.command"
echo
read -r -p "Нажми Enter, чтобы закрыть..." _
