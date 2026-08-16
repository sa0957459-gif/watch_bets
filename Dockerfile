FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY watch_bets.py .

# Playwright browsers are already in the base image
CMD ["python", "watch_bets.py"]