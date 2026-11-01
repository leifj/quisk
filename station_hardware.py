# MQTT Hardware Module for Quisk
# Integrates station hardware via MQTT (specifically for XPA125B)
# Subclasses the Hermes hardware to maintain existing functionality.

from __future__ import print_function
from __future__ import absolute_import
from __future__ import division

import _quisk as QS
from hermes.quisk_hardware import Hardware as BaseHardware
import paho.mqtt.client as mqtt
import threading
import time
import json
import urllib.parse
import urllib.request
import wx
import socket
import os
import base64
import struct
try:
  import yaml as _yaml
except ImportError:
  _yaml = None

# Override station endpoint mappings here.
station_host_map = {
  "mqtt": {
    "host": "ka10.mnt.se",
    "port": 1883,
  },
  "lz1aq": {
    "host": "loopcontroller.local",
    "port": 80,
    "timeout": 0.35,
  },
  "switchctl": {
    "host": "10.0.0.229",
    "port": 80,
    "timeout": 0.25,
  },
  "xpa125b": {
    "host": "xpa125b.local",
    "fallback_hosts": (
      "10.0.0.66",
    ),
    "port": 80,
    "timeout": 1.0,
  },
  "atr1000": {
    "host": "10.0.0.180",
    "port": 60001,
    "timeout": 2.0,
  },
  "atu100": {
    "host": "atu100.local",
    "fallback_hosts": (
      "10.0.0.230",
    ),
    "port": 80,
    # The controller sits on WiFi; keep enough headroom for a marginal RF
    # link (responses over 1 s were observed on the bench).
    "timeout": 2.0,
  },
  "esp32swr": {
    "host": "esp32swr.local",
    "port": 80,
    # WiFi link; same headroom as the atu100.
    "timeout": 2.0,
  },
}

# ATR-1000 memory slot per band, indexed by antenna switch pin.
# Outer key = switchctl pin (int); inner key = Quisk band string; value = ATR-1000 memory ID 1-255.
# Add or edit pin entries to match your antenna/tuner setup.
atr1000_band_memory_map = {
  0: {             # Pin 0 (e.g. dipole / primary antenna)
    "10":  1,      # 10m  -> M1
    "12":  2,      # 12m  -> M2
    "15":  3,      # 15m  -> M3
    "17":  4,      # 17m  -> M4
    "20":  5,      # 20m  -> M5
    "30":  6,      # 30m  -> M6
    "40":  7,      # 40m  -> M7
    "80":  8,      # 80m  -> M8
    "160": 9,      # 160m -> M9
  },
  1: {             # Pin 1 (e.g. vertical / secondary antenna)
  },
  2: {             # Pin 2
  },
  3: {             # Pin 3
  },
}

mqtt_host = station_host_map["mqtt"]["host"]
mqtt_port = int(station_host_map["mqtt"]["port"])
mqtt_user = None
mqtt_pass = None
lz1aq_host = station_host_map["lz1aq"]["host"]
lz1aq_port = int(station_host_map["lz1aq"]["port"])
lz1aq_timeout = float(station_host_map["lz1aq"]["timeout"])
switchctl_host = station_host_map["switchctl"]["host"]
switchctl_port = int(station_host_map["switchctl"]["port"])
switchctl_timeout = float(station_host_map["switchctl"]["timeout"])
xpa125b_host = station_host_map["xpa125b"]["host"]
xpa125b_fallback_hosts = tuple(station_host_map["xpa125b"].get("fallback_hosts", ()))
xpa125b_port = int(station_host_map["xpa125b"]["port"])
xpa125b_timeout = float(station_host_map["xpa125b"]["timeout"])
atr1000_host = station_host_map["atr1000"]["host"]
atr1000_port = int(station_host_map["atr1000"]["port"])
atr1000_timeout = float(station_host_map["atr1000"]["timeout"])
atu100_host = station_host_map["atu100"]["host"]
atu100_fallback_hosts = tuple(station_host_map["atu100"].get("fallback_hosts", ()))
atu100_port = int(station_host_map["atu100"]["port"])
atu100_timeout = float(station_host_map["atu100"]["timeout"])
esp32swr_host = station_host_map["esp32swr"]["host"]
esp32swr_port = int(station_host_map["esp32swr"]["port"])
esp32swr_timeout = float(station_host_map["esp32swr"]["timeout"])
xpa125b_relay_button_config = (
  {"label": "Power", "relay_index": 3, "short_press_ms": 2000, "long_press_ms": 1000, "power_button": True},
  {"label": "Relay 1", "relay_index": 0, "short_press_ms": 400, "long_press_ms": 1000},
  {"label": "Relay 2", "relay_index": 1, "short_press_ms": 400, "long_press_ms": 1000},
  {"label": "Relay 3", "relay_index": 2, "short_press_ms": 400, "long_press_ms": 1000},
)

# Station config is loaded from a YAML file pointed to by:
#   QUISK_STATION_PROFILES=/path/to/station_config.yaml
#
# The YAML file is keyed by radio profile name. Set the active profile in your
# Quisk radio config file with:
#   station_profile = "<name>"
# Or override at runtime for all radio configs with:
#   QUISK_STATION_PROFILE=<name>
#
# Each profile can specify: equipment, hosts, mqtt credentials/topics,
# xpa125b_relays, and atr1000_band_memory. See station_config.yaml for the
# full schema. If the env var is not set or the file cannot be read, the
# built-in defaults below are used.

_station_equipment_profiles_builtin = {
  "default": frozenset({"mqtt", "xpa125b", "atr1000", "lz1aq", "switchctl", "esp32swr", "atu100"}),
}

def _load_station_config():
  """Load full per-profile config dict from YAML. Returns {profile_name: cfg} or None."""
  config_path = os.environ.get("QUISK_STATION_PROFILES", "")
  if not config_path:
    return None
  if _yaml is None:
    print("station_hardware: PyYAML not installed; cannot load %s" % config_path)
    return None
  try:
    with open(config_path, "r") as f:
      raw = _yaml.safe_load(f)
    if not isinstance(raw, dict):
      raise ValueError("Top-level value must be a mapping")
    # Support both {profiles: {name: cfg}} and flat {name: cfg}
    profiles = raw.get("profiles", raw)
    if not isinstance(profiles, dict):
      raise ValueError("'profiles' key must be a mapping")
    print("station_hardware: loaded config from %s: profiles=%s" % (config_path, list(profiles.keys())))
    return profiles
  except Exception as e:
    print("station_hardware: failed to load %s: %s" % (config_path, e))
    return None

_station_yaml_profiles = _load_station_config()

def _build_equipment_profiles(yaml_profiles):
  if not yaml_profiles:
    return _station_equipment_profiles_builtin
  loaded = {}
  for name, profile in yaml_profiles.items():
    eq = profile.get("equipment", []) if isinstance(profile, dict) else []
    loaded[str(name)] = frozenset(str(k) for k in eq)
  if "default" not in loaded:
    loaded["default"] = _station_equipment_profiles_builtin["default"]
  return loaded

station_equipment_profiles = _build_equipment_profiles(_station_yaml_profiles)

