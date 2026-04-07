ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.12
FROM $BUILD_FROM

# Install Chromium system dependencies
RUN apk add --no-cache \
    chromium \
    chromium-chromedriver \
    nss \
    freetype \
    harfbuzz \
    ca-certificates \
    ttf-freefont \
    font-noto \
    udev \
    && rm -rf /var/cache/apk/*

# Tell Playwright to use the system Chromium instead of downloading its own
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium-browser

# Install Python dependencies
RUN pip install --no-cache-dir \
    playwright>=1.40.0 \
    pillow>=10.0.0 \
    aiohttp>=3.9.0 \
    beautifulsoup4>=4.12.0

# Copy addon scripts
COPY fetcher.py /fetcher.py
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
