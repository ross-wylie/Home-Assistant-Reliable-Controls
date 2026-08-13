# Supervisor 2026.04.0 removed the automatic BUILD_FROM fallback, so the ARG
# carries its own default. build.yaml overrides it per architecture; the
# default here matches a normal x86 Home Assistant box.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest
FROM ${BUILD_FROM}

# The HA base images are Alpine and already carry bashio + s6-overlay, which
# run.sh needs. Newer pip refuses to touch system site-packages without
# --break-system-packages; in a single-purpose container that is what we want.
RUN apk add --no-cache python3 py3-pip \
    && pip3 install --no-cache-dir --break-system-packages paho-mqtt \
    && rm -rf /root/.cache

WORKDIR /app
COPY rcp.py rc_mqtt_bridge.py rc_mock_panel.py /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
