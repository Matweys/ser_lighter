# 🚀 Настройка автозапуска через systemd

## 📋 Установка сервиса

### 1. Скопируйте service файл в systemd:

```bash
sudo cp l.service /etc/systemd/system/l.service
```

### 2. Отредактируйте пути в файле (ОБЯЗАТЕЛЬНО!):

```bash
sudo nano /etc/systemd/system/l.service
```

**Обязательно измените:**
- `User` и `Group` - ваш пользователь на сервере (узнать: `whoami` и `id -gn`)
- `WorkingDirectory` - путь к проекту на сервере
- `ExecStart` - путь к Python (узнать: `which python3`) и путь к скрипту
- `EnvironmentFile` - путь к `.env` файлу на сервере

**Пример для Linux сервера:**
```ini
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/futures_bot
ExecStart=/usr/bin/python3 /home/ubuntu/futures_bot/lighter_trading_bot.py
EnvironmentFile=-/home/ubuntu/futures_bot/.env
```

### 3. Перезагрузите systemd:

```bash
sudo systemctl daemon-reload
```

### 4. Включите автозапуск:

```bash
sudo systemctl enable l.service
```

### 5. Запустите сервис:

```bash
sudo systemctl start l.service
```

## 🔍 Управление сервисом

### Проверка статуса:
```bash
sudo systemctl status l.service
```

### Просмотр логов:
```bash
# Все логи
sudo journalctl -u l.service -f

# Последние 100 строк
sudo journalctl -u l.service -n 100

# Логи за сегодня
sudo journalctl -u l.service --since today
```

### Остановка:
```bash
sudo systemctl stop l.service
```

### Перезапуск:
```bash
sudo systemctl restart l.service
```

### Отключение автозапуска:
```bash
sudo systemctl disable l.service
```

## ⚙️ Настройка путей

**Если проект в другом месте**, отредактируйте `/etc/systemd/system/l.service`:

```ini
WorkingDirectory=/path/to/futures_bot
ExecStart=/usr/bin/python3 /path/to/futures_bot/lighter_trading_bot.py
EnvironmentFile=-/path/to/futures_bot/.env
```

**Если Python в другом месте:**
```bash
which python3  # Узнать путь к Python
```

## 🔐 Безопасность

Сервис настроен с базовыми ограничениями безопасности:
- `NoNewPrivileges=true` - запрет повышения привилегий
- `PrivateTmp=true` - изолированный /tmp
- `ProtectSystem=strict` - защита системных файлов
- `ProtectHome=read-only` - защита домашних директорий

## 📝 Переменные окружения

Убедитесь, что файл `.env` содержит все необходимые переменные:
```bash
TELEGRAM_TOKEN=...
TELEGRAM_CHANNEL_ID=...
REDIS_URL=...
LIGHTER_SYMBOL=SOL
```

## ✅ Проверка работы

После запуска проверьте:
1. Статус: `sudo systemctl status l.service`
2. Логи: `sudo journalctl -u l.service -f`
3. Telegram: должны приходить уведомления в канал

## 🐛 Решение проблем

### Сервис не запускается:
```bash
# Проверьте логи
sudo journalctl -u l.service -n 50

# Проверьте права на файлы
ls -la /path/to/futures_bot/lighter_trading_bot.py
```

### Ошибки импорта:
- Убедитесь, что все зависимости установлены
- Проверьте виртуальное окружение (если используется)

### Ошибки подключения:
- Проверьте Redis (если используется)
- Проверьте Telegram токен
- Проверьте права на файл `.env`

