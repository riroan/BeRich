FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir pip --upgrade && \
    pip install --no-cache-dir .

# Copy source code
COPY . .

# Install package
RUN pip install --no-cache-dir -e .

CMD ["python", "scripts/run_bot.py"]
