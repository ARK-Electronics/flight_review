FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libfftw3-dev \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY app/requirements.txt ./app/requirements.txt
RUN pip install --no-cache-dir -r app/requirements.txt

# Copy application code
COPY . .

# Setup database and directories
RUN python app/setup_db.py

# Expose port (Kinsta will set the PORT env var)
EXPOSE 8080

# Start the application
CMD cd app && python serve.py --port ${PORT:-8080} --address 0.0.0.0 --allow-websocket-origin=ark-flight-review-9cuak.kinsta.app
