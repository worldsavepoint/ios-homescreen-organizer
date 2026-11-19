# Starts the stdio MCP server for source inspection. USB device operation runs locally on macOS.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY homescreen ./homescreen
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 organizer
USER organizer
ENV HOMESCREEN_READ_ONLY=1 HOMESCREEN_ALLOW_APPLY=0 HOMESCREEN_DATA=/tmp/screen-organizer
CMD ["home-screen-mcp"]