class Hardware(BaseHardware):
  REG_IOBOARD_TOGGLE = 11
  def __init__(self, app, conf):
    BaseHardware.__init__(self, app, conf)
    _profile_name = os.environ.get(
      'QUISK_STATION_PROFILE',
      getattr(conf, 'station_profile', 'default')
    )
    self.station_equipment = frozenset(
      station_equipment_profiles.get(_profile_name, station_equipment_profiles['default'])
    )

    # Resolve the YAML profile for this radio; fall back to empty dict (uses module defaults)
    _yaml_profile = {}
    if _station_yaml_profiles:
      _yaml_profile = _station_yaml_profiles.get(
        _profile_name, _station_yaml_profiles.get('default', {})
      )

    # --- Connection / host config (YAML overrides module-level defaults) ---
    _hosts = _yaml_profile.get("hosts", {})

    def _hget(device, field, default):
      return _hosts.get(device, {}).get(field, default)

    self.mqtt_host = _hget("mqtt", "host", mqtt_host)
    self.mqtt_port = int(_hget("mqtt", "port", mqtt_port))

    _mqtt_cfg = _yaml_profile.get("mqtt", {})
    self.mqtt_user = _mqtt_cfg.get("user", mqtt_user) or None
    self.mqtt_pass = _mqtt_cfg.get("pass", mqtt_pass) or None

    self.lz1aq_host = _hget("lz1aq", "host", lz1aq_host)
    self.lz1aq_port = int(_hget("lz1aq", "port", lz1aq_port))
    self.lz1aq_timeout = float(_hget("lz1aq", "timeout", lz1aq_timeout))

    self.switchctl_host = _hget("switchctl", "host", switchctl_host)
    self.switchctl_port = int(_hget("switchctl", "port", switchctl_port))
    self.switchctl_timeout = float(_hget("switchctl", "timeout", switchctl_timeout))

    _xpa_cfg = _hosts.get("xpa125b", {})
    self.xpa125b_host = _xpa_cfg.get("host", xpa125b_host)
    _fb = _xpa_cfg.get("fallback_hosts", list(xpa125b_fallback_hosts))
    self.xpa125b_fallback_hosts = tuple(_fb) if isinstance(_fb, (list, tuple)) else xpa125b_fallback_hosts
    self.xpa125b_port = int(_xpa_cfg.get("port", xpa125b_port))
    self.xpa125b_timeout = float(_xpa_cfg.get("timeout", xpa125b_timeout))
    self.xpa125b_active_host = self.xpa125b_host

    _atr_cfg = _hosts.get("atr1000", {})
    self.atr1000_host = _atr_cfg.get("host", atr1000_host)
    self.atr1000_port = int(_atr_cfg.get("port", atr1000_port))
    self.atr1000_timeout = float(_atr_cfg.get("timeout", atr1000_timeout))

    _atu_cfg = _hosts.get("atu100", {})
    self.atu100_host = _atu_cfg.get("host", atu100_host)
    _atu_fb = _atu_cfg.get("fallback_hosts", list(atu100_fallback_hosts))
    self.atu100_fallback_hosts = tuple(_atu_fb) if isinstance(_atu_fb, (list, tuple)) else atu100_fallback_hosts
    self.atu100_port = int(_atu_cfg.get("port", atu100_port))
    self.atu100_timeout = float(_atu_cfg.get("timeout", atu100_timeout))
    self.atu100_active_host = self.atu100_host

    _espswr_cfg = _hosts.get("esp32swr", {})
    self.esp32swr_host = _espswr_cfg.get("host", esp32swr_host)
    _espswr_fb = _espswr_cfg.get("fallback_hosts", ())
    self.esp32swr_fallback_hosts = tuple(_espswr_fb) if isinstance(_espswr_fb, (list, tuple)) else ()
    self.esp32swr_port = int(_espswr_cfg.get("port", esp32swr_port))
    self.esp32swr_timeout = float(_espswr_cfg.get("timeout", esp32swr_timeout))
    self.esp32swr_active_host = self.esp32swr_host

    # --- XPA125B relay button config ---
    _relay_cfg = _yaml_profile.get("xpa125b_relays", None)
    if _relay_cfg and isinstance(_relay_cfg, list):
      self.xpa125b_relay_button_config = tuple(dict(item) for item in _relay_cfg)
    else:
      self.xpa125b_relay_button_config = tuple(dict(item) for item in xpa125b_relay_button_config)

    # --- ATR-1000 band/memory map ---
    _band_mem = _yaml_profile.get("atr1000_band_memory", None)
    if _band_mem and isinstance(_band_mem, dict):
      self.atr1000_band_memory_map = {
        int(pin): {str(band): int(mem) for band, mem in (bands or {}).items()}
        for pin, bands in _band_mem.items()
      }
    else:
      self.atr1000_band_memory_map = dict(atr1000_band_memory_map)

    # --- MQTT topics (overridable from YAML) ---
    _mqtt_topics = _mqtt_cfg.get("topics", {})
    self.topic_setband = _mqtt_topics.get("setband", "xpa125b/setband")
    self.topic_status = _mqtt_topics.get("status", "xpa125b/#")
    self.topic_swr = _mqtt_topics.get("swr", "esp32swr/raw")

    # --- Runtime state ---
    self.amp_band = "Unknown"
    self.current_band = "Unknown"
    self.mqtt_connected = False
    self.ioboard_toggle_value = 0
    self.ioboard_toggle_sent = False
    self.ioboard_toggle_retry = 0
    self.swr_data = None
    self.last_swr_time = 0.0
    self.swr_stale_seconds = 1.5
    self.esp32swr_connected = False
    self.esp32swr_status = None
    self.esp32swr_last_error = None
    self.lz1aq_power = None
    self.lz1aq_mode = None
    self.lz1aq_connected = False
    self.lz1aq_poll_interval = 1.0
    self.lz1aq_poll_next = 0.0
    self.switchctl_power = None
    self.switchctl_pin = None
    self.switchctl_connected = False
    self.switchctl_poll_interval = 1.0
    self.switchctl_poll_next = 0.0
    self.control_poll_interval = 1.0
    self.control_polling_enabled = True
    self.xpa125b_connected = False
    self.xpa125b_power_state = None
    self.xpa125b_poll_interval = 2.0
    self.xpa125b_poll_next = 0.0
    self.xpa125b_polling_enabled = True
    self.xpa125b_sync_in_progress = False
    self.atr1000_connected = False
    self.atr1000_socket = None
    self.atr1000_socket_lock = threading.Lock()
    self.atr1000_polling_enabled = True
    self.atr1000_swr = None
    self.atr1000_fwd = None
    self.atr1000_inductance = None
    self.atr1000_capacitance = None
    self.atr1000_network = None
    self.atr1000_tuned = None
    self.atr1000_desired_memory_id = None
    self.atr1000_last_applied_memory_id = None
    self.atu100_connected = False
    self.atu100_status = None
    self.atu100_polling_enabled = True
    self.atu100_last_tune = 0.0
    self.atu100_last_tune_band = None
    self.atu100_tune_active = False
    self.atu100_keyed = False
    self.atu100_saved_power = None
    # --- ATU-100 tuning behaviour (hosts live under hosts.atu100) ---
    # Key a tuning carrier from quisk after arming the ATU. Without RF the
    # ATU-100 tune() fails instantly and atu_reset() zeroes the network,
    # which looks like "TUNE then RESET" on the display. The HL2 coupler
    # needs >= ~0.05 W for quisk's built-in SWR readout (fwd >= 0.05 in
    # quisk_widgets), so tune_power is a percent of normal drive and the
    # default of 50% keeps the readout alive while staying gentle.
    _atu_cfg = _yaml_profile.get("atu100", {})
    self.atu100_auto_key = bool(_atu_cfg.get("auto_key", True))
    self.atu100_tune_power = int(_atu_cfg.get("tune_power", 50))
    # Key the tune carrier even while quisk's amp-band TX inhibit is set
    # (XPA125B offline or band not yet confirmed). The guard is cleared only
    # for the duration of the tune carrier and restored afterwards.
    self.atu100_ignore_inhibit = bool(_atu_cfg.get("ignore_inhibit", False))
    # Adaptive tune cycle: keep the carrier up until the measured SWR
    # converges (esp32swr meter primary, the ATU's own reading as
    # fallback), bounded by tune_max_time. tune_settle is the number of
    # consecutive good readings required before declaring convergence.
    self.atu100_tune_max_time = int(_atu_cfg.get("tune_max_time", 25))
    self.atu100_tune_target_swr = float(_atu_cfg.get("tune_target_swr", 1.3))
    self.atu100_tune_settle = int(_atu_cfg.get("tune_settle", 2))
    self.atu100_block_reason = None
    self.atu100_inhibit_bypassed = False
    self.atu100_last_error = None
    self.atu100_saved_spot = None
    
    self.client = mqtt.Client()
    if self.mqtt_user and self.mqtt_pass:
      self.client.username_pw_set(self.mqtt_user, self.mqtt_pass)
      
    self.client.on_connect = self.on_connect
    self.client.on_message = self.on_message
    self.client.on_disconnect = self.on_disconnect

    if 'mqtt' in self.station_equipment:
      self.thread = threading.Thread(target=self.mqtt_loop)
      self.thread.daemon = True
      self.thread.start()

    if 'lz1aq' in self.station_equipment or 'switchctl' in self.station_equipment or 'atu100' in self.station_equipment or 'esp32swr' in self.station_equipment:
      self.control_thread = threading.Thread(target=self.control_loop)
      self.control_thread.daemon = True
      self.control_thread.start()

    if 'xpa125b' in self.station_equipment:
      self.xpa_thread = threading.Thread(target=self.xpa_loop)
      self.xpa_thread.daemon = True
      self.xpa_thread.start()

    if 'atr1000' in self.station_equipment:
      self.atr1000_thread = threading.Thread(target=self.atr1000_loop)
      self.atr1000_thread.daemon = True
      self.atr1000_thread.start()

    # Sync XPA125B controller config on startup
    if 'xpa125b' in self.station_equipment:
      self._StartXpa125bSyncConfigAsync()

  def xpa_loop(self):
    while self.xpa125b_polling_enabled:
      try:
        self.PollXpa125bConfig()
      except Exception:
        pass
      time.sleep(self.xpa125b_poll_interval)

  def control_loop(self):
    while self.control_polling_enabled:
      if 'switchctl' in self.station_equipment:
        try:
          self.PollSwitchCtlStatus()
        except Exception:
          pass
      if 'lz1aq' in self.station_equipment:
        try:
          self.PollLz1aqStatus()
        except Exception:
          pass
      if 'atu100' in self.station_equipment:
        try:
          self.PollAtu100Status()
        except Exception:
          pass
      if 'esp32swr' in self.station_equipment:
        try:
          self.PollEsp32SwrStatus()
        except Exception:
          pass
      time.sleep(self.control_poll_interval)

  def _FormatAmpLabel(self):
    yellow = wx.Colour(220, 200, 0)
    green = wx.Colour(0, 200, 0)
    red = wx.Colour(220, 60, 60)
    normal = self.conf.color_freq_txt
    if not self.xpa125b_connected:
      if self.xpa125b_sync_in_progress:
        return " AMP: Syncing... ", yellow
      if self.current_band != "Unknown":
        return " AMP: OFFLINE (want %s) " % self.current_band, red
      return " AMP: OFFLINE ", red
    if self.amp_band != "Unknown" and self.current_band != "Unknown":
      if self.amp_band == self.current_band:
        return " AMP: %s " % self.amp_band, green
      return " AMP: %s (want %s) " % (self.amp_band, self.current_band), yellow
    if self.amp_band != "Unknown":
      return " AMP: %s " % self.amp_band, normal
    if self.current_band != "Unknown":
      return " AMP: -> %s " % self.current_band, yellow
    return " AMP: Unknown ", normal

  def _ApplyAmpLabel(self, label, color):
    if hasattr(self.application, 'amp_status'):
      self.application.amp_status.SetLabel(label)
      self.application.amp_status.TextCtrl.SetForegroundColour(color)
      self.application.amp_status.TextCtrl.Refresh()

  def _FormatSwrLabel(self):
    normal = self.conf.color_freq_txt
    green = wx.Colour(0, 200, 0)
    yellow = wx.Colour(220, 200, 0)
    red = wx.Colour(220, 60, 60)
    if not self.swr_data:
      return " SWR: --- ", normal
    fwd = self.swr_data.get("fwd", "0.000")
    rev = self.swr_data.get("rev", "0.000")
    vswr = self.swr_data.get("vswr", "1.000")
    try:
      vswr_value = float(vswr)
    except Exception:
      return " SWR %s  F %s  R %s " % (vswr, fwd, rev), normal
    if vswr_value > 2.0:
      color = red
    elif vswr_value > 1.5:
      color = yellow
    else:
      color = green
    return " SWR %s  F %s  R %s " % (vswr, fwd, rev), color

  def _ApplySwrLabel(self, label, color):
    if hasattr(self.application, 'swr_status'):
      self.application.swr_status.SetLabel(label)
      self.application.swr_status.TextCtrl.SetForegroundColour(color)
      self.application.swr_status.TextCtrl.Refresh()

  def _UpdateSwrLabel(self):
    label, color = self._FormatSwrLabel()
    wx.CallAfter(self._ApplySwrLabel, label, color)

  def _UpdateAmpLabel(self):
    label, color = self._FormatAmpLabel()
    wx.CallAfter(self._ApplyAmpLabel, label, color)

  def mqtt_loop(self):
    while True:
      try:
        self.client.connect(self.mqtt_host, self.mqtt_port, 60)
        self.client.loop_forever()
      except Exception as e:
        print("MQTT Connection Error: %s" % str(e))
        time.sleep(5)

  def on_connect(self, client, userdata, flags, rc):
    if rc == 0:
      self.mqtt_connected = True
      if 'xpa125b' in self.station_equipment:
        client.subscribe(self.topic_status)
        if self.current_band != "Unknown":
          self.send_band(self.current_band)
      if 'esp32swr' in self.station_equipment:
        client.subscribe(self.topic_swr)
    else:
      self.mqtt_connected = False
    self._UpdateAmpLabel()

  def on_disconnect(self, client, userdata, rc):
    self.mqtt_connected = False
    self._UpdateAmpLabel()

  def on_message(self, client, userdata, msg):
    if 'xpa125b' in self.station_equipment:
      if msg.topic == "xpa125b/band":
        self.amp_band = msg.payload.decode().strip()
        self._UpdateAmpLabel()
        self.check_tx_inhibit()
      elif msg.topic == "xpa125b/status":
        pass
    if 'esp32swr' in self.station_equipment:
      if msg.topic == self.topic_swr:
        try:
          self.swr_data = json.loads(msg.payload.decode().strip())
          self.last_swr_time = time.monotonic()
          self._UpdateSwrLabel()
        except Exception:
          pass

  def send_band(self, band):
    if self.mqtt_connected and 'xpa125b' in self.station_equipment:
      self.client.publish(self.topic_setband, band)

  def _NormalizeLz1aqMode(self, mode):
    text = str(mode).strip()
    mapping = {
      "A": "LoopA",
      "loopA": "LoopA",
      "LoopA": "LoopA",
      "B": "LoopB",
      "loopB": "LoopB",
      "LoopB": "LoopB",
      "Crossed": "Crossed",
      "crossed": "Crossed",
      "Vertical": "Vertical",
      "vertical": "Vertical",
    }
    return mapping.get(text)

  def _ApplyLz1aqStatus(self, data):
    power_raw = str(data.get("power", "")).strip().lower()
    if power_raw == "true":
      self.lz1aq_power = True
    elif power_raw == "false":
      self.lz1aq_power = False
    mode = self._NormalizeLz1aqMode(data.get("mode", ""))
    if mode:
      self.lz1aq_mode = mode

  def _Lz1aqRequest(self, endpoint):
    url = "http://%s:%d%s" % (self.lz1aq_host, self.lz1aq_port, endpoint)
    try:
      with urllib.request.urlopen(url, timeout=self.lz1aq_timeout) as response:
        payload = response.read().decode("utf-8", "ignore")
      data = json.loads(payload)
      self._ApplyLz1aqStatus(data)
      self.lz1aq_connected = True
      return True
    except Exception:
      self.lz1aq_connected = False
      return False

  def PollLz1aqStatus(self):
    return self._Lz1aqRequest("/api/status")

  def SetLz1aqPower(self, enabled):
    if enabled:
      return self._Lz1aqRequest("/api/on")
    return self._Lz1aqRequest("/api/off")

  def SetLz1aqMode(self, mode):
    mode = self._NormalizeLz1aqMode(mode)
    if mode == "LoopA":
      return self._Lz1aqRequest("/api/loopA")
    if mode == "LoopB":
      return self._Lz1aqRequest("/api/loopB")
    if mode == "Crossed":
      return self._Lz1aqRequest("/api/crossed")
    if mode == "Vertical":
      return self._Lz1aqRequest("/api/vertical")
    return False

  def GetLz1aqPower(self):
    return self.lz1aq_power

  def GetLz1aqMode(self):
    return self.lz1aq_mode

  def _ApplySwitchCtlStatus(self, data):
    power_raw = data.get("power", None)
    if power_raw is not None:
      power_text = str(power_raw).strip().lower()
      if power_text == "true":
        self.switchctl_power = True
      elif power_text == "false":
        self.switchctl_power = False
    enabled_raw = data.get("enabled", None)
    if enabled_raw is not None:
      enabled_text = str(enabled_raw).strip().lower()
      if enabled_text == "true":
        self.switchctl_power = True
      elif enabled_text == "false":
        self.switchctl_power = False
    pin_raw = data.get("pin", None)
    try:
      pin = int(str(pin_raw).strip())
    except Exception:
      pin = None
    if pin is not None and -1 <= pin <= 5:
      self.switchctl_pin = pin
      if enabled_raw is None and power_raw is None:
        self.switchctl_power = pin >= 0

  def _SwitchCtlRequest(self, endpoint):
    url = "http://%s:%d%s" % (self.switchctl_host, self.switchctl_port, endpoint)
    try:
      with urllib.request.urlopen(url, timeout=self.switchctl_timeout) as response:
        payload = response.read().decode("utf-8", "ignore")
      data = json.loads(payload)
      self._ApplySwitchCtlStatus(data)
      self.switchctl_connected = True
      return True
    except Exception:
      self.switchctl_connected = False
      return False

  def PollSwitchCtlStatus(self):
    return self._SwitchCtlRequest("/api/status")

  def SetSwitchCtlPower(self, enabled):
    if enabled:
      return self._SwitchCtlRequest("/api/on")
    return self._SwitchCtlRequest("/api/off")

  def SetSwitchCtlPin(self, pin):
    try:
      pin = int(pin)
    except Exception:
      return False
    if pin < 0 or pin > 5:
      return False
    return self._SwitchCtlRequest("/api/on?pin=%d" % pin)

  def GetSwitchCtlPower(self):
    return self.switchctl_power

  def GetSwitchCtlPin(self):
    return self.switchctl_pin

  # --- ATU-100 WiFi controller (atu100wifi) -------------------------------
  # REST API on the ESP32 controller:
  #   /api/status  JSON state (power, swr, pwr_in/out, efficiency, L, C, ...)
  #   /api/tune    pulse the ATU TUNE line (tuning starts on the next RF)
  #   /api/reset   pulse the ATU RESET line
  #   /api/auto    toggle automatic tuning mode
  #   /api/bypass  toggle bypass

  def _Atu100Request(self, endpoint, params=None):
    hosts = []
    if self.atu100_active_host:
      hosts.append(self.atu100_active_host)
    if self.atu100_host and self.atu100_host not in hosts:
      hosts.append(self.atu100_host)
    for host in self.atu100_fallback_hosts:
      if host and host not in hosts:
        hosts.append(host)

    for host in hosts:
      url = "http://%s:%d%s" % (host, self.atu100_port, endpoint)
      if params:
        url += "?" + urllib.parse.urlencode(params)
      try:
        with urllib.request.urlopen(url, timeout=self.atu100_timeout) as response:
          payload = response.read().decode("utf-8", "ignore")
        self.atu100_connected = True
        self.atu100_active_host = host
        self.atu100_last_error = None
        try:
          return json.loads(payload)
        except Exception:
          return payload
      except Exception as e:
        # Keep the most recent failure visible in the status label; a proxy
        # environment variable, dead mDNS or a changed DHCP address all end
        # up here while ping and a browser still work.
        self.atu100_last_error = "%s: %s" % (type(e).__name__, e)
        continue

    self.atu100_connected = False
    return None

  def GetAtu100Connected(self):
    return self.atu100_connected

  def GetAtu100Status(self):
    return self.atu100_status

  def PollAtu100Status(self):
    response = self._Atu100Request("/api/status")
    if isinstance(response, dict):
      self.atu100_status = response
      wx.CallAfter(self._UpdateAtu100Label)
      return True
    return False

  def GetAtu100Tuning(self):
    """True while a tune cycle is armed/running or a request is in flight."""
    if self.atu100_tune_active:
      return True
    st = self.atu100_status
    if isinstance(st, dict):
      return str(st.get("tuning", "")).lower() == "true"
    return False

  def _Atu100EnsurePower(self):
    st = self.atu100_status if isinstance(self.atu100_status, dict) else {}
    if str(st.get("power", "")).lower() != "true":
      if self._Atu100Request("/api/on") is None:
        return False
      time.sleep(2.5)          # allow the ATU to boot its LCD
      self.PollAtu100Status()
    return True

  def _Atu100GetAppPtt(self):
    """Read the quisk PTT button state from the GUI thread."""
    box = {}
    done = threading.Event()

    def _read():
      btn = getattr(self.application, 'pttButton', None)
      box['tx'] = bool(btn.GetValue()) if btn else False
      done.set()

    wx.CallAfter(_read)
    done.wait(1.0)
    return box.get('tx', False)

  def _Atu100WaitTxInhibit(self, timeout):
    """Wait until quisk's amp-band TX guard clears so keying is allowed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
      if not getattr(self.application, 'tx_inhibit', False):
        return True
      time.sleep(0.1)
    return False

  def _Atu100KeyCarrier(self, on):
    app = getattr(self, 'application', None)
    if app is not None and hasattr(app, 'SetPTT'):
      wx.CallAfter(app.SetPTT, 1 if on else 0)
      return True
    return False

  def _Atu100OnSpot(self, level):
    # Mirror the Spot button's hardware side effects (IO board tuner bits).
    if hasattr(self, 'OnSpot'):
      wx.CallAfter(self.OnSpot, level)

  def _Atu100KeyCarrier(self, on):
    """Key the tune carrier with quisk's Spot mechanism.

    The spot path transmits a pure constant carrier in ANY mode
    (transmit_mic_carrier in microphone.c) while PTT is down; PTT alone
    produces no RF in SSB/DGT modes (suppressed carrier). The carrier
    amplitude is the full (already reduced) TX drive.
    """
    app = getattr(self, 'application', None)
    if app is None or not hasattr(app, 'SetPTT'):
      return False
    if on:
      box = {}
      done = threading.Event()

      def _read():
        btn = getattr(app, 'spotButton', None)
        box['down'] = bool(btn.GetValue()) if btn else False
        box['level'] = getattr(app, 'levelSpot', 500)
        done.set()

      wx.CallAfter(_read)
      done.wait(1.0)
      self.atu100_saved_spot = box if done.is_set() else {'down': False, 'level': 500}
      wx.CallAfter(QS.set_spot_level, 1000)
      self._Atu100OnSpot(1000)
      wx.CallAfter(app.SetPTT, 1)
    else:
      saved = self.atu100_saved_spot or {'down': False, 'level': 500}
      self.atu100_saved_spot = None
      if saved['down']:
        wx.CallAfter(QS.set_spot_level, saved['level'])
        self._Atu100OnSpot(saved['level'])
      else:
        wx.CallAfter(QS.set_spot_level, -1)
        self._Atu100OnSpot(-1)
      wx.CallAfter(app.SetPTT, 0)
    return True

  def _Atu100ApplyTxPower(self, pct):
    """Set the TX drive multiplier to pct% (GUI thread); returns prior values."""
    box = {}
    done = threading.Event()

    def _apply():
      app = self.application
      box['tx'] = getattr(app, 'tx_level', 100)
      box['dgt'] = getattr(app, 'digital_tx_level', None)
      app.tx_level = pct
      if box['dgt'] is not None:
        app.digital_tx_level = pct
      if hasattr(self, 'SetTxLevel'):
        self.SetTxLevel()
      done.set()

    wx.CallAfter(_apply)
    done.wait(2.0)
    return box if done.is_set() else None

  def _Atu100RestoreTxPower(self, saved):
    if not saved:
      return
    done = threading.Event()

    def _apply():
      app = self.application
      if 'tx' in saved:
        app.tx_level = saved['tx']
      if saved.get('dgt') is not None:
        app.digital_tx_level = saved['dgt']
      if hasattr(self, 'SetTxLevel'):
        self.SetTxLevel()
      done.set()

    wx.CallAfter(_apply)
    done.wait(2.0)

  def _Atu100SwrNow(self):
    """Current SWR as seen by the station: the esp32swr meter when it is
    active and valid, otherwise the ATU's own post-tune reading."""
    st = self.esp32swr_status
    if 'esp32swr' in self.station_equipment and isinstance(st, dict) \
        and str(st.get("valid", "")).lower() == "true":
      try:
        value = float(st.get("vswr", "1"))
        if value > 0:
          return value
      except Exception:
        pass
    st = self.atu100_status if isinstance(self.atu100_status, dict) else {}
    try:
      value = float(st.get("swr", "1"))
      if value > 0:
        return value
    except Exception:
      pass
    return None

  def Atu100Tune(self, key_carrier=None):
    """Arm the ATU tune and key a tuning carrier until the cycle finishes.

    The ATU-100 tunes only with RF on the input: the stock firmware fails
    the tune and runs atu_reset() when there is no carrier, so the carrier
    is keyed BEFORE the TUNE pulse lands. The carrier is held until the
    measured SWR converges (adaptive), never longer than tune_max_time.
    """
    if self.atu100_tune_active:
      return True
    self.atu100_tune_active = True
    self.atu100_block_reason = None
    converged = False
    try:
      if not self._Atu100EnsurePower():
        self.atu100_block_reason = "ATU unreachable"
        wx.CallAfter(self._UpdateAtu100Label)
        return False
      if key_carrier is None:
        key_carrier = self.atu100_auto_key
      if key_carrier and not self._Atu100GetAppPtt():
        inhibited = bool(getattr(self.application, 'tx_inhibit', False))
        if inhibited and self.atu100_ignore_inhibit:
          # Temporarily lift the amp-band guard for the low-power tune
          # carrier; restored in the finally block below.
          self._Atu100BypassInhibit(True)
        elif inhibited and not self._Atu100WaitTxInhibit(3.0):
          # The amp band is not confirmed: quisk unkeys any PTT while the
          # guard is set, so keying is pointless. Say so instead of failing
          # silently.
          key_carrier = False
          self.atu100_block_reason = "TX inhibit (amp band not confirmed)"
          print("ATU-100: tune skipped, %s" % self.atu100_block_reason)
          wx.CallAfter(self._UpdateAtu100Label)
      if key_carrier:
        saved = self._Atu100ApplyTxPower(self.atu100_tune_power)
        if saved:
          self.atu100_saved_power = saved
        self._Atu100KeyCarrier(1)
        self.atu100_keyed = True
        time.sleep(0.3)        # carrier onset before the ATU samples RF
      if self._Atu100Request("/api/tune") is None:
        return False

      # Adaptive hold: poll both the ATU and the SWR meter. Converged when
      # the SWR stays at or below the target for tune_settle consecutive
      # readings. The ATU leaving its TUNE state without convergence ends
      # the cycle early (it reset or gave up); the hard cap is tune_max_time.
      started = time.monotonic()
      armed_seen = False
      good = 0
      while time.monotonic() - started < self.atu100_tune_max_time:
        if not self.atu100_keyed:
          break                     # carrier released externally (Stop)
        time.sleep(0.5)
        self.PollAtu100Status()
        if 'esp32swr' in self.station_equipment:
          try:
            self.PollEsp32SwrStatus()
          except Exception:
            pass
        st = self.atu100_status if isinstance(self.atu100_status, dict) else {}
        if str(st.get("power", "")).lower() != "true":
          break
        if str(st.get("tuning", "")).lower() == "true":
          armed_seen = True
          good = 0
        vswr_now = self._Atu100SwrNow()
        if vswr_now is not None and vswr_now <= self.atu100_tune_target_swr:
          good += 1
          if good >= self.atu100_tune_settle:
            converged = True
            break
        else:
          good = 0
        if armed_seen and str(st.get("tuning", "")).lower() != "true" \
            and time.monotonic() - started > 3.0:
          break                     # ATU finished its cycle, SWR not met
      elapsed = time.monotonic() - started
      if converged:
        print("ATU-100: tune converged in %.1f s" % elapsed)
      else:
        print("ATU-100: tune ended without convergence after %.1f s" % elapsed)
    finally:
      self._Atu100BypassInhibit(False)
      self._Atu100ReleaseCarrier()
      self.atu100_tune_active = False
    return True

  def _Atu100BypassInhibit(self, bypass):
    """Clear (and later restore) application.tx_inhibit for the tune carrier."""
    if bypass and not self.atu100_inhibit_bypassed:
      self.atu100_inhibit_bypassed = True
      wx.CallAfter(setattr, self.application, 'tx_inhibit', False)
      print("ATU-100: TX inhibit lifted for the tune carrier")
    elif not bypass and self.atu100_inhibit_bypassed:
      self.atu100_inhibit_bypassed = False
      wx.CallAfter(setattr, self.application, 'tx_inhibit', True)

  def _Atu100ReleaseCarrier(self):
    """Drop the tuning carrier and restore Spot state and TX drive."""
    if self.atu100_keyed:
      self._Atu100KeyCarrier(0)
      self.atu100_keyed = False
      saved = self.atu100_saved_power
      self.atu100_saved_power = None
      if saved:
        self._Atu100RestoreTxPower(saved)

  def Atu100Stop(self):
    """Release the tuning carrier if this module keyed it."""
    if self.atu100_keyed:
      self._Atu100ReleaseCarrier()
      return True
    return False

  def Atu100Reset(self):
    return self._Atu100Request("/api/reset") is not None

  def Atu100Auto(self):
    return self._Atu100Request("/api/auto") is not None

  def Atu100Bypass(self):
    return self._Atu100Request("/api/bypass") is not None

  def _FormatAtu100Label(self):
    normal = self.conf.color_freq_txt
    green = wx.Colour(0, 200, 0)
    yellow = wx.Colour(220, 200, 0)
    red = wx.Colour(220, 60, 60)
    if self.atu100_block_reason:
      return " ATU: %s " % self.atu100_block_reason, red
    if not self.atu100_connected:
      err = getattr(self, 'atu100_last_error', None)
      if err:
        if len(err) > 34:
          err = err[0:33] + '…'
        return " ATU: offline (%s) " % err, normal
      return " ATU: --- ", normal
    st = self.atu100_status
    if not isinstance(st, dict):
      return " ATU: ... ", yellow

    def _val(key):
      value = st.get(key)
      if value is None:
        return None
      text = str(value).strip()
      if not text or text == "--":
        return None
      return text

    parts = []
    if _val("swr"):
      parts.append("SWR %s" % _val("swr"))
    if _val("pwr_out"):
      parts.append("%sW" % _val("pwr_out"))
    if _val("inductance"):
      parts.append("%suH" % _val("inductance"))
    if _val("capacitance"):
      parts.append("%spF" % _val("capacitance"))

    tuning = str(st.get("tuning", "")).lower() == "true"
    power_on = str(st.get("power", "")).lower() == "true"
    if tuning:
      parts.append("Tuning")
    elif not power_on:
      parts.append("Off")
    elif str(st.get("auto_mode", "")).lower() == "true":
      parts.append("Auto")
    else:
      parts.append("Man")

    label = " ATU: " + "  ".join(parts) + " "
    if tuning:
      color = yellow
    elif power_on:
      color = green
    else:
      color = normal
    return label, color

  def _ApplyAtu100Label(self, label, color):
    if hasattr(self.application, 'atu100_status'):
      self.application.atu100_status.SetLabel(label)
      self.application.atu100_status.TextCtrl.SetForegroundColour(color)
      self.application.atu100_status.TextCtrl.Refresh()

  def _UpdateAtu100Label(self):
    label, color = self._FormatAtu100Label()
    self._ApplyAtu100Label(label, color)

  def _ScheduleAtu100Tune(self, band):
    """Start a tune cycle after a band change.

    The controller pulses the ATU TUNE line; the ATU-100 then retunes on the
    next RF. Debounced per band so spinning across a band edge does not fire
    repeated tune pulses.
    """
    now = time.monotonic()
    if band == self.atu100_last_tune_band and now - self.atu100_last_tune < 2.0:
      return
    self.atu100_last_tune_band = band
    self.atu100_last_tune = now
    threading.Thread(target=self.Atu100Tune, daemon=True).start()

  def _IoBoardAvailable(self):
    return bool(getattr(self, 'io_board', None) and getattr(self.io_board, 'have_IO_Board', False))

  def IsIoBoardAvailable(self):
    return self._IoBoardAvailable()

  def GetIoBoardToggle(self):
    return self.ioboard_toggle_value

  def SetIoBoardToggle(self, value):
    self.ioboard_toggle_value = 1 if value else 0
    self.ioboard_toggle_retry = 3
    if self._IoBoardAvailable():
      self.WriteI2C(0x7d, 0x1D, self.REG_IOBOARD_TOGGLE, self.ioboard_toggle_value)
      self.ioboard_toggle_sent = True
      self.ioboard_toggle_retry -= 1

  def _Atr1000WsConnect(self):
    s = socket.create_connection((self.atr1000_host, self.atr1000_port), timeout=self.atr1000_timeout)
    s.settimeout(5.0)
    key = base64.b64encode(os.urandom(16)).decode()
    upgrade = (
      "GET / HTTP/1.1\r\n"
      "Host: %s:%d\r\n"
      "Upgrade: websocket\r\n"
      "Connection: Upgrade\r\n"
      "Sec-WebSocket-Key: %s\r\n"
      "Sec-WebSocket-Version: 13\r\n"
      "\r\n"
    ) % (self.atr1000_host, self.atr1000_port, key)
    s.sendall(upgrade.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
      chunk = s.recv(1024)
      if not chunk:
        raise IOError("ATR-1000 WebSocket handshake failed")
      buf += chunk
    return s

  def _Atr1000WsSend(self, s, data):
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    s.sendall(bytes([0x82, 0x80 | len(data)]) + mask + masked)

  def _Atr1000WsRecvFrame(self, s):
    header = b""
    while len(header) < 2:
      try:
        chunk = s.recv(2 - len(header))
      except socket.timeout:
        return b""
      if not chunk:
        return None
      header += chunk
    length = header[1] & 0x7F
    if length == 126:
      ext = b""
      while len(ext) < 2:
        try:
          chunk = s.recv(2 - len(ext))
        except socket.timeout:
          return b""
        if not chunk:
          return None
        ext += chunk
      length = struct.unpack(">H", ext)[0]
    elif length == 127:
      ext = b""
      while len(ext) < 8:
        try:
          chunk = s.recv(8 - len(ext))
        except socket.timeout:
          return b""
        if not chunk:
          return None
        ext += chunk
      length = struct.unpack(">Q", ext)[0]
    payload = b""
    while len(payload) < length:
      try:
        chunk = s.recv(length - len(payload))
      except socket.timeout:
        return b""
      if not chunk:
        return None
      payload += chunk
    return payload

  def _Atr1000ParseFrame(self, s, payload):
    if len(payload) < 2 or payload[0] != 0xFF:
      return
    cmd = payload[1]
    if cmd == 2 and len(payload) >= 10:  # SCMD_METER_STATUS
      swr_raw = struct.unpack_from("<H", payload, 4)[0]
      fwd = struct.unpack_from("<H", payload, 6)[0]
      self.atr1000_swr = round(swr_raw / 100.0, 2) if swr_raw >= 100 else float(swr_raw)
      self.atr1000_fwd = fwd
      wx.CallAfter(self._UpdateAtr1000Label)
    elif cmd == 5 and len(payload) >= 9:  # SCMD_RELAY_STATUS
      sw = payload[3]
      ind = payload[4]
      cap = payload[5]
      L = struct.unpack_from("<H", payload, 6)[0] / 100.0
      C = struct.unpack_from("<H", payload, 8)[0]
      self.atr1000_network = "LC" if sw == 0 else "CL"
      self.atr1000_inductance = L
      self.atr1000_capacitance = C
      self.atr1000_tuned = (ind > 0 or cap > 0)
      wx.CallAfter(self._UpdateAtr1000Label)
    elif cmd == 254:  # SCMD_TEST - device keepalive ping
      try:
        self._Atr1000WsSend(s, bytes([0xFF, 0xFD, 0x00]))
      except Exception:
        pass

  def atr1000_loop(self):
    while self.atr1000_polling_enabled:
      s = None
      try:
        s = self._Atr1000WsConnect()
        with self.atr1000_socket_lock:
          self.atr1000_socket = s
        self.atr1000_connected = True
        wx.CallAfter(self._UpdateAtr1000Label)
        self._Atr1000WsSend(s, bytes([0xFF, 0x01, 0x00]))  # SCMD_SYNC
        if self.atr1000_desired_memory_id is not None:
          self.Atr1000ApplyMemory(self.atr1000_desired_memory_id)
        while self.atr1000_polling_enabled:
          payload = self._Atr1000WsRecvFrame(s)
          if payload is None:
            break
          if payload == b"":
            continue
          self._Atr1000ParseFrame(s, payload)
      except Exception:
        pass
      finally:
        with self.atr1000_socket_lock:
          self.atr1000_socket = None
        if s:
          try:
            s.close()
          except Exception:
            pass
      self.atr1000_connected = False
      wx.CallAfter(self._UpdateAtr1000Label)
      if self.atr1000_polling_enabled:
        time.sleep(5.0)

  def _Atr1000SendFrame(self, data):
    with self.atr1000_socket_lock:
      s = self.atr1000_socket
      if s is None:
        return False
      try:
        self._Atr1000WsSend(s, data)
        return True
      except Exception as e:
        print("ATR-1000 send error: %s" % e)
        self.atr1000_socket = None
        return False

  def Atr1000ApplyMemory(self, memory_id):
    try:
      memory_id = int(memory_id)
    except Exception:
      return False
    if memory_id < 1 or memory_id > 255:
      return False
    self.atr1000_desired_memory_id = memory_id
    ok = self._Atr1000SendFrame(bytes([0xFF, 0x08, 0x01, memory_id]))
    if ok:
      self.atr1000_last_applied_memory_id = memory_id
    return ok

  def Atr1000FineTune(self):
    return self._Atr1000SendFrame(bytes([0xFF, 0x04, 0x01, 0x03]))

  def GetAtr1000Connected(self):
    return self.atr1000_connected

  def _FormatAtr1000Label(self):
    normal = self.conf.color_freq_txt
    green = wx.Colour(0, 200, 0)
    yellow = wx.Colour(220, 200, 0)
    if not self.atr1000_connected:
      return " ATR: --- ", normal
    parts = []
    memory_id = self.atr1000_last_applied_memory_id or self.atr1000_desired_memory_id
    if memory_id is not None:
      parts.append("M%d" % int(memory_id))
    if self.atr1000_swr is not None and self.atr1000_swr > 0:
      parts.append("SWR %.2f" % self.atr1000_swr)
    if self.atr1000_fwd is not None:
      parts.append("%dW" % self.atr1000_fwd)
    if self.atr1000_network is not None:
      parts.append(self.atr1000_network)
    if self.atr1000_inductance is not None:
      parts.append("%.1fuH" % self.atr1000_inductance)
    if self.atr1000_capacitance is not None:
      parts.append("%dpF" % self.atr1000_capacitance)
    if self.atr1000_tuned is not None:
      parts.append("Tuned" if self.atr1000_tuned else "Bypass")
    label = " ATR: " + "  ".join(parts) + " " if parts else " ATR: -- "
    color = green if self.atr1000_tuned else (yellow if self.atr1000_tuned is False else normal)
    return label, color

  def _ApplyAtr1000Label(self, label, color):
    if hasattr(self.application, 'atr1000_status'):
      self.application.atr1000_status.SetLabel(label)
      self.application.atr1000_status.TextCtrl.SetForegroundColour(color)
      self.application.atr1000_status.TextCtrl.Refresh()

  def _UpdateAtr1000Label(self):
    label, color = self._FormatAtr1000Label()
    self._ApplyAtr1000Label(label, color)

  def ChangeBand(self, band):
    BaseHardware.ChangeBand(self, band)
    self.current_band = band
    self._UpdateAmpLabel()
    self.send_band(band)
    self.check_tx_inhibit()
    pin = self.switchctl_pin if self.switchctl_pin is not None else 0
    try:
      pin = int(pin)
    except Exception:
      pin = 0
    pin_map = self.atr1000_band_memory_map.get(pin, {})
    memory_id = pin_map.get(str(band))
    if memory_id is not None:
      self.atr1000_desired_memory_id = int(memory_id)
      threading.Thread(target=self.Atr1000ApplyMemory, args=(memory_id,), daemon=True).start()
    if 'atu100' in self.station_equipment:
      self._ScheduleAtu100Tune(band)

  # --- esp32swr meter -------------------------------------------------------
  # REST API: /api/status (fwd, rev, vswr, version, uptime, rssi, heap).
  # The primary SWR feed is the MQTT topic esp32swr/raw; HTTP polling is the
  # atu100-style path that also surfaces the meter state independently of
  # the broker.

  def _Esp32SwrRequest(self, endpoint, params=None):
    hosts = []
    if self.esp32swr_active_host:
      hosts.append(self.esp32swr_active_host)
    if self.esp32swr_host and self.esp32swr_host not in hosts:
      hosts.append(self.esp32swr_host)
    for host in self.esp32swr_fallback_hosts:
      if host and host not in hosts:
        hosts.append(host)

    for host in hosts:
      url = "http://%s:%d%s" % (host, self.esp32swr_port, endpoint)
      if params:
        url += "?" + urllib.parse.urlencode(params)
      try:
        with urllib.request.urlopen(url, timeout=self.esp32swr_timeout) as response:
          payload = response.read().decode("utf-8", "ignore")
        self.esp32swr_connected = True
        self.esp32swr_active_host = host
        self.esp32swr_last_error = None
        try:
          return json.loads(payload)
        except Exception:
          return payload
      except Exception as e:
        self.esp32swr_last_error = "%s: %s" % (type(e).__name__, e)
        continue

    self.esp32swr_connected = False
    return None

  def GetEsp32SwrConnected(self):
    return self.esp32swr_connected

  def GetEsp32SwrStatus(self):
    return self.esp32swr_status

  def PollEsp32SwrStatus(self):
    response = self._Esp32SwrRequest("/api/status")
    if isinstance(response, dict):
      self.esp32swr_status = response
      try:
        fwd = float(response.get("fwd", "0"))
      except Exception:
        fwd = 0.0
      if fwd > 0.001:
        # Same semantics as the MQTT feed (esp32swr/raw): the reading only
        # counts as valid while there is measurable forward power, so the
        # label goes stale when the rig stops transmitting.
        self.swr_data = {
          "fwd": str(response.get("fwd", "0")),
          "rev": str(response.get("rev", "0")),
          "vswr": str(response.get("vswr", "1")),
        }
        self.last_swr_time = time.monotonic()
        wx.CallAfter(self._UpdateSwrLabel)
      return True
    return False

  def check_tx_inhibit(self):
    # The amplifier band guard protects the XPA125B only while it is online:
    # with the amp out of the chain (or its feed down) there is nothing to
    # protect, and inhibiting would block all transmit. When the amp
    # reconnects and reports a mismatched band, the guard re-arms.
    if not self.xpa125b_connected or self.amp_band == self.current_band:
      self.application.tx_inhibit = False
    else:
      self.application.tx_inhibit = True

  def HeartBeat(self):
    BaseHardware.HeartBeat(self)
    if self._IoBoardAvailable():
      if not self.ioboard_toggle_sent or self.ioboard_toggle_retry > 0:
        self.SetIoBoardToggle(self.ioboard_toggle_value)
    else:
      self.ioboard_toggle_sent = False
    if self.swr_data is not None and time.monotonic() - self.last_swr_time > self.swr_stale_seconds:
      self.swr_data = None
      self._UpdateSwrLabel()

  def _Xpa125bRequest(self, endpoint, params=None, preferred_hosts=None):
    hosts = []
    if preferred_hosts:
      for host in preferred_hosts:
        if host and host not in hosts:
          hosts.append(host)
    if self.xpa125b_active_host:
      hosts.append(self.xpa125b_active_host)
    if self.xpa125b_host and self.xpa125b_host not in hosts:
      hosts.append(self.xpa125b_host)
    for host in self.xpa125b_fallback_hosts:
      if host and host not in hosts:
        hosts.append(host)

    for host in hosts:
      url = "http://%s:%d%s" % (host, self.xpa125b_port, endpoint)
      if params:
        url += "?" + urllib.parse.urlencode(params)
      try:
        with urllib.request.urlopen(url, timeout=self.xpa125b_timeout) as response:
          payload = response.read().decode("utf-8", "ignore")
        self.xpa125b_connected = True
        self.xpa125b_active_host = host
        try:
          return json.loads(payload)
        except Exception:
          return payload
      except Exception:
        continue

    self.xpa125b_connected = False
    return None

  def GetXpa125bConnected(self):
    return self.xpa125b_connected

  def GetXpa125bRelayLabels(self):
    return tuple(item["label"] for item in self.xpa125b_relay_button_config)

  def GetXpa125bRelayButtonConfig(self):
    return tuple(dict(item) for item in self.xpa125b_relay_button_config)

  def GetXpa125bPressTime(self, relay_index, is_long=False):
    try:
      relay_index = int(relay_index)
    except Exception:
      return None
    if relay_index < 0 or relay_index >= len(self.xpa125b_relay_button_config):
      return None
    key = "long_press_ms" if is_long else "short_press_ms"
    return int(self.xpa125b_relay_button_config[relay_index][key])

  def _IsXpa125bPowerButton(self, relay_index):
    try:
      relay_index = int(relay_index)
    except Exception:
      return False
    if relay_index < 0 or relay_index >= len(self.xpa125b_relay_button_config):
      return False
    return bool(self.xpa125b_relay_button_config[relay_index].get("power_button", False))

  def _GetXpa125bRelayApiIndex(self, relay_index):
    try:
      relay_index = int(relay_index)
    except Exception:
      return None
    if relay_index < 0 or relay_index >= len(self.xpa125b_relay_button_config):
      return None
    try:
      return int(self.xpa125b_relay_button_config[relay_index].get("relay_index", relay_index))
    except Exception:
      return None

  def _UpdateXpa125bPowerStateFromMessage(self, message):
    text = str(message or "").strip().lower()
    if "power on" in text:
      self.xpa125b_power_state = True
    elif "power off" in text:
      self.xpa125b_power_state = False

  def PollXpa125bConfig(self):
    response = self._Xpa125bRequest("/config")
    if isinstance(response, dict):
      if "power_state" in response:
        self.xpa125b_power_state = bool(response.get("power_state"))
      return True
    return response is not None

  def PressXpa125bRelay(self, relay_index, is_long=False, press_time_ms=None):
    try:
      relay_index = int(relay_index)
    except Exception:
      return False
    if relay_index < 0 or relay_index >= len(self.xpa125b_relay_button_config):
      return False
    relay_api_index = self._GetXpa125bRelayApiIndex(relay_index)
    if relay_api_index is None:
      return False
    is_power_button = self._IsXpa125bPowerButton(relay_index)
    if press_time_ms is None:
      if is_power_button and self.xpa125b_power_state is not None:
        # Power timing is state-driven: OFF->ON uses short press, ON->OFF uses long press.
        press_time_ms = self.GetXpa125bPressTime(relay_index, is_long=self.xpa125b_power_state)
      else:
        press_time_ms = self.GetXpa125bPressTime(relay_index, is_long=is_long)
    try:
      press_time_ms = int(press_time_ms)
    except Exception:
      return False
    if press_time_ms <= 0:
      return False
    response = self._Xpa125bRequest("/pulseRelay", {
      "relay": relay_api_index,
      "time": press_time_ms,
    })
    if isinstance(response, dict):
      ok = bool(response.get("ok", False))
      if ok and is_power_button:
        self._UpdateXpa125bPowerStateFromMessage(response.get("message", ""))
        if self.xpa125b_power_state is None:
          self.xpa125b_power_state = not bool(self.xpa125b_power_state)
      return ok
    if response is not None and is_power_button:
      if self.xpa125b_power_state is None:
        self.xpa125b_power_state = True
      else:
        self.xpa125b_power_state = not self.xpa125b_power_state
    return response is not None

  def _Xpa125bSyncConfig(self, preferred_hosts=None):
    # Sync MQTT config to XPA125B controller on startup
    # Set MQTT config
    params = {
      "mqttserver": self.mqtt_host,
      "mqttuser": self.mqtt_user or "",
      "mqttpass": self.mqtt_pass or "",
      "mqtt": "enable"
    }
    self._Xpa125bRequest("/setmqttconfig", params, preferred_hosts=preferred_hosts)
    
    # Enable MQTT
    self._Xpa125bRequest("/setmqtt", {"mqtt": "enable"}, preferred_hosts=preferred_hosts)
    
    # Set mode to mqtt
    self._Xpa125bRequest("/setmode", {"mode": "mqtt"}, preferred_hosts=preferred_hosts)

  def _Xpa125bSyncConfigWorker(self):
    try:
      self._Xpa125bSyncConfig(preferred_hosts=self.xpa125b_fallback_hosts)
    except Exception:
      pass
    self.xpa125b_sync_in_progress = False
    self._UpdateAmpLabel()

  def _StartXpa125bSyncConfigAsync(self):
    if self.xpa125b_sync_in_progress:
      return
    self.xpa125b_sync_in_progress = True
    self._UpdateAmpLabel()
    thread = threading.Thread(target=self._Xpa125bSyncConfigWorker)
    thread.daemon = True
    thread.start()

  def close(self):
    self.control_polling_enabled = False
    self.xpa125b_polling_enabled = False
    self.atr1000_polling_enabled = False
    self.atu100_polling_enabled = False
    with self.atr1000_socket_lock:
      if self.atr1000_socket:
        try:
          self.atr1000_socket.close()
        except Exception:
          pass
        self.atr1000_socket = None
    if 'mqtt' in self.station_equipment:
      self.client.disconnect()
    BaseHardware.close(self)
