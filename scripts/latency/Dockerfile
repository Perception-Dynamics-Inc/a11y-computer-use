# Deliberately pinned app versions; changes require a new calibration report.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    gdb ca-certificates curl git xz-utils python3 python3-venv python3-gi \
    gir1.2-atspi-2.0 gir1.2-gtk-3.0 at-spi2-core=2.56.2-1+deb13u2 \
    mousepad=0.6.3-1 xfwm4=4.20.0-1 xvfb xauth dbus-x11 xdotool \
    fonts-liberation libasound2t64 libdbus-glib-1-2 libgtk-3-0 libnss3 \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_155.0.8059.84-1_amd64.deb -o /tmp/chrome.deb \
    && apt-get update && apt-get install -y /tmp/chrome.deb && rm /tmp/chrome.deb \
    && curl -fsSL https://archive.mozilla.org/pub/firefox/releases/158.0.1/linux-x86_64/en-US/firefox-158.0.1.tar.xz | tar -xJ -C /opt
ENV FIREFOX=/opt/firefox/firefox CHROME=/opt/google/chrome/chrome A11Y_VISIBLE_ACTIONS=0
WORKDIR /src
COPY . .
RUN python3 -m venv --system-site-packages /opt/baseline \
    && python3 -m venv --system-site-packages /opt/head \
    && /opt/head/bin/pip install '.[dev,browser]' \
    && printf 'a11y-computer-use==0.4.76 --hash=sha256:b3081933c90b824a56e2df7d1405a5d304713c1937a51bedd1ef87a1c599b262\n' > /tmp/baseline.txt \
    && /opt/baseline/bin/pip download --no-deps --require-hashes -r /tmp/baseline.txt -d /tmp/wheel \
    && /opt/baseline/bin/pip install /tmp/wheel/*.whl
CMD ["bash", "scripts/latency/session.sh", "/opt/head/bin/python", "scripts/latency/run.py", "--baseline", "/opt/baseline/bin/python", "--head", "/opt/head/bin/python", "--output", "/results/latency.json"]
