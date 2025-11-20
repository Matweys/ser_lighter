# 🖥️ Настройка на сервере

## ⚠️ Ошибка: ModuleNotFoundError

Если видите ошибку `ModuleNotFoundError: No module named 'environs'`, нужно установить зависимости.

## 📦 Установка зависимостей

### Вариант 1: Глобальная установка (рекомендуется)

```bash
cd /root/ser_lighter

# Обновляем pip
python3 -m pip install --upgrade pip

# Устанавливаем все зависимости
python3 -m pip install -r requirements.txt
```

### Вариант 2: Использование скрипта

```bash
cd /root/ser_lighter
chmod +x install_dependencies.sh
./install_dependencies.sh
```

### Вариант 3: Установка в виртуальное окружение

```bash
cd /root/ser_lighter

# Создаем виртуальное окружение
python3 -m venv venv

# Активируем
source venv/bin/activate

# Устанавливаем зависимости
pip install -r requirements.txt

# Обновляем service файл для использования venv
# ExecStart=/root/ser_lighter/venv/bin/python /root/ser_lighter/lighter_trading_bot.py
```

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

