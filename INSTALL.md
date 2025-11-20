# 🚀 Установка на сервере

## Быстрая установка systemd сервиса

### 1. На сервере скопируйте service файл:

```bash
cd /root/ser_lighter
sudo cp l.service /etc/systemd/system/l.service
```

### 2. Проверьте путь к Python (если нужно изменить):

```bash
which python3
# Если путь другой, отредактируйте ExecStart в /etc/systemd/system/l.service
```

### 3. Убедитесь, что файл `.env` существует:

```bash
ls -la /root/ser_lighter/.env
# Если нет, создайте с необходимыми переменными:
# TELEGRAM_TOKEN=...
# TELEGRAM_CHANNEL_ID=...
# REDIS_URL=...
# LIGHTER_SYMBOL=SOL
```

### 4. Перезагрузите systemd и запустите:

```bash
sudo systemctl daemon-reload
sudo systemctl enable l.service
sudo systemctl start l.service
```

### 5. Проверьте статус:

```bash
sudo systemctl status l.service
```

### 6. Смотрите логи в реальном времени:

```bash
sudo journalctl -u l.service -f
```

## ✅ Готово!

Бот будет автоматически запускаться при перезагрузке сервера.

## 📋 Управление

```bash
# Статус
sudo systemctl status l.service

# Остановка
sudo systemctl stop l.service

# Запуск
sudo systemctl start l.service

# Перезапуск
sudo systemctl restart l.service

# Логи
sudo journalctl -u l.service -f
```

