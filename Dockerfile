FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Copy the example config if no config.json exists
RUN [ ! -f config.json ] && cp config.example.json config.json || true

ENV OVERMESH_HOST=0.0.0.0
ENV OVERMESH_PORT=8081

EXPOSE 8081

CMD ["python3", "app.py"]
