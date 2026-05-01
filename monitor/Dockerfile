FROM python:3.11-slim
WORKDIR /app
RUN pip install requests docker
COPY monitor.py .
CMD ["python", "-u", "monitor.py"]
