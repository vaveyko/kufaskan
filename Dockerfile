FROM python:3.12-slim

WORKDIR /app

# Для работы pip install . нам нужны и настройки, и сам файл скрипта
COPY pyproject.toml ./
COPY main.py ./

# Устанавливаем зависимости и сам проект одной командой через обычный pip
RUN pip install --no-cache-dir .

# Создаем папку для базы данных
RUN mkdir -p /app/data

# Запускаем бота
CMD ["python", "main.py"]