FROM python:3.11-slim
WORKDIR /elastik
COPY . .
RUN pip install uvicorn
EXPOSE 3004
ENV PYTHONUNBUFFERED=1
CMD ["python", "server.py"]
