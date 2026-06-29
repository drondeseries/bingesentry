FROM python:3.9-slim

# Force stdin, stdout and stderr to be totally unbuffered
ENV PYTHONUNBUFFERED=1

# Set the working directory in the container
WORKDIR /app

# Install rclone
RUN apt-get update \
    && apt-get install -y rclone \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy all source files and default configuration
COPY *.py ./
COPY config.example.ini ./config/config.ini

# Run BingeSentry directly as daemon
CMD ["python", "main.py"]
