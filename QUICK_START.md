# 🚀 Быстрый старт

## 📋 Установка systemd сервиса

### 1. Скопируйте файл на сервер:

```bash
scp l.service user@server:/tmp/
```

### 2. На сервере отредактируйте пути:

```bash
sudo nano /tmp/l.service
```

**Измените:**
- `User` - ваш пользователь (`whoami`)
- `Group` - ваша группа (`id -gn`)
- `WorkingDirectory` - путь к проекту
- `ExecStart` - путь к Python (`which python3`) и скрипту
- `EnvironmentFile` - путь к `.env`
- `ReadWritePaths` - путь к проекту

### 3. Установите сервис:

```bash
sudo cp /tmp/l.service /etc/systemd/system/l.service
sudo systemctl daemon-reload
sudo systemctl enable l.service
sudo systemctl start l.service
```

### 4. Проверьте статус:

```bash
sudo systemctl status l.service
```

### 5. Смотрите логи:

```bash
sudo journalctl -u l.service -f
```

## ✅ Готово!

Бот будет автоматически запускаться при перезагрузке сервера.

