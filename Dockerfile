FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir runpod
COPY handler.py /app/handler.py
WORKDIR /app
CMD ["python", "-u", "handler.py"]
