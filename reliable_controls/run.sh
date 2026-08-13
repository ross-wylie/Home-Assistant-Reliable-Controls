#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

# Supervisor writes every option to /data/options.json, including the panels
# list. The bridge reads that directly rather than us flattening a list of
# dicts into shell arguments.
CONFIG=/data/options.json

PANEL_COUNT=$(bashio::jq "${CONFIG}" '.panels | length')
if [ "${PANEL_COUNT}" = "0" ] || [ -z "${PANEL_COUNT}" ]; then
    bashio::log.fatal "No panels configured."
    bashio::log.fatal "Add at least one entry under 'panels' in the"
    bashio::log.fatal "Configuration tab: name, host, controller."
    bashio::exit.nok
fi

# ---------------------------------------------------------------------------
# MQTT broker: prefer Supervisor's managed broker, since the options schema
# has no MQTT fields any more.
# ---------------------------------------------------------------------------
if bashio::services.available 'mqtt'; then
    MQTT_HOST=$(bashio::services 'mqtt' 'host')
    MQTT_PORT=$(bashio::services 'mqtt' 'port')
    MQTT_USER=$(bashio::services 'mqtt' 'username')
    MQTT_PASS=$(bashio::services 'mqtt' 'password')
    bashio::log.info "Using MQTT broker ${MQTT_HOST}:${MQTT_PORT}"
else
    bashio::log.fatal "No MQTT broker available."
    bashio::log.fatal "Install and start the Mosquitto broker app, then"
    bashio::log.fatal "restart this one."
    bashio::exit.nok
fi

ARGS=(
    --config "${CONFIG}"
    --mqtt-host "${MQTT_HOST}"
    --mqtt-port "${MQTT_PORT}"
)
[ -n "${MQTT_USER}" ] && ARGS+=(--mqtt-user "${MQTT_USER}")
[ -n "${MQTT_PASS}" ] && ARGS+=(--mqtt-pass "${MQTT_PASS}")

if bashio::config.true 'purge_on_start'; then
    bashio::log.warning "PURGE enabled: all retained discovery configs for this"
    bashio::log.warning "bridge will be deleted, then republished from scratch."
    bashio::log.warning "Set purge_on_start back to false once orphans are gone."
    ARGS+=(--purge)
fi

if bashio::config.true 'verbose'; then
    ARGS+=(-vv)
fi

if bashio::config.true 'read_only'; then
    bashio::log.info "READ-ONLY: variables publish as sensors, nothing is written."
else
    bashio::log.warning "WRITE ENABLED: Home Assistant can change variables"
    bashio::log.warning "on all ${PANEL_COUNT} configured panel(s)."
fi

bashio::log.info "Starting bridge for ${PANEL_COUNT} panel(s)"

cd /app || bashio::exit.nok
exec python3 -u rc_mqtt_bridge.py "${ARGS[@]}"
