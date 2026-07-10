#!/usr/bin/env python3
"""
Withings → Home Assistant MQTT bridge.

Polls the Withings API for new body measurements and publishes them
to Home Assistant via MQTT discovery, matching the official Withings
integration's sensor structure.

First-time setup (OAuth authorization):
    python withings_mqtt.py --setup

Normal operation:
    python withings_mqtt.py
"""

import argparse
import http.server
import json
import logging
import os
import sys
import time
import threading
import webbrowser
from urllib.parse import urlencode, urlparse, parse_qs

import paho.mqtt.client as mqtt
import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Withings API endpoints
AUTH_URL    = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL   = "https://wbsapi.withings.net/v2/oauth2"
MEASURE_URL = "https://wbsapi.withings.net/measure"

# Withings measure type id → (slug, friendly name, unit, HA device_class or None)
# Matches the sensors exposed by the official HA Withings integration.
MEASURES = {
    1:  ("weight_kg",             "Weight",              "kg",    "weight"),
    5:  ("fat_free_mass_kg",      "Fat free mass",       "kg",    "weight"),
    6:  ("fat_ratio",             "Fat ratio",           "%",     None),
    8:  ("fat_mass_kg",           "Fat mass",            "kg",    "weight"),
    76: ("muscle_mass_kg",        "Muscle mass",         "kg",    "weight"),
    77: ("hydration_kg",          "Hydration",           "kg",    "weight"),
    88: ("bone_mass_kg",          "Bone mass",           "kg",    "weight"),
    91: ("pulse_wave_velocity",   "Pulse wave velocity", "m/s",   None),
}

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
TOKENS_FILE = os.path.join(SCRIPT_DIR, "tokens.json")
STATE_FILE  = os.path.join(SCRIPT_DIR, "state.json")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.yaml")

trigger_event = threading.Event()

class _WebhookHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/api/webhook/withings_sync":
            self.send_response(200)
            self.end_headers()
            log.info("Webhook trigger received from router, polling now...")
            trigger_event.set()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # suppress request logging

def start_webhook_listener(port=8888):
    server = http.server.HTTPServer(("0.0.0.0", port), _WebhookHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info("Webhook listener started on port %d", port)

# ── Configuration ─────────────────────────────────────────────────────────────

def load_config():
    if not os.path.exists(CONFIG_FILE):
        log.error("config.yaml not found in %s", SCRIPT_DIR)
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


# ── Token management ──────────────────────────────────────────────────────────

def load_tokens():
    if not os.path.exists(TOKENS_FILE):
        return None
    with open(TOKENS_FILE) as f:
        return json.load(f)


def save_tokens(tokens):
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=2)
    log.debug("Tokens saved.")


def refresh_tokens(config, tokens):
    """Exchange a refresh_token for a new access_token. Mutates tokens dict."""
    log.info("Access token expired, refreshing...")
    r = requests.post(TOKEN_URL, data={
        "action":        "requesttoken",
        "client_id":     config["withings"]["client_id"],
        "client_secret": config["withings"]["client_secret"],
        "refresh_token": tokens["refresh_token"],
        "grant_type":    "refresh_token",
    })
    r.raise_for_status()
    body = r.json()
    if body["status"] != 0:
        raise RuntimeError(f"Token refresh failed: {body}")
    b = body["body"]
    tokens["access_token"]  = b["access_token"]
    tokens["refresh_token"] = b["refresh_token"]
    tokens["expires_at"]    = int(time.time()) + b["expires_in"] - 60
    save_tokens(tokens)
    log.info("Tokens refreshed successfully.")


def ensure_valid_token(config, tokens):
    if time.time() >= tokens["expires_at"]:
        refresh_tokens(config, tokens)


# ── OAuth setup (run once) ────────────────────────────────────────────────────

