FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

ENV OVERMESH_CONFIG=/app/data/config.json
ENV OVERMESH_DATA_DIR=/app/data
ENV OVERMESH_HOST=0.0.0.0
ENV OVERMESH_PORT=8081

EXPOSE 8081

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "app.py"]
