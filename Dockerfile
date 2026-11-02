# Wersja obrazu musi odpowiadać wersji pakietu playwright w requirements.txt (przeglądarki są w obrazie).
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Warsaw \
    PIP_BREAK_SYSTEM_PACKAGES=1

WORKDIR /app

COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt

COPY vulcan_forwarder ./vulcan_forwarder

# Użytkownik bez uprawnień roota, ze stałym UID — katalogi state/ i debug/ na hoście trzeba mu oddać:
#   sudo chown -R 10001:10001 state debug
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin forwarder \
    && mkdir -p /app/state /app/debug \
    && chown -R forwarder:forwarder /app/state /app/debug
USER forwarder

ENTRYPOINT ["python3", "-m", "vulcan_forwarder"]
CMD ["--loop"]