def run_oauth_setup(config):
    """
    Interactive OAuth2 authorization flow.
    Opens a browser, catches the redirect, exchanges the code for tokens,
    and saves them to tokens.json.
    """
    client_id     = config["withings"]["client_id"]
    client_secret = config["withings"]["client_secret"]
    redirect_uri  = config["withings"].get("redirect_uri", "http://localhost:8888/callback")

    port = urlparse(redirect_uri).port or 8888

    auth_params = {
        "response_type": "code",
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "scope":         "user.metrics",
        "state":         "withings_mqtt",
    }
    auth_link = f"{AUTH_URL}?{urlencode(auth_params)}"

    auth_code = {"value": None}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            if "code" in qs:
                auth_code["value"] = qs["code"][0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<h2>Authorization successful. You can close this window.</h2>")
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"<h2>Authorization failed &mdash; no code received.</h2>")

        def log_message(self, fmt, *args):
            pass

    server = http.server.HTTPServer(("0.0.0.0", port), _Handler)
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()

    print(f"\nOpening browser for Withings authorization...")
    print(f"If it does not open automatically, visit:\n  {auth_link}\n")
    webbrowser.open(auth_link)
    t.join(timeout=120)
    server.server_close()

    if not auth_code["value"]:
        log.error("No authorization code received within 120 seconds. Aborting.")
        sys.exit(1)

    r = requests.post(TOKEN_URL, data={
        "action":        "requesttoken",
        "client_id":     client_id,
        "client_secret": client_secret,
        "code":          auth_code["value"],
        "redirect_uri":  redirect_uri,
        "grant_type":    "authorization_code",
    })
    r.raise_for_status()
    body = r.json()
    if body["status"] != 0:
        log.error("Token exchange failed: %s", body)
        sys.exit(1)

    b = body["body"]
    tokens = {
        "access_token":  b["access_token"],
        "refresh_token": b["refresh_token"],
        "expires_at":    int(time.time()) + b["expires_in"] - 60,
        "user_id":       b["userid"],
    }
    save_tokens(tokens)
    print(f"\nSetup complete! Tokens saved to tokens.json")
    print(f"User ID: {tokens['user_id']}")
    print(f"\nYou can now run the bridge with:  python withings_mqtt.py\n")


# ── Withings API ──────────────────────────────────────────────────────────────

def fetch_measurements(tokens, since_timestamp=0):
    """
    Fetch all measurement groups newer than since_timestamp.
    Returns a list of measuregrps sorted oldest-first.
    """
    params = {
        "action":      "getmeas",
        "meastypes":   ",".join(str(t) for t in MEASURES),
        "category":    1,               # real measurements, not goals
        "lastupdate":  since_timestamp,
    }
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    r = requests.get(MEASURE_URL, params=params, headers=headers)
    r.raise_for_status()
    body = r.json()
    if body["status"] != 0:
        raise RuntimeError(f"Withings API error: {body}")
    grps = body["body"].get("measuregrps", [])
    return sorted(grps, key=lambda g: g["date"])


def decode_group(measuregrp):
    """
    Decode a measuregrp into {slug: float_value}.
    Withings encodes values as:  actual = value × 10^unit
    """
    result = {}
    for m in measuregrp["measures"]:
        if m["type"] not in MEASURES:
            continue
        slug = MEASURES[m["type"]][0]
        result[slug] = round(m["value"] * (10 ** m["unit"]), 4)
    return result


# ── MQTT / HA discovery ───────────────────────────────────────────────────────

def _discovery_topic(slug, user_id, cfg):
    prefix = cfg["mqtt"].get("discovery_prefix", "homeassistant")
    return f"{prefix}/sensor/withings_{user_id}/{slug}/config"


def _state_topic(slug, user_id, cfg):
    base = cfg["mqtt"].get("state_topic_base", "withings")
    return f"{base}/{user_id}/{slug}"


