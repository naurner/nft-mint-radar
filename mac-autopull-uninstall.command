#!/bin/bash
# Выключает автообновление. Сам бот и уже собранный контейнер не трогает.
# com.wlparser.updatewatch.plist в списке ради тех, кто успел поставить
# более раннюю версию (отдельная задача на кнопку) — если её нет, ничего
# страшного, цикл просто её не найдёт.
set -u

FOUND=0
for PLIST_NAME in com.wlparser.autopull.plist com.wlparser.updatewatch.plist; do
  PLIST="$HOME/Library/LaunchAgents/$PLIST_NAME"
  if [ -f "$PLIST" ]; then
    launchctl unload "$PLIST" >/dev/null 2>&1
    rm -f "$PLIST"
    FOUND=1
  fi
done

if [ "$FOUND" -eq 0 ]; then
  echo "Автообновление не было включено — выключать нечего."
else
  echo "Автообновление выключено."
fi

echo
read -r -p "Нажми Enter, чтобы закрыть..." _
