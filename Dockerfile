FROM python:3.9-slim-buster

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose port 80 if your bot has any web-facing components (e.g., a simple health check, if added)
# EXPOSE 80

# Command to run the bot. Assuming scanner.py is the main entry point.
# Adjust if your main script is different or if you need to pass specific arguments.
# For continuous operation, `scanner.py` is usually the one.
CMD ["python", "scanner.py"]