def publish_discovery(mqttc, tokens, cfg):
    """Send HA MQTT discovery config for every known sensor. Called on connect."""
    #user_id = tokens["user_id"]
    user_id = cfg["withings"]["user_name"]
    device = {
        "identifiers":  [f"withings_{user_id}"],
        "name":         f"Withings {user_id}",
        "manufacturer": "Withings",
    }
    for type_id, (slug, name, unit, device_class) in MEASURES.items():
        payload = {
            "name":                 f"{user_id} {name}",
            "unique_id":            f"withings_{user_id}_{slug}",
            "state_topic":          _state_topic(slug, user_id, cfg),
            "unit_of_measurement":  unit,
            "state_class":          "measurement",
            "device":               device,
        }
        if device_class:
            payload["device_class"] = device_class

        mqttc.publish(
            _discovery_topic(slug, user_id, cfg),
            json.dumps(payload),
            retain=True,
        )
    log.info("MQTT discovery messages published for user %s.", user_id)


def publish_values(mqttc, tokens, values, cfg):
    user_id = cfg["withings"]["user_name"]
#    user_id = tokens["user_id"]
    for slug, value in values.items():
        topic = _state_topic(slug, user_id, cfg)
        mqttc.publish(topic, str(value), retain=True)
        log.info("  %-28s %s", slug, value)


# ── State persistence ─────────────────────────────────────────────────────────

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_measurement_date": 0}
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Bridge main loop ──────────────────────────────────────────────────────────

def run_bridge(config):
    tokens = load_tokens()
    if not tokens:
        log.error("No tokens found. Run with --setup first.")
        sys.exit(1)

    mqtt_cfg = config["mqtt"]

    connected = threading.Event()

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            log.info("Connected to MQTT broker.")
            publish_discovery(client, tokens, config)
            connected.set()
        else:
            log.error("MQTT connection failed with rc=%d", rc)

    def on_disconnect(client, userdata, rc):
        if rc != 0:
            log.warning("Unexpected MQTT disconnect (rc=%d). Will reconnect.", rc)

    mqttc = mqtt.Client(client_id="withings_mqtt_bridge")
    mqttc.on_connect    = on_connect
    mqttc.on_disconnect = on_disconnect

    if mqtt_cfg.get("username"):
        mqttc.username_pw_set(mqtt_cfg["username"], mqtt_cfg.get("password", ""))

    mqttc.connect(mqtt_cfg["host"], mqtt_cfg.get("port", 1883), keepalive=60)
    mqttc.loop_start()

    if not connected.wait(timeout=15):
        log.error("Could not connect to MQTT broker within 15 seconds.")
        sys.exit(1)

    state        = load_state()
    poll_interval = config.get("poll_interval", 300)
    log.info("Bridge running. Polling every %ds. Press Ctrl+C to stop.", poll_interval)

    # Start webhook listener for router trigger
    webhook_port = config.get("webhook_port", 8888)
    start_webhook_listener(webhook_port)
    
    try:
        while True:
            try:
                ensure_valid_token(config, tokens)
                grps = fetch_measurements(tokens, since_timestamp=state["last_measurement_date"])

                if grps:
                    log.info("Found %d new measurement group(s).", len(grps))
                    log.info("Groups: %s", str(grps))
                    for group in grps:
                        if group["attrib"] < 10 :
                            values = decode_group(group)
                            log.info("Publishing measurement group from timestamp %d:", group["date"])
                            publish_values(mqttc, tokens, values, config)
                            state["last_measurement_date"] = group["date"]
                    save_state(state)
                else:
                    log.debug("No new measurements.")

            except Exception as exc:
                log.error("Poll error: %s", exc)

            # Wait for webhook trigger OR normal poll interval, whichever comes first
            triggered = trigger_event.wait(timeout=poll_interval)
            if triggered:
                log.info("Triggered by scale sync event.")
                trigger_event.clear()

    except KeyboardInterrupt:
        log.info("Stopping.")
    finally:
        mqttc.loop_stop()
        mqttc.disconnect()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Withings → Home Assistant MQTT bridge")
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Run the one-time OAuth2 authorization flow to obtain tokens",
    )
    args = parser.parse_args()

    cfg = load_config()

    if args.setup:
        run_oauth_setup(cfg)
    else:
        run_bridge(cfg)
