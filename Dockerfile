FROM python:3.12-slim

WORKDIR /app

# Install deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (no Revit proxy — requires Windows)
COPY cadre/ cadre/
COPY financial_mcp/ financial_mcp/
COPY web_search_mcp/ web_search_mcp/
COPY server.py .
COPY voice_client.html .
COPY landing.html .

# Cloud Run sets PORT automatically
ENV REVIT_ENABLED=false
ENV PORT=8080

EXPOSE 8080

CMD ["python", "server.py"]
