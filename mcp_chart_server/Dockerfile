# ---------------------------------------------------------------------------
# MCP Chart Server — Phase 9.5
#
# Lightweight Python service exposing the generate_chart MCP tool over SSE.
# Receives structured chart inputs; returns a guaranteed-valid Vega-Lite spec.
# ---------------------------------------------------------------------------

FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy server source
COPY server.py .

EXPOSE 8087

# PORT env var allows override without rebuilding
ENV PORT=8087

CMD ["python", "server.py"]
