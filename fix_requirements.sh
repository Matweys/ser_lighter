#!/bin/bash
# Быстрое исправление requirements.txt на сервере

cd /root/ser_lighter

echo "🔧 Исправляем requirements.txt для Python 3.10..."

# Создаем резервную копию
cp requirements.txt requirements.txt.backup

# Удаляем проблемные строки и дубликаты
sed -i '/^pandas==2\.3\.2$/d' requirements.txt
sed -i '/^pandas_ta==0\.3\.14b0$/d' requirements.txt

# Убеждаемся что pandas==2.2.2 есть (если нет - добавляем)
if ! grep -q "^pandas==2\.2\.2$" requirements.txt; then
    # Добавляем после numpy
    sed -i '/^numpy==/a pandas==2.2.2' requirements.txt
fi

echo "✅ requirements.txt исправлен!"
echo ""
echo "📋 Проверка:"
grep -E "^pandas" requirements.txt

