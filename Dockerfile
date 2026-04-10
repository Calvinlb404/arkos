FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 1112

CMD ["python", "-m", "uvicorn", "base_module.app:app", "--host", "0.0.0.0", "--port", "1112"]
