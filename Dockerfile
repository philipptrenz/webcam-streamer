FROM python:3.12-alpine
RUN apk add --no-cache ffmpeg
RUN pip install --no-cache-dir pyyaml
COPY switcher.py /app/switcher.py
ENTRYPOINT ["python3", "/app/switcher.py"]
