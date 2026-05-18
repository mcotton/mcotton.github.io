FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV FLASK_APP=app

EXPOSE 5000

ENTRYPOINT ["./entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:5000", \
     "--workers", "2", "--threads", "4", "--worker-class", "gthread", \
     "--timeout", "60", "--keep-alive", "5", \
     "--access-logfile", "-", "--error-logfile", "-", "--log-level", "warning", \
     "app:app"]
