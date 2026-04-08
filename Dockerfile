FROM python:3.12-slim

# Set timezone to KST
ENV TZ=Asia/Seoul
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir pip --upgrade && \
    pip install --no-cache-dir .

# Copy source code
COPY . .

# Install package
RUN pip install --no-cache-dir -e .

# Expose dashboard port
EXPOSE 9095

CMD ["python", "scripts/run_bot.py", "--web", "--web-port", "9095"]
