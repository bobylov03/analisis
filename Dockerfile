# Используем официальный тонкий образ Python
FROM python:3.11-slim

# 1) Устанавливаем LibreOffice и удаляем кеш apt
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      libreoffice-core libreoffice-writer && \
    rm -rf /var/lib/apt/lists/*

# 2) Создаём рабочую директорию и копируем requirements.txt
WORKDIR /app
COPY requirements.txt /app/requirements.txt

# 3) Устанавливаем Python-зависимости
RUN pip install --no-cache-dir -r /app/requirements.txt

# 4) Копируем весь остальной исходный код (bot.py, .docx-шаблоны, файлы .env не копируем!)
COPY . /app

# 5) Указываем команду запуска бота
CMD ["python", "bot.py"]
