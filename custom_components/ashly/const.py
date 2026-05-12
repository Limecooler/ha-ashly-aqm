"""Constants for the Ashly Audio integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "ashly"

DEFAULT_PORT = 8000
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "secret"
DEFAULT_SCAN_INTERVAL = 30

CONF_PORT = "port"

# Device topology (AQM1208)
NUM_INPUTS = 12
NUM_OUTPUTS = 8
NUM_DVCA_GROUPS = 12
NUM_MIXERS = NUM_OUTPUTS  # one mixer per output

# Channel ID format used by the device
INPUT_CHANNEL_ID = "InputChannel.{n}"
OUTPUT_CHANNEL_ID = "OutputChannel.{n}"
MIXER_ID = "Mixer.{n}"
DVCA_LEVEL_ID = "DCAChannel.{n}.Level"
DVCA_MUTE_ID = "DCAChannel.{n}.Mute"
MIXER_SOURCE_LEVEL_ID = "Mixer.{m}.InputChannel.{i}.Source Level"
MIXER_SOURCE_MUTE_ID = "Mixer.{m}.InputChannel.{i}.Source Mute"

# Source-level dB range, per device parameter type
MIXER_LEVEL_MIN_DB = -50.1
MIXER_LEVEL_MAX_DB = 12.0
MIXER_LEVEL_STEP_DB = 0.1

# DVCA level dB range; matched to mixer source level (device docs).
DVCA_LEVEL_MIN_DB = -50.1
DVCA_LEVEL_MAX_DB = 12.0
DVCA_LEVEL_STEP_DB = 0.1

# Mic preamp gain — discrete 6 dB steps from 0..+66 dB (per device API
# `micPreamp/type` and the AQM1208 manual). Treated as a stepped number.
MIC_PREAMP_GAIN_MIN_DB = 0
MIC_PREAMP_GAIN_MAX_DB = 66
MIC_PREAMP_GAIN_STEP_DB = 6
MIC_PREAMP_GAIN_ALLOWED = (0, 6, 12, 18, 24, 30, 36, 42, 48, 54, 60, 66)

# General-purpose outputs (2 pins on AQM1208 rear panel)
NUM_GPO = 2
GPO_PIN_ID = "General Purpose Output Pin.{n}"

# Live meter websocket — the AquaControl Portal uses socket.io 4.x on port 8001.
METER_WS_PORT = 8001
METER_INPUT_RANGE_DB = (-60.0, 20.0)  # dBu scale per channel meterParameter
# Reasonable HA refresh cadence: meters arrive ~6 Hz from the device, we
# throttle to 1 Hz to avoid spamming the recorder / frontend.
METER_PUBLISH_INTERVAL_S = 1.0

ASHLY_MAC_PREFIX = "0014AA"

# Sentinel for "no mixer assigned to this output"
NO_MIXER = "None"

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]
