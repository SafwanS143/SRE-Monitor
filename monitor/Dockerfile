FROM python:3.11-slim
WORKDIR /app
RUN pip install requests docker numpy scikit-learn
COPY monitor.py anomaly_detector.py ./
CMD ["python", "-u", "monitor.py"]
