FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py domain.py store.py settings.py schema.sql content.json rules.txt backup.py ./
RUN useradd --create-home bot
USER bot
CMD ["python", "bot.py"]
