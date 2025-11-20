# 🖥️ Настройка на сервере

## ⚠️ Ошибка: ModuleNotFoundError

Если видите ошибку `ModuleNotFoundError: No module named 'environs'`, нужно установить зависимости.

## 📦 Установка зависимостей через виртуальное окружение (рекомендуется)

### Автоматическая установка (скрипт)

```bash
cd /root/ser_lighter
chmod +x install_dependencies.sh
./install_dependencies.sh
```

Скрипт автоматически:
- Установит `python3-venv` если нужно
- Создаст виртуальное окружение `venv`
- Установит все зависимости из `requirements.txt`

### Ручная установка

```bash
cd /root/ser_lighter

# Устанавливаем python3-venv (если нужно)
apt update
apt install -y python3.10-venv || apt install -y python3-venv

# Создаем виртуальное окружение
python3 -m venv venv

# Активируем
source venv/bin/activate

# Обновляем pip
pip install --upgrade pip

# Устанавливаем зависимости
pip install -r requirements.txt
```

**Важно:** Service файл уже настроен на использование `/root/ser_lighter/venv/bin/python`

## ✅ После установки зависимостей

```bash
# Перезапустите сервис
sudo systemctl restart l.service

# Проверьте статус
sudo systemctl status l.service

# Смотрите логи
sudo journalctl -u l.service -f
```

## 🔍 Проверка установки

```bash
# Проверьте что зависимости установлены
python3 -c "import environs; print('OK')"
python3 -c "import aiogram; print('OK')"
python3 -c "import aiosqlite; print('OK')"
python3 -c "import lighter; print('OK')"
```

## 📝 Если проблемы с правами

Если pip требует sudo:
```bash
sudo python3 -m pip install -r requirements.txt
```

Или установите для пользователя:
```bash
python3 -m pip install --user -r requirements.txt
```

