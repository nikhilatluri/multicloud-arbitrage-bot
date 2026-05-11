FROM python:3.11-slim

WORKDIR /app
COPY pricefeed/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY pricefeed/app.py .
EXPOSE 8000

CMD ["uvicorn", "app:app", "--host=0.0.0.0", "--port=8000"]
