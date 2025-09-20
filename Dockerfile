# Используем официальный Python образ
FROM python:3.11-slim

# Устанавливаем системные зависимости для сборки
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем uv для быстрой установки зависимостей (опционально, но рекомендуется)
RUN pip install --no-cache-dir uv

# Создаем рабочую директорию
WORKDIR /app

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем зависимости через uv (быстрее чем pip)
RUN uv pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY . .

# Создаем директорию для логов
RUN mkdir -p logs

# Устанавливаем переменные окружения
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Команда запуска бота
CMD ["python", "bot.py"]