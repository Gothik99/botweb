#!/bin/bash
set -e

# Проверка запуска из /root/bot/
if [[ "$PWD" != "/root/bot" ]]; then
  echo "Скрипт должен запускаться из /root/bot/! Текущая папка: $PWD"
  exit 1
fi

# Установка Python и pip
apt update
apt install -y python3 python3-venv python3-pip

# Создание venv для бота
if [ ! -d "venv" ]; then
  python3 -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

deactivate

# Создание venv для web_admin
if [ ! -d "web_admin/venv" ]; then
  python3 -m venv web_admin/venv
fi
source web_admin/venv/bin/activate
pip install --upgrade pip
pip install -r web_admin/requirements.txt || pip install -r requirements.txt

deactivate

# Переименование ex.env в .env
if [ -f "ex.env" ] && [ ! -f ".env" ]; then
  echo "Переименовываю ex.env в .env..."
  mv ex.env .env
  echo "Файл .env создан!"
elif [ -f ".env" ]; then
  echo "Файл .env уже существует, пропускаю переименование."
else
  echo "Файл ex.env не найден, создаю пустой .env..."
  touch .env
fi

echo "\nУстановка завершена!"

echo "\nНастраиваю автозапуск сервисов..."
cp vpn-bot.service /etc/systemd/system/
cp vpn-webadmin.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vpn-bot.service vpn-webadmin.service

echo "\nБот и веб-админка запущены и добавлены в автозагрузку!" 