"""
PRISM v1.0 Energy Intelligence Engine
AppDaemon Python app for Home Assistant

Evolved from AXLE v3.7 (deployed May 2026).

Changelog v1.0:
  All AXLE v3.7 patches retained (see axle_v3.py for patch notes).

  PRISM v1.0 additions:
  - RENAME:   AxleV3Engine -> PrismEngine, axle_* -> prism_*, axle_memory.json -> prism_memory.json
  - ITEM 2:   sensor.prism_evening_min_soc_actual — rolling min SOC from 17:00 via run_every
  - ITEM 3:   _solar_forecast_today_remaining() vs _solar_forecast_tomorrow() — separate helpers
  - ITEM 4/5: Growatt bootstrap — derive correction factors from historicals; age-weighted decay
  - ITEM 6:   _solar_correction() — blended 0->100% over 10 local observations
  - ITEM 7:   Discharge efficiency 0.94 in _simulate()
  - ITEM 12:  Weighted decision bias — exponential decay weighting
  - ITEM 13:  Rolling 7-day MAE published as sensor.prism_mae_7d
  - ITEM 15:  Config validation on startup
  - ITEM 16:  Forecast freshness check in health check
  - ITEM 17:  Simulation output keys renamed pv_kw/load_kw -> pv_kwh/load_kwh
  - ITEM 18:  Export manager — PI controller with anti-windup (Ki term added)
  - ITEM 19:  sensor.prism_savings_today — estimated savings vs grid import
  - ITEM 20:  Dynamic charge_safety_buffer based on confidence (item 53)
  - ITEM 21:  Manual override events — force_charge / force_no_charge (item 54)
  - ITEM 22:  sensor.prism_confidence_trend — 7-day rolling accuracy (item 55)
  - ITEM 23:  _load_sensor_map() — all entity refs via self.s_* / self.e_*
  - ITEM 24:  apps.yaml sensor override map supported
"""

import appdaemon.plugins.hass.hassapi as hass
import json
import os
import copy
from datetime import datetime, timedelta


try:
    import growattServer
    GROWATT_AVAILABLE = True
except ImportError:
    GROWATT_AVAILABLE = False

# -------------------------------------------------------------
# MODULE-LEVEL DEFAULTS
# -------------------------------------------------------------
_DEFAULTS = {
    "battery_capacity_kwh": 13.0,
    "export_limit_kw": 4.5,
    "inverter_limit_kw": 5.0,
    "charge_power_kw": 5.0,
    "num_panels_se": 10,
    "num_panels_nw": 10,
    "panel_rating_w": 440,
    "cheap_rate_start": 2,
    "cheap_rate_end": 5,
    "soc_min_floor": 20,
    "charge_safety_buffer": 5,
    "safe_mode_soc": 85,
    "winter_solar_threshold": 15.0,
    "bms_balance_days": 7,
    "shift_cycle_ref": "2026-06-01",
    "shift_cycle_weeks": 3,
    "inverter_prefix": "aio_ch2344g372",
    "growatt_se_sensor": "sensor.wnkde2101d_energy_today_input_1",
    "growatt_nw_sensor": "sensor.wnkde2101d_energy_today_input_2",
    "growatt_total_sensor": "sensor.wnkde2101d_energy_today",
    "health_check_day": 6,
    "axle_pence_per_kwh": 100,
    # Octopus Flux unit rates (p/kWh) — used for savings estimate
    "rate_cheap_p": 7.5,
    "rate_peak_p": 37.0,
    "rate_offpeak_p": 20.0,
}

SHIFT_PATTERN = {
    0:"OFF", 1:"OFF", 2:"OFF", 3:"DAYS", 4:"DAYS", 5:"DAYS", 6:"SUNDAY_WORK",
    7:"LATES", 8:"LATES", 9:"OFF", 10:"OFF", 11:"OFF", 12:"OFF", 13:"OFF",
    14:"DAYS", 15:"DAYS", 16:"DAYS", 17:"LATES", 18:"LATES", 19:"LATES", 20:"OFF"
}

SHIFT_LOAD_BOOTSTRAP = {
    "OFF": 13.0, "DAYS": 8.5, "LATES": 9.5, "SUNDAY_WORK": 9.0, "BANK_HOLIDAY": 13.0
}

SHIFT_HOURLY_WEIGHTS = {
    "OFF":    {0:0.3,1:0.2,2:0.2,3:0.2,4:0.2,5:0.3,6:0.5,7:0.8,8:1.0,9:1.0,10:0.9,11:0.8,12:0.8,13:0.8,14:0.8,15:0.9,16:1.0,17:1.2,18:1.3,19:1.2,20:1.1,21:0.9,22:0.6,23:0.4},
    "DAYS":   {0:0.3,1:0.2,2:0.2,3:0.2,4:0.2,5:0.3,6:0.5,7:0.8,8:0.6,9:0.3,10:0.3,11:0.3,12:0.3,13:0.3,14:0.3,15:0.3,16:0.3,17:0.5,18:1.0,19:1.2,20:1.1,21:0.9,22:0.6,23:0.4},
    "LATES":  {0:0.3,1:0.2,2:0.2,3:0.2,4:0.2,5:0.3,6:0.5,7:0.8,8:1.0,9:1.0,10:0.9,11:0.8,12:0.5,13:0.3,14:0.3,15:0.3,16:0.3,17:0.3,18:0.3,19:0.3,20:0.5,21:0.8,22:0.6,23:0.4},
    "SUNDAY_WORK": {0:0.3,1:0.2,2:0.2,3:0.2,4:0.2,5:0.3,6:0.5,7:0.8,8:0.8,9:0.6,10:0.4,11:0.3,12:0.3,13:0.3,14:0.3,15:0.3,16:0.4,17:0.7,18:1.0,19:1.1,20:1.0,21:0.8,22:0.6,23:0.4},
    "BANK_HOLIDAY": {0:0.3,1:0.2,2:0.2,3:0.2,4:0.2,5:0.3,6:0.5,7:0.8,8:1.0,9:1.0,10:0.9,11:0.8,12:0.8,13:0.8,14:0.8,15:0.9,16:1.0,17:1.2,18:1.3,19:1.2,20:1.1,21:0.9,22:0.6,23:0.4},
}

NW_SEASONAL_WEIGHT = {
    1:0.3, 2:0.35, 3:0.45, 4:0.55, 5:0.65, 6:0.7,
    7:0.65, 8:0.55, 9:0.45, 10:0.35, 11:0.3, 12:0.25
}

SE_HOUR = {
    6:0.1, 7:0.3, 8:0.6, 9:0.85, 10:1.0, 11:1.05,
    12:1.0, 13:0.9, 14:0.75, 15:0.55, 16:0.35, 17:0.15, 18:0.05
}

NW_HOUR = {
    6:0.05, 7:0.1, 8:0.2, 9:0.35, 10:0.55, 11:0.75,
    12:0.9, 13:1.0, 14:1.05, 15:1.0, 16:0.85, 17:0.65, 18:0.4, 19:0.15
}

WINTER_MONTHS = {11, 12, 1, 2, 3}

WEATHER_CLASSES = ["CLEAR", "PARTLY_CLOUDY", "OVERCAST", "RAINY", "STORMY"]

MONTHLY_SOLAR_PRIOR = {
    1: 4.0, 2: 6.5, 3: 11.0, 4: 17.0, 5: 20.0, 6: 22.0,
    7: 21.0, 8: 18.0, 9: 13.0, 10: 8.0, 11: 4.5, 12: 3.5
}

MEMORY_FILE = "/homeassistant/prism_memory.json"
WEATHER_CORRECTION_MIN_SAMPLES = 6

# Octopus Flux peak hours — used for savings estimate
PEAK_HOURS = {16, 17, 18}
CHEAP_HOURS = {2, 3, 4}


class PrismEngine(hass.Hass):

    def initialize(self):
        self.log("=" * 60)
        self.log("PRISM v1.0 Energy Intelligence Engine starting")
        self.log("=" * 60)

        self._load_config()
        # ITEM 15: Config validation on startup
        self._validate_config()
        self._load_sensor_map()  # ITEM 23: Inverter abstraction
        self.memory = self._load_memory()

        obs  = len(self.memory.get("observations", []))
        hist = len(self.memory.get("decision_history", []))
        self.log(f"Memory: {obs} observations | {hist} decision history records")

        # Restore cheap rate window from memory — survives Octopus downtime
        mem_start = self.memory.get("cheap_rate_start", 0)
        mem_end   = self.memory.get("cheap_rate_end", 0)
        if mem_start and mem_end:
            self.cheap_rate_start = mem_start
            self.cheap_rate_end   = mem_end

        self._restore_live_state()
        self._recover_export_state()

        self.run_daily(self.overnight_charge_decision, "01:30:00")
        self.run_daily(self.record_daily_observation,  "23:50:00")
        self.run_daily(self.self_validate,             "23:55:00")
        self.run_daily(self.midday_recalculation,      "08:00:00")
        self.run_daily(self.midday_recalculation,      "12:00:00")
        self.run_daily(self.midday_recalculation,      "16:00:00")

        self.run_every(self._bms_soc_monitor,          "now+60",  15 * 60)
        # ITEM 2: Track rolling evening min SOC from 17:00
        self.run_every(self._track_evening_min_soc,    "now+120",  5 * 60)
        # ITEM 19: Update savings estimate hourly
        self.run_every(self._update_savings_today,     "now+180", 60 * 60)

        self.run_in(self._run_health_check, 45, is_startup=True)
        self.run_daily(self._run_health_check, "08:05:00")

        self.run_in(self.startup_check,            30)
        self.run_in(self.publish_simulation_curve, 35)
        self.run_every(self.cheap_rate_watchdog,   "now+60", 30 * 60)
        self.run_every(self.export_soc_watchdog,   "now+60",  2 * 60)
        self.run_in(self.attempt_growatt_bootstrap, 60)

        # Auto-detect cheap rate window — retry every 15 mins in case Octopus sensors
        # take longer than 120s to populate after HA restart
        self.run_in(lambda k: self._detect_cheap_rate_window(), 120)
        self.run_every(self._detect_cheap_rate_window, "now+900", 15 * 60)

        # Axle Energy VPP — reads from sensor.axle_vpp_event (populated by HA REST sensor)
        self.run_in(self._poll_axle_api, 90)
        self.run_every(self._poll_axle_api, "now+600", 10 * 60)
        self.log(f"Axle Energy VPP: polling enabled ({self.axle_pence_per_kwh}p/kWh)")

        # Listen for manual trigger events from HA scripts
        self.listen_event(self._handle_trigger_event, "prism_trigger")

        self.log(f"Config loaded: inv={self.inv} | health_day={self.health_check_day} | "
                 f"charge_kw={self.charge_power_kw} | Growatt SE={self.growatt_se} | "
                 f"GROWATT_AVAILABLE={GROWATT_AVAILABLE}")

    # -- CONFIG LOADING ----------------------------------------

    def _load_config(self):
        def g(key, cast=float, default=None):
            if default is None:
                default = _DEFAULTS.get(key)
            val = self.args.get(key, default)
            try:
                return cast(val) if val is not None else default
            except (ValueError, TypeError):
                self.log(f"Config warning: could not cast {key}={val}, using default",
                         level="WARNING")
                return default

        self.battery_capacity_kwh   = g("battery_capacity_kwh")
        self.export_limit_kw        = g("export_limit_kw")
        self.inverter_limit_kw      = g("inverter_limit_kw")
        self.charge_power_kw        = g("charge_power_kw")
        self.num_panels_se          = g("num_panels_se", int)
        self.num_panels_nw          = g("num_panels_nw", int)
        self.panel_rating_w         = g("panel_rating_w", int)
        self.cheap_rate_start       = g("cheap_rate_start", int)
        self.cheap_rate_end         = g("cheap_rate_end", int)
        self.soc_min_floor          = g("soc_min_floor", int)
        self.charge_safety_buffer   = g("charge_safety_buffer", int)
        self.safe_mode_soc          = g("safe_mode_soc", int)
        self.winter_solar_threshold = g("winter_solar_threshold")
        self.bms_balance_days       = g("bms_balance_days", int)
        self.shift_cycle_ref        = self.args.get("shift_cycle_ref",
                                                    _DEFAULTS["shift_cycle_ref"])
        self.shift_cycle_weeks      = g("shift_cycle_weeks", int)
        self.inv                    = self.args.get("inverter_prefix",
                                                    _DEFAULTS["inverter_prefix"])
        self.health_check_day       = g("health_check_day", int)
        self.rate_cheap_p           = g("rate_cheap_p")
        self.rate_peak_p            = g("rate_peak_p")
        self.rate_offpeak_p         = g("rate_offpeak_p")

        self.growatt_se    = (self.args.get("growatt_se_sensor")
                              or _DEFAULTS["growatt_se_sensor"])
        self.growatt_nw    = (self.args.get("growatt_nw_sensor")
                              or _DEFAULTS["growatt_nw_sensor"])
        self.growatt_total = (self.args.get("growatt_total_sensor")
                              or _DEFAULTS["growatt_total_sensor"])

        # Axle Energy VPP
        self.axle_pence_per_kwh = g("axle_pence_per_kwh", int)

    # ITEM 15: Config validation on startup
    def _validate_config(self):
        errors = []
        if self.battery_capacity_kwh <= 0:
            errors.append(f"battery_capacity_kwh={self.battery_capacity_kwh} must be > 0")
        if not (0 < self.charge_power_kw <= self.inverter_limit_kw):
            errors.append(f"charge_power_kw={self.charge_power_kw} outside (0, inverter_limit_kw={self.inverter_limit_kw}]")
        if not (0 <= self.cheap_rate_start < self.cheap_rate_end <= 24):
            errors.append(f"cheap_rate window invalid: {self.cheap_rate_start}-{self.cheap_rate_end}")
        if not (0 <= self.soc_min_floor <= 40):
            errors.append(f"soc_min_floor={self.soc_min_floor} outside [0, 40]")
        if self.bms_balance_days < 1:
            errors.append(f"bms_balance_days={self.bms_balance_days} must be >= 1")
        try:
            datetime.strptime(self.shift_cycle_ref, "%Y-%m-%d")
        except ValueError:
            errors.append(f"shift_cycle_ref={self.shift_cycle_ref} not in YYYY-MM-DD format")

        if errors:
            for e in errors:
                self.log(f"CONFIG ERROR: {e}", level="ERROR")
            self.log("PRISM starting with config errors — check apps.yaml", level="ERROR")
        else:
            self.log("Config validation passed")

    # ITEM 23: Inverter abstraction — all entity refs via self.s_* / self.e_*
    def _load_sensor_map(self):
        """
        Build self.s_* (read sensors) and self.e_* (control entities) from
        inverter_prefix, with per-entity overrides from apps.yaml.
        Leave override blank in apps.yaml to use the prefix-based default.
        """
        p = self.inv

        def _s(key, default_suffix):
            override = self.args.get(key, "")
            return override if override else f"sensor.{p}_{default_suffix}"

        def _e(key, default_entity):
            override = self.args.get(key, "")
            return override if override else default_entity

        # Read sensors
        self.s_soc             = _s("sensor_soc",             "soc")
        self.s_battery_power   = _s("sensor_battery_power",   "battery_power")
        self.s_pv_power        = _s("sensor_pv_power",        "pv_power")
        self.s_load_power      = _s("sensor_load_power",      "load_power")
        self.s_grid_power      = _s("sensor_grid_power",      "grid_power")
        self.s_export_power    = _s("sensor_export_power",    "export_power")
        self.s_import_power    = _s("sensor_import_power",    "import_power")
        self.s_load_energy     = _s("sensor_load_energy",     "load_energy_today_kwh")
        self.s_pv_energy       = _s("sensor_pv_energy",       "pv_energy_today_kwh")

        # Control entities
        self.e_mode            = _e("control_mode",            f"select.{p}_mode")
        self.e_charge_switch   = _e("control_charge_switch",   f"switch.{p}_enable_charge_schedule")
        self.e_charge_start    = _e("control_charge_start",    f"select.{p}_charge_start_time_slot_1")
        self.e_charge_end      = _e("control_charge_end",      f"select.{p}_charge_end_time_slot_1")
        self.e_charge_target   = _e("control_charge_target",   f"number.{p}_charge_target_soc_1")
        self.e_charge_rate     = _e("control_charge_rate",     f"number.{p}_battery_charge_rate")
        self.e_discharge_rate  = _e("control_discharge_rate",  f"number.{p}_battery_discharge_rate")

        self.log(f"Sensor map: SOC={self.s_soc} mode={self.e_mode} "
                 f"charge_switch={self.e_charge_switch}")

    # -- RESTART RESILIENCE ------------------------------------

    def _restore_live_state(self):
        live = self.memory.get("live_state", {})
        if not live:
            self.log("No live state to restore - fresh start")
            return
        self.log(f"Live state restored: strategy={live.get('current_strategy')} "
                 f"last_recalc={live.get('last_recalc')}")

    def _persist_live_state(self, strategy, sim):
        self.memory["live_state"] = {
            "current_strategy": strategy,
            "last_recalc":      datetime.now().isoformat(),
            "today_simulation": sim
        }

    def _recover_export_state(self):
        try:
            mode         = self._get_state(self.e_mode, "Eco")
            bat_power    = self._f(self.s_battery_power, 0)
            export_power = self._f(self.s_export_power, 0)

            if (mode in ["Timed Export", "Timed Demand"] and
                    bat_power > 200 and export_power > 100):
                self.log("Export state recovery: inverter in export mode on startup - "
                         "resuming export management", level="WARNING")
                self.memory["export_managing"]   = True
                self.memory["export_correction"] = 0
            else:
                if self.memory.get("export_managing", False):
                    self.log("Export state recovery: clearing stale export_managing flag")
                    self.memory["export_managing"]   = False
                    self.memory["export_correction"] = 0
        except Exception as e:
            self.log(f"Export state recovery error: {e}", level="WARNING")

    # -- WEATHER CLASSIFICATION --------------------------------

    def _classify_weather(self, for_tomorrow=False):
        codes   = self.get_state("sensor.solar_weather_raw", attribute="weathercode")
        precip  = self.get_state("sensor.solar_weather_raw",
                                  attribute="precipitation_probability")
        clouds  = self.get_state("sensor.solar_weather_raw", attribute="cloud_cover")
        rain    = self.get_state("sensor.solar_weather_raw", attribute="precipitation")

        if not codes:
            return "UNKNOWN"

        offset     = 24 if for_tomorrow else 0
        day_codes  = [codes[offset + h]  for h in range(7, 20) if offset + h < len(codes)]
        day_precip = [precip[offset + h] for h in range(7, 20)
                      if precip and offset + h < len(precip)]
        day_cloud  = [clouds[offset + h] for h in range(7, 20)
                      if clouds and offset + h < len(clouds)]
        day_rain   = [rain[offset + h]   for h in range(7, 20)
                      if rain and offset + h < len(rain)]

        if not day_codes:
            return "UNKNOWN"

        max_code      = max(day_codes)
        avg_cloud     = sum(day_cloud)  / len(day_cloud)  if day_cloud  else 50
        max_precip    = max(day_precip) if day_precip else 0
        total_rain_mm = sum(day_rain)   if day_rain   else 0

        if max_code >= 80 or max_precip > 70:
            return "STORMY"
        if (max_code >= 51 or max_precip > 40) and total_rain_mm >= 1.0:
            return "RAINY"
        if avg_cloud >= 75:
            return "OVERCAST"
        if avg_cloud >= 35 or max_code >= 2:
            return "PARTLY_CLOUDY"
        return "CLEAR"

    def _apply_weather_correction(self, forecast, month, weather_class):
        monthly = self._solar_correction(month)
        wx_corrections = self.memory.get("solar_corrections_by_weather", {})
        wx_samples     = self.memory.get("solar_corrections_weather_samples", {})
        month_wx       = wx_corrections.get(str(month), {})
        month_samples  = wx_samples.get(str(month), {})

        if (weather_class != "UNKNOWN" and
                month_samples.get(weather_class, 0) >= WEATHER_CORRECTION_MIN_SAMPLES):
            correction = month_wx.get(weather_class, monthly)
            if correction != monthly:
                self.log(f"Weather correction ({weather_class}) REPLACES monthly: "
                         f"{monthly:.3f} -> {correction:.3f} "
                         f"(n={month_samples[weather_class]})")
        else:
            correction = monthly
            if weather_class != "UNKNOWN":
                self.log(f"Weather correction ({weather_class}): insufficient samples "
                         f"({month_samples.get(weather_class, 0)}/{WEATHER_CORRECTION_MIN_SAMPLES}) "
                         f"- using monthly {monthly:.3f}")

        return {
            "p10": round(forecast["p10"] * correction, 2),
            "p50": round(forecast["p50"] * correction, 2),
            "p90": round(forecast["p90"] * correction, 2),
        }, correction

    # -- CONFIDENCE & SOC FLOOR --------------------------------

    def _calculate_confidence(self, p10, p50, p90):
        """Calculate confidence from spread/estimate ratio."""
        if p50 <= 0.1:
            return 0.3
        spread = p90 - p10
        spread_ratio = spread / p50
        return round(max(0.1, min(1.0, 1.0 - spread_ratio * 0.6)), 3)

    def _soc_floor_from_confidence(self, confidence):
        """Convert confidence score to SOC floor adjustment."""
        if confidence < 0.3:
            return self.soc_min_floor + 10
        if confidence < 0.6:
            return self.soc_min_floor + 5
        return self.soc_min_floor

    # ITEM 20: Dynamic charge_safety_buffer based on confidence
    def _dynamic_charge_buffer(self, confidence):
        """
        Item 53: buffer = 5 + (10 * (1 - confidence)) + max(0, error_trend * 2)
        error_trend is the mean of recent signed errors (positive = we over-predicted solar).
        """
        history   = self.memory.get("decision_history", [])
        completed = [h for h in history if "min_soc_actual" in h]
        error_trend = 0.0
        if len(completed) >= 3:
            recent = completed[-5:]
            errors = [h["min_soc_actual"] - h.get("min_soc_predicted_evening",
                       h.get("min_soc_predicted", 50)) for h in recent]
            # Positive error = we were conservative (actual > predicted) — reduce buffer
            # Negative error = we over-predicted — increase buffer
            error_trend = -(sum(errors) / len(errors))  # invert: negative error -> positive trend

        buffer = 5 + (10 * (1 - confidence)) + max(0, error_trend * 2)
        buffer = round(max(2.0, min(15.0, buffer)), 1)
        self.log(f"Dynamic buffer: confidence={confidence:.2f} error_trend={error_trend:.1f} "
                 f"-> buffer={buffer:.1f}%")
        return buffer

    # -- DECISION BIAS ENGINE ----------------------------------

    # ITEM 12: Exponential decay weighting in bias calculation
    def _decision_bias(self):
        history   = self.memory.get("decision_history", [])
        completed = [h for h in history if "min_soc_actual" in h]
        if len(completed) < 3:
            self.log("Decision bias: insufficient history - no adjustment")
            return 0.0

        recent = completed[-5:]
        # Exponential decay: most recent gets highest weight
        weights = [2 ** i for i in range(len(recent))]  # [1, 2, 4, 8, 16] for 5 records
        errors  = [h["min_soc_actual"] - h.get("min_soc_predicted_evening",
                   h.get("min_soc_predicted", 50)) for h in recent]
        total_w   = sum(weights)
        avg_error = sum(e * w for e, w in zip(errors, weights)) / total_w

        month = datetime.now().month
        correction_deviation = abs(self._solar_correction(month) - 1.0)
        bias_cap = max(2.0, 5.0 - (correction_deviation * 10))

        bias = round(max(-bias_cap, min(bias_cap, avg_error * 0.5)), 1)
        self.log(f"Decision bias: avg_error={avg_error:.1f}% (exp-weighted) -> bias={bias:+.1f}% "
                 f"cap={bias_cap:.1f}% (correction_dev={correction_deviation:.2f})")
        return bias

    # -- HEALTH CHECK ------------------------------------------

    def _run_health_check(self, kwargs=None):
        # is_startup=True when called via run_in(45s) — skip slow/not-yet-ready checks
        is_startup = (kwargs or {}).get("is_startup", False)
        self.log(f"PRISM: Running Health Check{'  (startup)' if is_startup else ''}...")
        faults        = []
        checks_passed = 0
        checks_total  = 0

        def _chk(entity, name, min_val=None, max_val=None):
            raw = self.get_state(entity)
            if raw in (None, "unknown", "unavailable", ""):
                faults.append(f"{name} unavailable")
                return False
            try:
                val = float(raw)
            except (ValueError, TypeError):
                faults.append(f"{name} invalid value: {raw}")
                return False
            if min_val is not None and val < min_val:
                faults.append(f"{name} too low: {val}")
                return False
            if max_val is not None and val > max_val:
                faults.append(f"{name} too high: {val}")
                return False
            return True

        def chk(entity, name, min_val=None, max_val=None):
            nonlocal checks_passed, checks_total
            checks_total += 1
            if _chk(entity, name, min_val, max_val):
                checks_passed += 1
                return True
            return False

        chk(self.s_soc,          "GivEnergy SOC",          0, 100)
        chk(self.s_pv_power,     "GivEnergy PV Power",     0)
        chk(self.s_load_power,   "GivEnergy Load Power",   0)
        chk(self.s_battery_power,"GivEnergy Battery Power")
        chk("sensor.solcast_pv_forecast_forecast_today", "Solcast Today",  0)
        chk(self.growatt_se,    "Growatt SE",    min_val=0)
        chk(self.growatt_nw,    "Growatt NW",    min_val=0)
        chk(self.growatt_total, "Growatt Total", min_val=0)

        # Charge decision freshness — skipped on startup (fresh memory has no decision yet)
        if not is_startup:
            checks_total += 1
            last = self.memory.get("last_charge_decision_detail", {})
            if last and last.get("date"):
                try:
                    days_old = (datetime.now() -
                                datetime.strptime(last["date"], "%Y-%m-%d")).days
                    if days_old > 1:
                        faults.append(f"Charge decision is {days_old} days old")
                    else:
                        checks_passed += 1
                except Exception:
                    faults.append("Charge decision date invalid")
            else:
                faults.append("No recent charge decision")

        # ITEM 16: Forecast freshness check — skipped on startup (Solcast may not have polled yet)
        if not is_startup:
            checks_total += 1
            solcast_state = self.get_state("sensor.solcast_pv_forecast_forecast_tomorrow")
            if solcast_state in (None, "unknown", "unavailable"):
                faults.append("Solcast tomorrow forecast unavailable")
            else:
                detailed = self.get_state("sensor.solcast_pv_forecast_forecast_tomorrow",
                                          attribute="detailedForecast")
                if not detailed or not isinstance(detailed, list) or len(detailed) < 24:
                    faults.append("Solcast detailedForecast missing or sparse")
                else:
                    checks_passed += 1

        checks_total += 1
        if isinstance(self.memory, dict) and "observations" in self.memory:
            checks_passed += 1
        else:
            faults.append("Memory file corrupted")

        status    = "FAULT" if faults else "OK"
        timestamp = datetime.now().isoformat()

        self.set_state("sensor.prism_health_status", state=status, attributes={
            "faults":        faults,
            "last_check":    timestamp,
            "checks_passed": checks_passed,
            "checks_failed": checks_total - checks_passed,
            "checks_total":  checks_total,
            "friendly_name": "PRISM Health Status"
        })
        self.log(f"Health Check completed: {status} ({checks_passed}/{checks_total})")

        today_str = datetime.now().strftime("%Y-%m-%d")

        if status == "FAULT":
            last_fault_date = self.memory.get("last_health_fault_date", "")
            if last_fault_date != today_str:
                msg = "PRISM Health Check failed:\n- " + "\n- ".join(faults)
                self.call_service("notify/notify",
                                  title="PRISM HEALTH FAULT", message=msg)
                self.memory["last_health_fault_date"] = today_str
                self._save()
        elif datetime.now().weekday() == self.health_check_day:
            last_ok_date = self.memory.get("last_health_notification_date", "")
            if last_ok_date != today_str:
                self.call_service("notify/notify",
                    title="PRISM System Healthy",
                    message=(f"All {checks_passed} checks passed.\n"
                             f"Last check: {datetime.now().strftime('%Y-%m-%d %H:%M')}"))
                self.memory["last_health_notification_date"] = today_str
                self._save()

    # -- STARTUP -----------------------------------------------

    def startup_check(self, kwargs):
        soc         = self._f(self.s_soc, 50)
        pv          = self._f(self.s_pv_power, 0)
        load        = self._f(self.s_load_power, 0)
        shift_today = self._get_shift_type(datetime.now())
        shift_tmrw  = self._get_shift_type(datetime.now() + timedelta(days=1))
        days_100    = self._days_since_full_charge()
        correction  = self._solar_correction(datetime.now().month)
        weather     = self._classify_weather(for_tomorrow=False)

        self.log(f"Status: SOC={soc}% PV={pv}W Load={load}W")
        self.log(f"Shift today={shift_today} tomorrow={shift_tmrw}")
        self.log(f"Days since 100%={days_100} Solar correction={correction:.3f} "
                 f"Weather={weather}")

        self.set_state("sensor.prism_status", state="RUNNING", attributes={
            "observations":           len(self.memory.get("observations", [])),
            "decision_history_count": len(self.memory.get("decision_history", [])),
            "accuracy_score":         self.memory.get("accuracy_score", 0),
            "last_decision":          self.memory.get("last_charge_decision", "never"),
            "last_observation":       self.memory.get("last_observation_date", "never"),
            "shift_today":            shift_today,
            "shift_tomorrow":         shift_tmrw,
            "days_since_full_charge": days_100,
            "weather_today":          weather,
            "friendly_name":          "PRISM Status"
        })
        self.set_state("sensor.prism_shift_today", state=shift_today, attributes={
            "shift_tomorrow": shift_tmrw,
            "cycle_position": self._get_cycle_position(datetime.now()),
            "shift_week":     self._get_shift_week(datetime.now()),
            "friendly_name":  "PRISM Shift Today"
        })
        self._publish_observability_sensors()
        self._axle_publish_sensor()

    def _publish_observability_sensors(self):
        month      = datetime.now().month
        correction = self._solar_correction(month)
        self.set_state("sensor.prism_solar_correction_factor",
            state=str(round(correction, 3)),
            attributes={"month": month, "unit_of_measurement": "",
                        "friendly_name": "PRISM Solar Correction Factor"})
        detail = self.memory.get("last_charge_decision_detail", {})
        if detail:
            solar   = detail.get("solar_forecast", 0)
            load    = detail.get("load_forecast", 0)
            deficit = max(0, round(load - solar, 2))
            min_soc = detail.get("min_soc_predicted_evening", 0)
        else:
            solar = load = deficit = min_soc = 0

        self.set_state("sensor.prism_energy_deficit_kwh", state=str(deficit),
            attributes={"unit_of_measurement": "kWh",
                        "friendly_name": "PRISM Energy Deficit Tomorrow"})
        self.set_state("sensor.prism_predicted_min_soc_tomorrow", state=str(min_soc),
            attributes={"unit_of_measurement": "%",
                        "friendly_name": "PRISM Predicted Min SOC Tomorrow"})

        # ITEM 13: Publish 7-day MAE
        self._publish_mae_sensor()
        # ITEM 22: Publish confidence trend
        self._publish_confidence_trend()

    # ITEM 13: Rolling 7-day MAE sensor
    def _publish_mae_sensor(self):
        history   = self.memory.get("decision_history", [])
        completed = [h for h in history if "min_soc_actual" in h]
        recent    = completed[-7:]
        if not recent:
            mae = 0.0
        else:
            errors = [abs(h["min_soc_actual"] - h.get("min_soc_predicted_evening",
                          h.get("min_soc_predicted", 50))) for h in recent]
            mae = round(sum(errors) / len(errors), 2)
        self.set_state("sensor.prism_mae_7d", state=str(mae),
            attributes={"unit_of_measurement": "%",
                        "samples": len(recent),
                        "friendly_name": "PRISM 7-Day MAE"})

    # ITEM 22: sensor.prism_confidence_trend — 7-day rolling accuracy
    def _publish_confidence_trend(self):
        history   = self.memory.get("decision_history", [])
        completed = [h for h in history if "accuracy" in h]
        recent    = completed[-7:]
        if not recent:
            trend = 0.0
        else:
            trend = round(sum(h["accuracy"] for h in recent) / len(recent), 1)
        self.set_state("sensor.prism_confidence_trend", state=str(trend),
            attributes={"unit_of_measurement": "%",
                        "samples": len(recent),
                        "friendly_name": "PRISM 7-Day Confidence Trend"})

    # ITEM 2: Track rolling evening min SOC from 17:00
    def _track_evening_min_soc(self, kwargs=None):
        h = datetime.now().hour
        if h < 17:
            return
        soc = self._f(self.s_soc, 0)
        # Retrieve current tracked min (reset each day at 17:00)
        current = self.memory.get("live_state", {}).get("evening_min_soc", 100.0)
        date    = self.memory.get("live_state", {}).get("evening_min_date", "")
        today   = datetime.now().strftime("%Y-%m-%d")
        if date != today:
            current = soc  # reset at start of evening window each day
        new_min = min(current, soc)
        ls = self.memory.get("live_state", {})
        ls["evening_min_soc"]  = new_min
        ls["evening_min_date"] = today
        self.memory["live_state"] = ls
        self.set_state("sensor.prism_evening_min_soc_actual", state=str(round(new_min, 1)),
            attributes={"unit_of_measurement": "%",
                        "date": today,
                        "friendly_name": "PRISM Evening Min SOC Actual"})

    # ITEM 19: Savings estimate sensor
    def _update_savings_today(self, kwargs=None):
        """
        Estimate today's savings vs full grid import.
        Savings = (solar_used_locally * avoided_rate) + (cheap_rate_import_saving vs peak_rate).
        Uses Growatt energy today as solar source.
        """
        try:
            pv_today   = self._f(self.growatt_total, 0)
            load_today = self._f(self.s_load_energy, 0)
            soc        = self._f(self.s_soc, 50)
            # Rough solar self-consumption = min(pv, load)
            self_used = round(min(pv_today, load_today), 2)
            # Saving vs buying at off-peak rate
            avoided_import_saving = round(self_used * self.rate_offpeak_p / 100, 2)
            # Cheap rate charge estimate (battery capacity * SOC charged / 100)
            # If battery is above 40% and we imported at cheap rate overnight:
            cheap_kwh    = round(max(0, (soc - 20) / 100 * self.battery_capacity_kwh), 2)
            cheap_saving = round(cheap_kwh * (self.rate_peak_p - self.rate_cheap_p) / 100, 2)
            total_saving = round(avoided_import_saving + cheap_saving, 2)

            self.set_state("sensor.prism_savings_today", state=str(total_saving),
                attributes={
                    "unit_of_measurement":   "GBP",
                    "solar_self_used_kwh":   self_used,
                    "avoided_import_saving": avoided_import_saving,
                    "cheap_rate_saving":     cheap_saving,
                    "friendly_name":         "PRISM Estimated Savings Today"
                })
        except Exception as e:
            self.log(f"Savings update error: {e}", level="WARNING")

    # -- SHIFT CYCLE -------------------------------------------

    def _get_cycle_position(self, dt):
        ref = datetime.strptime(self.shift_cycle_ref, "%Y-%m-%d")
        return (dt.date() - ref.date()).days % 21

    def _get_shift_week(self, dt):
        return (self._get_cycle_position(dt) // 7) + 1

    def _get_shift_type(self, dt):
        today = datetime.now().date()

        if dt.date() == today:
            status = self.get_state("binary_sensor.workday")
            if status in (None, "unknown", "unavailable"):
                if dt.weekday() < 5:
                    self.log("workday unavailable - assuming BANK_HOLIDAY",
                             level="WARNING")
                    return "BANK_HOLIDAY"
            elif status == "off" and dt.weekday() < 5:
                return "BANK_HOLIDAY"

        elif dt.date() == today + timedelta(days=1):
            status = self.get_state("binary_sensor.workday_tomorrow")
            if status in (None, "unknown", "unavailable"):
                if dt.weekday() < 5:
                    self.log("workday_tomorrow unavailable - assuming BANK_HOLIDAY",
                             level="WARNING")
                    return "BANK_HOLIDAY"
            elif status == "off" and dt.weekday() < 5:
                return "BANK_HOLIDAY"

        pos = self._get_cycle_position(dt)
        return SHIFT_PATTERN.get(pos, "OFF")

    def _days_since_full_charge(self):
        last = self.memory.get("last_full_charge_date")
        if not last:
            return 999
        try:
            return (datetime.now() -
                    datetime.strptime(last, "%Y-%m-%d")).days
        except Exception:
            return 999

    # ITEM 6: Blended solar correction — 0->100% over 10 local observations
    def _solar_correction(self, month):
        """
        Return blended correction factor:
        - < 10 observations for this month: blend memory correction with 1.0 (neutral prior)
        - >= 10 observations: return memory correction as-is
        Also applies age-decay toward 1.0 using growatt_monthly_totals if populated.
        """
        raw = self.memory.get("solar_corrections", {}).get(str(month), 1.0)
        month_obs = [o for o in self.memory.get("observations", [])
                     if datetime.strptime(o["date"], "%Y-%m-%d").month == month]
        n = len(month_obs)
        if n >= 10:
            return raw
        # Blend: 0 obs -> 1.0 (neutral), 10 obs -> raw
        blend = n / 10.0
        return round(raw * blend + 1.0 * (1 - blend), 3)

    def _predicted_load(self, shift_type):
        loads = self.memory.get("daily_loads", {}).get(shift_type, [])
        if len(loads) >= 3:
            weights = list(range(1, len(loads) + 1))
            return round(
                sum(l * w for l, w in zip(loads, weights)) / sum(weights), 2
            )
        return SHIFT_LOAD_BOOTSTRAP.get(shift_type, 10.0)

    # -- SAFE MODE ---------------------------------------------

    def _check_safe_mode(self):
        critical = [self.s_soc, self.s_load_power]
        unavailable = [s for s in critical
                       if self.get_state(s) in
                       (None, "unknown", "unavailable", "")]
        if unavailable:
            self.log(f"SAFE MODE: unavailable sensors: {unavailable}",
                     level="WARNING")
            return True
        if not isinstance(self.memory, dict):
            self.log("SAFE MODE: memory corrupted", level="WARNING")
            return True
        return False

    # -- SOLAR FORECAST (PROBABILISTIC) -----------------------

    # ITEM 3: Separate today_remaining vs tomorrow forecast helpers
    def _solar_forecast_tomorrow(self):
        """
        Returns probabilistic solar forecast for tomorrow (the day we're charging for).
        Primary: Solcast detailedForecast on forecast_tomorrow.
        Falls back to Solcast daily total, then Open-Meteo physics model.
        """
        solcast_hourly = self.get_state(
            "sensor.solcast_pv_forecast_forecast_tomorrow",
            attribute="detailedForecast")

        if solcast_hourly and isinstance(solcast_hourly, list) and len(solcast_hourly) > 0:
            try:
                p10_hours = []
                p50_hours = []
                p90_hours = []

                # Sum all available intervals — no coverage scaling
                # Dividing by coverage (< 1.0) would inflate a partial forecast incorrectly
                # If Solcast has fewer than 48 intervals, just use what's there
                num_intervals = len(solcast_hourly)
                coverage = min(1.0, num_intervals / 48.0)

                for h in solcast_hourly:
                    p10_hours.append(float(h.get("pv_estimate10", 0)) / 2)
                    p50_hours.append(float(h.get("pv_estimate",   0)) / 2)
                    p90_hours.append(float(h.get("pv_estimate90", 0)) / 2)

                p10 = sum(p10_hours)
                p50 = sum(p50_hours)
                p90 = sum(p90_hours)

                confidence = self._calculate_confidence(p10, p50, p90)

                self.log(f"Solar forecast tomorrow Solcast hourly: p10={p10:.1f} p50={p50:.1f} "
                         f"p90={p90:.1f} conf={confidence:.2f} kWh "
                         f"intervals={num_intervals} coverage={coverage:.2f}")

                # Blend with seasonal prior if <5 observations this month
                month = datetime.now().month
                month_obs = [o for o in self.memory.get("observations", [])
                             if datetime.strptime(o["date"], "%Y-%m-%d").month == month]
                if len(month_obs) < 5:
                    prior = MONTHLY_SOLAR_PRIOR.get(month, p50)
                    blend = len(month_obs) / 5.0
                    p50_blended = round(p50 * blend + prior * (1 - blend), 2)
                    self.log(f"Seasonal prior blend: obs={len(month_obs)}/5 "
                             f"prior={prior:.1f} p50 {p50:.1f}->{p50_blended:.1f}")
                    p50 = p50_blended

                return {
                    "p10":        round(p10, 2),
                    "p50":        round(p50, 2),
                    "p90":        round(p90, 2),
                    "confidence": confidence,
                    "source":     "solcast_hourly",
                    "p10_hours":  p10_hours,
                    "p50_hours":  p50_hours,
                    "p90_hours":  p90_hours,
                }
            except Exception as e:
                self.log(f"Solcast hourly parse error: {e}", level="WARNING")

        # Fallback 1: Solcast daily total
        solcast = self._f("sensor.solcast_pv_forecast_forecast_tomorrow", 0)
        if solcast > 0:
            try:
                p10 = float(self.get_state(
                    "sensor.solcast_pv_forecast_forecast_tomorrow",
                    attribute="estimate10") or solcast * 0.5)
                p90 = float(self.get_state(
                    "sensor.solcast_pv_forecast_forecast_tomorrow",
                    attribute="estimate90") or solcast * 1.5)
            except Exception:
                p10 = solcast * 0.5
                p90 = solcast * 1.5
            confidence = self._calculate_confidence(p10, solcast, p90)
            self.log(f"Solar forecast tomorrow Solcast daily fallback: p50={solcast:.1f} kWh")
            return {
                "p10":        round(p10, 2),
                "p50":        round(solcast, 2),
                "p90":        round(p90, 2),
                "confidence": confidence,
                "source":     "solcast_daily",
                "p10_hours":  None,
                "p50_hours":  None,
                "p90_hours":  None,
            }

        # Fallback 2: Open-Meteo physics model
        self.log("Solcast unavailable - Open-Meteo fallback", level="WARNING")
        total = self._open_meteo_forecast()
        return {
            "p10":        round(total * 0.6, 2),
            "p50":        round(total, 2),
            "p90":        round(total * 1.3, 2),
            "confidence": 0.3,
            "source":     "open_meteo",
            "p10_hours":  None,
            "p50_hours":  None,
            "p90_hours":  None,
        }

    def _solar_forecast_today_remaining(self):
        """
        Returns solar forecast for the remainder of today.
        Used by midday_recalculation to get remaining generation from now.
        Uses Solcast forecast_today detailedForecast, trimmed to current hour onwards.
        Falls back to _solar_forecast_tomorrow() behaviour if unavailable.
        """
        now_hour = datetime.now().hour
        solcast_hourly = self.get_state(
            "sensor.solcast_pv_forecast_forecast_today",
            attribute="detailedForecast")

        if solcast_hourly and isinstance(solcast_hourly, list) and len(solcast_hourly) > 0:
            try:
                p10_hours_all = [float(h.get("pv_estimate10", 0)) / 2
                                 for h in solcast_hourly]
                p50_hours_all = [float(h.get("pv_estimate",   0)) / 2
                                 for h in solcast_hourly]
                p90_hours_all = [float(h.get("pv_estimate90", 0)) / 2
                                 for h in solcast_hourly]

                # Trim to remaining half-hour slots from now_hour
                start_slot = now_hour * 2
                p10_hours = p10_hours_all[start_slot:]
                p50_hours = p50_hours_all[start_slot:]
                p90_hours = p90_hours_all[start_slot:]

                p10 = sum(p10_hours)
                p50 = sum(p50_hours)
                p90 = sum(p90_hours)
                confidence = self._calculate_confidence(p10, p50, p90)

                self.log(f"Solar forecast today remaining (h{now_hour}+): "
                         f"p10={p10:.1f} p50={p50:.1f} p90={p90:.1f} kWh")

                return {
                    "p10":        round(p10, 2),
                    "p50":        round(p50, 2),
                    "p90":        round(p90, 2),
                    "confidence": confidence,
                    "source":     "solcast_today_remaining",
                    "p10_hours":  p10_hours,
                    "p50_hours":  p50_hours,
                    "p90_hours":  p90_hours,
                }
            except Exception as e:
                self.log(f"Solcast today_remaining parse error: {e}", level="WARNING")

        # Fall through to tomorrow forecast as best available proxy
        self.log("Solcast today unavailable for midday recalc - using tomorrow forecast",
                 level="WARNING")
        return self._solar_forecast_tomorrow()

    # Keep legacy name for the overnight decision (always uses tomorrow)
    def _solar_forecast_next_period(self):
        return self._solar_forecast_tomorrow()

    def _open_meteo_forecast(self):
        rad_all   = self.get_state("sensor.solar_weather_raw",
                                    attribute="shortwave_radiation")
        cloud_all = self.get_state("sensor.solar_weather_raw",
                                    attribute="cloud_cover")
        if not rad_all or not cloud_all:
            return self._f("sensor.solar_forecast_kwh", 5.0)

        month      = datetime.now().month
        nw_w       = NW_SEASONAL_WEIGHT.get(month, 0.5)
        correction = self._solar_correction(month)
        sol_start  = int(self._f("sensor.prism_solar_start_hour", 7))
        sol_end    = int(self._f("sensor.prism_solar_end_hour", 20))
        temp_high  = self._f("sensor.prism_ambient_temp_forecast_high", 20)

        rad   = rad_all[24:48]   if len(rad_all)   >= 48 else rad_all
        cloud = cloud_all[24:48] if len(cloud_all) >= 48 else cloud_all

        return round(sum(
            self._calculate_hourly_pv(h, rad, cloud, nw_w, correction,
                                      sol_start, sol_end, temp_high)
            for h in range(24)
        ), 2)

    # -- OVERNIGHT CHARGE DECISION ----------------------------

    def overnight_charge_decision(self, kwargs):
        self.log("=" * 60)
        self.log("PRISM v1.0: Overnight charge decision")

        if self._check_safe_mode():
            self.log(f"Safe mode - charging to {self.safe_mode_soc}%")
            self._apply_decision(True, self.safe_mode_soc, self.charge_power_kw)
            try:
                self.call_service("notify/notify",
                    title="PRISM: Safe mode activated",
                    message=(f"Critical sensors unavailable. "
                             f"Charging to {self.safe_mode_soc}% as precaution."))
            except Exception:
                pass
            return

        soc      = self._f(self.s_soc, 50)
        capacity = self._f("input_number.prism_battery_capacity",
                            self.battery_capacity_kwh)

        decision_time = datetime.now()
        planning_for  = decision_time.date()
        shift_type    = self._get_shift_type(datetime(planning_for.year,
                                                       planning_for.month,
                                                       planning_for.day))
        month   = planning_for.month
        weather = self._classify_weather(for_tomorrow=False)

        self.log(f"Planning for: {planning_for} shift={shift_type} "
                 f"SOC={soc}% weather={weather}")

        # ITEM 3: Overnight decision uses tomorrow forecast
        forecast   = self._solar_forecast_tomorrow()
        solar_p10  = forecast["p10"]
        solar_p50  = forecast["p50"]
        solar_p90  = forecast["p90"]
        confidence = forecast["confidence"]

        corrected, wx_correction = self._apply_weather_correction(
            {"p10": solar_p10, "p50": solar_p50, "p90": solar_p90},
            month, weather
        )
        solar_p10 = corrected["p10"]
        solar_p50 = corrected["p50"]
        solar_p90 = corrected["p90"]

        self.log(f"Solar: p10={solar_p10:.1f} p50={solar_p50:.1f} "
                 f"p90={solar_p90:.1f} kWh conf={confidence:.2f} "
                 f"source={forecast['source']}")

        load = self._predicted_load(shift_type)
        dd   = self._f("sensor.prism_degree_days_today", 0)
        load += dd * 0.3
        self.log(f"Load={load:.1f}kWh DD={dd:.1f}")

        soc_floor_base = self._soc_floor_from_confidence(confidence)
        bias           = self._decision_bias()
        soc_floor      = max(10, min(40, round(soc_floor_base - bias)))
        if bias != 0:
            self.log(f"SOC floor: base={soc_floor_base}% bias={bias:+.1f}% -> {soc_floor}%")

        # ITEM 20: Dynamic safety buffer based on confidence
        charge_safety_buffer = self._dynamic_charge_buffer(confidence)

        sim_p10 = self._simulate(soc, solar_p10, load, capacity, shift_type,
                                  forecast["p10_hours"],
                                  charge_active=False, charge_target=soc_floor)
        sim_p50 = self._simulate(soc, solar_p50, load, capacity, shift_type,
                                  forecast["p50_hours"],
                                  charge_active=False, charge_target=soc_floor)
        sim_p90 = self._simulate(soc, solar_p90, load, capacity, shift_type,
                                  forecast["p90_hours"],
                                  charge_active=False, charge_target=soc_floor)

        eve_p10 = min(p["soc"] for p in sim_p10 if p["h"] >= 17)
        eve_p50 = min(p["soc"] for p in sim_p50 if p["h"] >= 17)
        eve_p90 = min(p["soc"] for p in sim_p90 if p["h"] >= 17)
        end_p50 = sim_p50[-1]["soc"]

        self.log(f"Evening min (no-charge): p10={eve_p10:.1f}% p50={eve_p50:.1f}% "
                 f"p90={eve_p90:.1f}%")

        if soc < soc_floor:
            needed = soc_floor - soc + charge_safety_buffer
            reason = f"SOC {soc}% below floor {soc_floor}%"

        elif eve_p50 < soc_floor:
            needed = max(soc_floor - eve_p10,
                         soc_floor - eve_p50) + charge_safety_buffer
            reason = (f"p50 evening min {eve_p50:.1f}% below floor {soc_floor}% "
                      f"[{weather}]")

        elif eve_p10 < soc_floor and confidence < 0.5:
            needed = charge_safety_buffer
            reason = (f"p10 evening min {eve_p10:.1f}% below floor, "
                      f"low confidence {confidence:.2f} [{weather}]")

        else:
            needed = 0
            reason = (f"p50 solar {solar_p50:.1f}kWh sufficient. "
                      f"Evening mins: p10={eve_p10:.1f}% p50={eve_p50:.1f}% "
                      f"p90={eve_p90:.1f}% [{weather}]")

        # Axle export headroom — reserve SOC for upcoming export events
        axle_headroom = self._axle_headroom_needed()
        if axle_headroom > 0:
            current_soc = self._f(self.s_soc, soc)
            soc_available = max(0, current_soc - self.soc_min_floor)
            if soc_available < axle_headroom:
                extra = round(axle_headroom - soc_available, 1)
                needed = max(needed, extra)
                reason += f" | Axle headroom: +{axle_headroom:.1f}% reserved for export event"
                self.log(f"Axle headroom: reserving {axle_headroom:.1f}% SOC for export event")

        # Winter full charge
        if month in WINTER_MONTHS and solar_p50 < self.winter_solar_threshold:
            needed = max(100 - soc, 0)
            reason += (f" | Winter: solar p50 {solar_p50:.1f}kWh "
                       f"< {self.winter_solar_threshold}kWh")
            self.log("Winter full-charge triggered")

        # BMS cell balancing
        days_100 = self._days_since_full_charge()
        if days_100 >= self.bms_balance_days:
            needed = max(100 - soc, needed)
            reason += f" | BMS balance: {days_100} days since full charge"
            self.log(f"BMS balance triggered: {days_100} days")

        target        = min(round(soc + needed), 100)
        charge_active = needed > 0

        sim_p50 = self._simulate(soc, solar_p50, load, capacity, shift_type,
                                  forecast["p50_hours"],
                                  charge_active=charge_active,
                                  charge_target=target)
        eve_p50 = min(p["soc"] for p in sim_p50 if p["h"] >= 17)
        end_p50 = sim_p50[-1]["soc"]

        self.log(f"Decision: needed={needed:.1f}% target={target}% - {reason}")

        self._apply_decision(charge_active, target, self.charge_power_kw)
        self._notify_charge_decision(charge_active, target, reason,
                                     solar_p50, load, eve_p50, shift_type)

        sim_store = copy.deepcopy(sim_p50)  # deepcopy prevents mutation of simulation results

        detail = {
            "date":                    planning_for.strftime("%Y-%m-%d"),
            "shift_type":              shift_type,
            "soc_at_decision":         soc,
            "solar_forecast":          round(solar_p50, 2),
            "solar_p10":               round(solar_p10, 2),
            "solar_p50":               round(solar_p50, 2),
            "solar_p90":               round(solar_p90, 2),
            "forecast_confidence":     round(confidence, 3),
            "weather_class":           weather,
            "load_forecast":           round(load, 2),
            "min_soc_predicted":       min(p["soc"] for p in sim_p50),
            "min_soc_predicted_evening": round(eve_p50, 1),
            "eve_p10":                 round(eve_p10, 1),
            "eve_p50":                 round(eve_p50, 1),
            "eve_p90":                 round(eve_p90, 1),
            "end_soc_predicted":       round(end_p50, 1),
            "charge_needed_pct":       round(needed, 1),
            "charge_target_soc":       target,
            "reason":                  reason,
            "charge_needed":           charge_active,
            "confidence":              round(confidence, 3),
            "bias_applied":            bias,
            "soc_floor_used":          soc_floor,
            "charge_safety_buffer":    charge_safety_buffer,
            "simulation":              sim_store,
        }
        self.memory["last_charge_decision"]        = datetime.now().isoformat()
        self.memory["last_charge_decision_detail"] = detail

        history = self.memory.get("decision_history", [])
        history.append({
            "date":                      planning_for.strftime("%Y-%m-%d"),
            "shift":                     shift_type,
            "solar_p10":                 round(solar_p10, 2),
            "solar_p50":                 round(solar_p50, 2),
            "solar_p90":                 round(solar_p90, 2),
            "weather_class":             weather,
            "load_forecast":             round(load, 2),
            "min_soc_predicted":         min(p["soc"] for p in sim_p50),
            "min_soc_predicted_evening": round(eve_p50, 1),
            "eve_p10":                   round(eve_p10, 1),
            "soc_start":                 soc,
            "charge_target":             target,
            "charge_applied":            charge_active,
            "confidence":                round(confidence, 3),
            "bias_applied":              bias,
            "soc_floor_used":            soc_floor,
        })
        if len(history) > 60:
            history = history[-60:]
        self.memory["decision_history"] = history

        strategy = "CHARGE" if charge_active else "NO_CHARGE"
        self._persist_live_state(strategy, sim_store)
        self._save()
        self.run_in(self.publish_simulation_curve, 5)
        self._publish_observability_sensors()

        self.set_state("sensor.prism_charge_decision", state=target, attributes={
            "charge_needed":       charge_active,
            "needed_pct":          round(needed, 1),
            "reason":              reason,
            "solar_kwh":           round(solar_p50, 2),
            "solar_p10":           round(solar_p10, 2),
            "solar_p90":           round(solar_p90, 2),
            "load_kwh":            round(load, 2),
            "min_soc":             min(p["soc"] for p in sim_p50),
            "min_soc_evening":     round(eve_p50, 1),
            "eve_p10":             round(eve_p10, 1),
            "eve_p90":             round(eve_p90, 1),
            "weather_class":       weather,
            "shift_type":          shift_type,
            "confidence":          round(confidence, 3),
            "bias_applied":        bias,
            "charge_safety_buffer": charge_safety_buffer,
            "unit_of_measurement": "%",
            "friendly_name":       "PRISM Charge Target"
        })
        self.log("=" * 60)

    def _apply_decision(self, charge, target, charge_kw):
        if charge:
            self.log(f"Enabling charge: 02:00-05:00 target={target}%")
            self.call_service("switch/turn_on",    entity_id=self.e_charge_switch)
            self.call_service("select/select_option",
                entity_id=self.e_charge_start, option="02:00:00")
            self.call_service("select/select_option",
                entity_id=self.e_charge_end,   option="05:00:00")
            self.call_service("number/set_value",
                entity_id=self.e_charge_target, value=target)
            self.call_service("number/set_value",
                entity_id=self.e_charge_rate,   value=int(charge_kw * 1000))
        else:
            self.log("No charge needed - disabling schedule")
            self.call_service("switch/turn_off", entity_id=self.e_charge_switch)

    # -- MID-DAY RECALCULATION --------------------------------

    def midday_recalculation(self, kwargs):
        hour = datetime.now().hour
        self.log(f"PRISM v1.0: Mid-day recalculation at {hour:02d}:00")

        if self._check_safe_mode():
            self.log("Safe mode - skipping recalculation")
            return

        soc        = self._f(self.s_soc, 50)
        capacity   = self._f("input_number.prism_battery_capacity",
                              self.battery_capacity_kwh)
        today      = datetime.now()
        shift_type = self._get_shift_type(today)
        weather    = self._classify_weather(for_tomorrow=False)
        month      = datetime.now().month

        # ITEM 3: Midday recalc uses today_remaining
        forecast   = self._solar_forecast_today_remaining()
        solar_p10  = forecast["p10"]
        solar_p50  = forecast["p50"]
        solar_p90  = forecast["p90"]
        confidence = forecast["confidence"]

        corrected, _ = self._apply_weather_correction(
            {"p10": solar_p10, "p50": solar_p50, "p90": solar_p90},
            month, weather
        )
        solar_p10 = corrected["p10"]
        solar_p50 = corrected["p50"]
        solar_p90 = corrected["p90"]

        load  = self._predicted_load(shift_type)
        dd    = self._f("sensor.prism_degree_days_today", 0)
        load += dd * 0.3

        sim_p50 = self._simulate(soc, solar_p50, load, capacity, shift_type,
                                  forecast["p50_hours"],
                                  charge_active=False, charge_target=self.soc_min_floor)
        sim_p10 = self._simulate(soc, solar_p10, load, capacity, shift_type,
                                  forecast["p10_hours"],
                                  charge_active=False, charge_target=self.soc_min_floor)
        sim_p90 = self._simulate(soc, solar_p90, load, capacity, shift_type,
                                  forecast["p90_hours"],
                                  charge_active=False, charge_target=self.soc_min_floor)

        min_soc = min(p["soc"] for p in sim_p50)
        eve_p50 = min(p["soc"] for p in sim_p50 if p["h"] >= 17)
        eve_p10 = min(p["soc"] for p in sim_p10 if p["h"] >= 17)
        eve_p90 = min(p["soc"] for p in sim_p90 if p["h"] >= 17)

        self.log(f"Recalc: shift={shift_type} weather={weather} "
                 f"solar p50={solar_p50:.1f}kWh load={load:.1f}kWh "
                 f"eve p10={eve_p10:.1f}% p50={eve_p50:.1f}% p90={eve_p90:.1f}%")

        # Store intraday forecast in a separate key - never overwrite overnight decision data
        detail = self.memory.get("last_charge_decision_detail", {})
        detail["intraday_simulation"]       = copy.deepcopy(sim_p50)
        detail["intraday_solar_forecast"]   = round(solar_p50, 2)
        detail["intraday_solar_p10"]        = round(solar_p10, 2)
        detail["intraday_solar_p90"]        = round(solar_p90, 2)
        detail["intraday_load_forecast"]    = round(load, 2)
        detail["intraday_min_soc"]          = round(min_soc, 1)
        detail["intraday_eve_p50"]          = round(eve_p50, 1)
        detail["intraday_eve_p10"]          = round(eve_p10, 1)
        detail["intraday_eve_p90"]          = round(eve_p90, 1)
        detail["intraday_weather"]          = weather
        detail["intraday_confidence"]       = round(confidence, 3)
        detail["intraday_updated_at"]       = datetime.now().isoformat()
        self.memory["last_charge_decision_detail"] = detail

        soc_floor = self._soc_floor_from_confidence(confidence)
        bias      = self._decision_bias()
        soc_floor = max(10, min(40, round(soc_floor - bias)))
        charge_safety_buffer = self._dynamic_charge_buffer(confidence)

        if soc < soc_floor:
            preview_charge = True
            preview_target = min(round(soc + soc_floor - soc + charge_safety_buffer), 100)
            preview_reason = f"SOC {soc}% below floor {soc_floor}% (preview)"
        elif eve_p50 < soc_floor:
            needed_preview = max(soc_floor - eve_p10,
                                 soc_floor - eve_p50) + charge_safety_buffer
            preview_charge = True
            preview_target = min(round(soc + needed_preview), 100)
            preview_reason = (f"p50 evening min {eve_p50:.1f}% below floor "
                              f"{soc_floor}% [{weather}] (preview)")
        elif eve_p10 < soc_floor and confidence < 0.5:
            preview_charge = True
            preview_target = min(round(soc + charge_safety_buffer), 100)
            preview_reason = (f"p10 {eve_p10:.1f}% below floor, low confidence "
                              f"[{weather}] (preview)")
        else:
            preview_charge = False
            preview_target = round(soc)
            preview_reason = (f"Solar p50 {solar_p50:.1f}kWh sufficient. "
                              f"Eve p10={eve_p10:.1f}% p50={eve_p50:.1f}% "
                              f"p90={eve_p90:.1f}% [{weather}] "
                              f"(preview - final decision at 01:30)")

        self.log(f"Preview: charge={preview_charge} target={preview_target}% - {preview_reason}")

        self._publish_preview_sensors(
            hour, solar_p10, solar_p50, solar_p90, load, shift_type,
            eve_p10, eve_p50, eve_p90, weather, confidence,
            preview_charge, preview_target, preview_reason, soc
        )

        strategy = "CHARGE" if detail.get("charge_needed", False) else "NO_CHARGE"
        self._persist_live_state(strategy, copy.deepcopy(sim_p50))
        self._save()
        self.publish_simulation_curve()
        self._publish_observability_sensors()

    def _publish_preview_sensors(self, hour, solar_p10, solar_p50, solar_p90,
                                  load, shift_type, eve_p10, eve_p50, eve_p90,
                                  weather, confidence, preview_charge, preview_target,
                                  preview_reason, soc):
        preview_time = datetime.now().strftime("%H:%M")
        attrs = {"updated": preview_time}

        self.set_state("sensor.prism_tomorrow_solar_p10",
            state=str(round(solar_p10, 2)),
            attributes={**attrs, "unit_of_measurement": "kWh",
                        "friendly_name": "PRISM Tomorrow Solar p10"})
        self.set_state("sensor.prism_tomorrow_solar_p50",
            state=str(round(solar_p50, 2)),
            attributes={**attrs, "unit_of_measurement": "kWh",
                        "friendly_name": "PRISM Tomorrow Solar p50"})
        self.set_state("sensor.prism_tomorrow_solar_p90",
            state=str(round(solar_p90, 2)),
            attributes={**attrs, "unit_of_measurement": "kWh",
                        "friendly_name": "PRISM Tomorrow Solar p90"})
        self.set_state("sensor.prism_tomorrow_load",
            state=str(round(load, 2)),
            attributes={**attrs, "unit_of_measurement": "kWh", "shift": shift_type,
                        "friendly_name": "PRISM Tomorrow Load Forecast"})
        self.set_state("sensor.prism_tomorrow_net_energy",
            state=str(round(solar_p50 - load, 2)),
            attributes={**attrs, "unit_of_measurement": "kWh",
                        "friendly_name": "PRISM Tomorrow Net Energy"})
        self.set_state("sensor.prism_tomorrow_eve_p10",
            state=str(round(eve_p10, 1)),
            attributes={**attrs, "unit_of_measurement": "%",
                        "friendly_name": "PRISM Tomorrow Evening Min p10"})
        self.set_state("sensor.prism_tomorrow_eve_p50",
            state=str(round(eve_p50, 1)),
            attributes={**attrs, "unit_of_measurement": "%",
                        "friendly_name": "PRISM Tomorrow Evening Min p50"})
        self.set_state("sensor.prism_tomorrow_eve_p90",
            state=str(round(eve_p90, 1)),
            attributes={**attrs, "unit_of_measurement": "%",
                        "friendly_name": "PRISM Tomorrow Evening Min p90"})
        self.set_state("sensor.prism_tomorrow_weather",
            state=weather,
            attributes={**attrs, "confidence": round(confidence, 3),
                        "friendly_name": "PRISM Tomorrow Weather Class"})
        self.set_state("sensor.prism_tomorrow_charge_needed",
            state="YES" if preview_charge else "NO",
            attributes={**attrs, "target_soc": preview_target,
                        "friendly_name": "PRISM Tomorrow Charge Needed"})
        self.set_state("sensor.prism_tomorrow_shift",
            state=shift_type,
            attributes={**attrs, "friendly_name": "PRISM Tomorrow Shift"})
        self.set_state("sensor.prism_preview_updated",
            state=preview_time,
            attributes={"date": datetime.now().strftime("%Y-%m-%d"),
                        "friendly_name": "PRISM Preview Last Updated"})

        self.set_state("sensor.prism_charge_decision",
            state=preview_target,
            attributes={
                "charge_needed":       preview_charge,
                "needed_pct":          0,
                "reason":              preview_reason,
                "solar_kwh":           round(solar_p50, 2),
                "solar_p10":           round(solar_p10, 2),
                "solar_p90":           round(solar_p90, 2),
                "load_kwh":            round(load, 2),
                "min_soc_evening":     round(eve_p50, 1),
                "eve_p10":             round(eve_p10, 1),
                "eve_p90":             round(eve_p90, 1),
                "weather_class":       weather,
                "shift_type":          shift_type,
                "confidence":          round(confidence, 3),
                "bias_applied":        self._decision_bias(),
                "is_preview":          True,
                "preview_hour":        hour,
                "unit_of_measurement": "%",
                "friendly_name":       "PRISM Charge Target"
            })
        self.log(f"Tomorrow preview sensors updated at {preview_time}")

    # -- PV CALCULATION ----------------------------------------

    def _calculate_hourly_pv(self, h, rad, cloud, nw_w, correction,
                              sol_start, sol_end, temp_high=20.0):
        if h < sol_start or h > sol_end:
            return 0.0
        if not rad or not cloud or len(rad) <= h:
            return 0.0
        r  = float(rad[h])
        c  = float(cloud[h])
        se = SE_HOUR.get(h, 0.0)
        nw = NW_HOUR.get(h, 0.0) * nw_w
        combined = (se + nw) / (1.0 + nw_w)
        pv = (r / 1000) * ((100 - c) / 100) * combined * \
             (self.num_panels_se + self.num_panels_nw) * self.panel_rating_w / 1000
        pv *= correction
        if temp_high > 25:
            pv *= (1 - (temp_high - 25) * 0.004)
        pv = min(pv, self.inverter_limit_kw)
        return round(max(0.0, pv), 2)

    # -- SIMULATION -------------------------------------------

    def _simulate(self, start_soc, solar_kwh, load_kwh, capacity,
                  shift_type, solcast_hours=None,
                  charge_active=False, charge_target=100):
        """
        ITEM 7:  Discharge efficiency 0.94 applied to net discharge hours.
        ITEM 17: Output keys renamed pv_kwh/load_kwh (pv_kw/load_kw removed).
        All AXLE v3.7 fixes retained (charge efficiency 0.93, SOC taper, etc.)
        """
        month      = datetime.now().month
        nw_w       = NW_SEASONAL_WEIGHT.get(month, 0.5)
        correction = self._solar_correction(month)
        sol_start  = int(self._f("sensor.prism_solar_start_hour", 7))
        sol_end    = int(self._f("sensor.prism_solar_end_hour", 20))
        temp_high  = self._f("sensor.prism_ambient_temp_forecast_high", 20)

        if solcast_hours and isinstance(solcast_hours, list) and len(solcast_hours) >= 2:
            # solcast_hours are half-hourly kWh values (each = 30-min interval)
            # Pair consecutive entries into hourly totals, pad with 0 if odd length
            # Handles both full 48-entry tomorrow forecasts and trimmed today_remaining lists
            entries = list(solcast_hours)
            if len(entries) % 2 != 0:
                entries.append(0.0)  # pad to even length
            paired = [float(entries[i]) + float(entries[i+1])
                      for i in range(0, len(entries), 2)]
            # Pad or trim to exactly 24 hours
            hourly_raw = (paired + [0.0] * 24)[:24]
            hourly_pv  = [round(min(v, self.inverter_limit_kw), 3) for v in hourly_raw]
            # No double correction — weather correction already applied upstream
        else:
            rad_all   = self.get_state("sensor.solar_weather_raw",
                                        attribute="shortwave_radiation")
            cloud_all = self.get_state("sensor.solar_weather_raw",
                                        attribute="cloud_cover")
            if rad_all and len(rad_all) >= 48:
                rad   = rad_all[24:48]
                cloud = cloud_all[24:48]
            else:
                rad   = rad_all
                cloud = cloud_all

            raw_pv = [self._calculate_hourly_pv(h, rad, cloud, nw_w, correction,
                                                 sol_start, sol_end, temp_high)
                      for h in range(24)]
            raw_total = sum(raw_pv)
            if raw_total > 0 and solar_kwh > 0:
                # Note: _calculate_hourly_pv applies correction internally, but solar_kwh
                # already has correction applied via _apply_weather_correction() upstream.
                # The scale factor (solar_kwh / raw_total) effectively normalises this —
                # no double correction in practice, but the shape is physics-model-derived.
                scale = solar_kwh / raw_total
                hourly_pv = [round(p * scale, 3) for p in raw_pv]
            else:
                hourly_pv = raw_pv

        weights      = SHIFT_HOURLY_WEIGHTS.get(shift_type, SHIFT_HOURLY_WEIGHTS["OFF"])
        total_weight = sum(weights.values())

        result = []
        soc    = start_soc

        for h in range(24):
            pv = hourly_pv[h]
            hl = load_kwh * weights.get(h, 0.5) / total_weight

            if (charge_active and
                    self._is_cheap_rate_hour(h) and
                    soc < charge_target):
                # Charge with efficiency and SOC taper
                taper = 1.0
                if soc > 85:
                    taper = max(0.2, 1.0 - (soc - 85) / 15.0)
                charge_kw = self.charge_power_kw * taper
                effective_charge = charge_kw * 0.93  # charge efficiency
                net = pv - hl + effective_charge
            else:
                net = pv - hl

            # ITEM 7: Apply discharge efficiency when net is negative (discharging)
            if net < 0:
                net_effective = net * 0.94  # discharge efficiency reduces effective net
            else:
                net_effective = net

            soc = max(0.0, min(100.0, soc + (net_effective / capacity) * 100))
            result.append({
                "h":        h,
                "soc":      round(soc, 1),
                "pv_kwh":   round(pv, 2),    # ITEM 17: renamed from pv_kw
                "load_kwh": round(hl, 2),    # ITEM 17: renamed from load_kw
                "net_kwh":  round(net, 2),   # ITEM 17: renamed from net_kw
            })

        return result

    # -- DAILY OBSERVATION ------------------------------------

    def record_daily_observation(self, kwargs):
        self.log("PRISM v1.0: Recording daily observation")
        today      = datetime.now().strftime("%Y-%m-%d")
        shift_type = self._get_shift_type(datetime.now())
        load       = self._f(self.s_load_energy, 0)
        pv_giv     = self._f(self.s_pv_energy, 0)
        pv_growatt = self._f(self.growatt_total, 0)
        pv_se      = self._f(self.growatt_se, 0)
        pv_nw      = self._f(self.growatt_nw, 0)
        pv_actual  = pv_growatt if pv_growatt > 0 else pv_giv
        # Use Solcast forecast_today as the correction baseline — this is what PRISM uses in decisions
        # Fall back to Open-Meteo if Solcast unavailable
        forecast   = self._f("sensor.solcast_pv_forecast_forecast_today", 0)
        if forecast <= 0:
            forecast = self._f("sensor.solar_forecast_kwh", 0)
        soc        = self._f(self.s_soc, 50)
        dd         = self._f("sensor.prism_degree_days_today", 0)
        weather    = self._classify_weather(for_tomorrow=False)
        obs_date   = datetime.strptime(today, "%Y-%m-%d")
        month      = str(obs_date.month)

        if soc >= 99.0:
            self.memory["last_full_charge_date"] = today
            self.log("Battery reached 100% - BMS timer reset")

        if pv_se > 0 and pv_nw > 0:
            ratios = self.memory.get("nw_se_ratios", {})
            r      = ratios.get(month, [])
            r.append(round(pv_nw / pv_se, 3))
            if len(r) > 30:
                r = r[-30:]
            ratios[month] = r
            self.memory["nw_se_ratios"] = ratios

        obs = {
            "date":           today,
            "shift_type":     shift_type,
            "load":           round(load, 2),
            "pv":             round(pv_actual, 2),
            "pv_se":          round(pv_se, 2),
            "pv_nw":          round(pv_nw, 2),
            "soc_end":        round(soc, 1),
            "degree_days":    round(dd, 2),
            "forecast_solar": round(forecast, 2),
            "weather_class":  weather
        }
        self.log(f"Observation: {json.dumps(obs)}")

        observations = self.memory.get("observations", [])
        observations.append(obs)
        if len(observations) > 21 * 21:
            observations = observations[-(21 * 21):]
        self.memory["observations"]          = observations
        self.memory["last_observation_date"] = today

        loads = self.memory.get("daily_loads", {})
        sl    = loads.get(shift_type, [])
        sl.append(round(load, 2))
        if len(sl) > 21:
            sl = sl[-21:]
        loads[shift_type]          = sl
        self.memory["daily_loads"] = loads

        if forecast >= 0.5:
            corrections = self.memory.get("solar_corrections", {})
            current     = corrections.get(month, 1.0)
            ratio       = max(0.3, min(2.0, pv_actual / forecast))
            err_mag     = abs(ratio - current) / max(current, 0.1)
            alpha       = 0.2 if err_mag > 0.3 else (0.15 if err_mag > 0.15 else 0.1)
            corrections[month] = round(current * (1 - alpha) + ratio * alpha, 3)
            self.memory["solar_corrections"] = corrections
            self.log(f"Solar correction m{month}: {current:.3f}->"
                     f"{corrections[month]:.3f} (a={alpha})")

            if weather != "UNKNOWN":
                wx_corrections = self.memory.get("solar_corrections_by_weather", {})
                wx_samples     = self.memory.get("solar_corrections_weather_samples", {})
                month_wx       = wx_corrections.get(month, {})
                month_samples  = wx_samples.get(month, {})

                wx_current = month_wx.get(weather, 1.0)
                wx_new     = round(wx_current * (1 - alpha) + ratio * alpha, 3)
                month_wx[weather]      = wx_new
                month_samples[weather] = month_samples.get(weather, 0) + 1

                wx_corrections[month] = month_wx
                wx_samples[month]     = month_samples
                self.memory["solar_corrections_by_weather"]      = wx_corrections
                self.memory["solar_corrections_weather_samples"] = wx_samples

                self.log(f"Weather correction {weather} m{month}: "
                         f"{wx_current:.3f}->{wx_new:.3f} "
                         f"(n={month_samples[weather]})")

        self._save()
        self.log(f"Saved: shift={shift_type} load={load:.1f} "
                 f"pv={pv_actual:.1f} SE={pv_se:.1f} NW={pv_nw:.1f} "
                 f"weather={weather}")

    # -- SELF VALIDATION --------------------------------------

    def self_validate(self, kwargs):
        self.log("PRISM v1.0: Self-validation")
        last = self.memory.get("last_charge_decision_detail", {})
        if not last:
            return

        sim = last.get("simulation", [])
        if sim:
            evening   = [p["soc"] for p in sim if p["h"] >= 17]
            predicted = min(evening) if evening else last.get("min_soc_predicted_evening", 50)
            self.log("Validation using evening min SOC (h17+) from most recent simulation")
        else:
            predicted = last.get("min_soc_predicted_evening",
                        last.get("min_soc_predicted", 50))

        actual   = self._f(self.s_soc, 50)
        error    = predicted - actual
        accuracy = max(0, 100 - abs(error) * 2)

        self.memory["accuracy_score"] = round(accuracy, 1)
        self.memory["last_error"]     = round(error, 1)

        history = self.memory.get("decision_history", [])
        if history:
            last_record   = history[-1]
            decision_date = last_record.get("date", "")
            today         = datetime.now().strftime("%Y-%m-%d")
            yesterday     = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            tomorrow      = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

            if decision_date in [today, yesterday, tomorrow]:
                last_record["min_soc_actual"]            = round(actual, 1)
                last_record["min_soc_predicted_eve"]     = round(predicted, 1)
                last_record["min_soc_predicted_evening"] = round(predicted, 1)
                last_record["error"]                     = round(error, 1)
                last_record["accuracy"]                  = round(accuracy, 1)
                history[-1]                              = last_record
                self.memory["decision_history"]          = history
                self.log(f"Decision history closed: predicted_eve={predicted:.1f}% "
                         f"actual={actual:.1f}% error={error:.1f}%")
            else:
                self.log(f"Validation date mismatch: decision={decision_date} "
                         f"today={today} - history not updated", level="WARNING")

        self.call_service("input_number/set_value",
            entity_id="input_number.prism_soc_forecast_error",
            value=round(error, 1))
        self.call_service("input_number/set_value",
            entity_id="input_number.prism_soc_accuracy_score",
            value=round(accuracy, 1))

        self.log(f"Validation: predicted_eve={predicted:.1f}% "
                 f"actual={actual:.1f}% error={error:.1f}% "
                 f"accuracy={accuracy:.1f}%")

        # ITEM 22: Refresh confidence trend after validation closes
        self._publish_confidence_trend()
        self._publish_mae_sensor()
        self._save()

    # -- PUBLISH SIMULATION CURVE -----------------------------

    def publish_simulation_curve(self, kwargs=None):
        detail = self.memory.get("last_charge_decision_detail", {})
        sim    = detail.get("simulation", [])
        if not sim:
            return
        decision_date = detail.get("date", "")
        if not decision_date:
            return
        try:
            base = datetime.strptime(decision_date, "%Y-%m-%d")
        except Exception:
            return

        # ITEM 17: Support both old key names (pv_kw) and new (pv_kwh) during transition
        curve = [
            {
                "t":        (base + timedelta(hours=p["h"])).isoformat(),
                "soc":      p.get("soc", 0),
                "pv_kwh":   p.get("pv_kwh", p.get("pv_kw", 0)),
                "load_kwh": p.get("load_kwh", p.get("load_kw", 0)),
                "net_kwh":  p.get("net_kwh", p.get("net_kw", 0)),
            }
            for p in sim
        ]
        min_soc  = min(p["soc"] for p in sim)
        max_soc  = max(p["soc"] for p in sim)
        min_hour = next(p["h"] for p in sim if p["soc"] == min_soc)

        self.set_state("sensor.prism_soc_simulation_curve", state="OK",
            attributes={
                "curve":               curve,
                "decision_date":       decision_date,
                "shift_type":          detail.get("shift_type", ""),
                "solar_forecast":      detail.get("solar_forecast", 0),
                "solar_p10":           detail.get("solar_p10", 0),
                "solar_p90":           detail.get("solar_p90", detail.get("solar_forecast", 0)),
                "load_forecast":       detail.get("load_forecast", 0),
                "weather_class":       detail.get("weather_class", "UNKNOWN"),
                "forecast_confidence": detail.get("forecast_confidence",
                                       detail.get("confidence", 0)),
                "min_soc_predicted":   round(min_soc, 1),
                "max_soc_predicted":   round(max_soc, 1),
                "min_soc_hour":        min_hour,
                "charge_needed":       detail.get("charge_needed", False),
                "charge_target":       detail.get("charge_target_soc", 0),
                "reason":              detail.get("reason", ""),
                "friendly_name":       "PRISM SOC Simulation Curve"
            })

        pv_vals   = [p["pv_kwh"]   for p in curve]
        load_vals = [p["load_kwh"] for p in curve]
        soc_vals  = [p["soc"]      for p in curve]
        times     = [p["t"]        for p in curve]

        self.set_state("sensor.prism_sim_pv", state="ok",
            attributes={"curve": pv_vals, "times": times,
                        "friendly_name": "PRISM Sim PV Curve"})
        self.set_state("sensor.prism_sim_load", state="ok",
            attributes={"curve": load_vals, "times": times,
                        "friendly_name": "PRISM Sim Load Curve"})
        self.set_state("sensor.prism_sim_soc", state="ok",
            attributes={"curve": soc_vals, "times": times,
                        "friendly_name": "PRISM Sim SOC Curve"})

        self.log(f"Simulation curve: {len(curve)} points "
                 f"min={min_soc:.1f}% h{min_hour} max={max_soc:.1f}%")

    # -- NOTIFICATIONS ----------------------------------------

    def _notify_charge_decision(self, charge_needed, target_soc, reason,
                                 solar_kwh, load_kwh, min_soc, shift_type):
        days = self._days_since_full_charge()
        if charge_needed:
            title   = f"PRISM: Charging to {target_soc}% tonight"
            message = (f"Shift: {shift_type} | Solar p50: {solar_kwh:.1f}kWh | "
                       f"Load: {load_kwh:.1f}kWh | Evening min SOC: {min_soc:.1f}% | "
                       f"{reason}")
        else:
            title   = "PRISM: No charge needed tonight"
            message = (f"Shift: {shift_type} | Solar p50: {solar_kwh:.1f}kWh covers "
                       f"{load_kwh:.1f}kWh | Evening min: {min_soc:.1f}% | "
                       f"Days since full: {days}")
        try:
            self.call_service("notify/notify", title=title, message=message)
            self.log(f"Notification: {title}")
        except Exception as e:
            self.log(f"Notification failed: {e}", level="WARNING")

    # -- WATCHDOGS --------------------------------------------


    def _detect_cheap_rate_window(self):
        """
        Auto-detect cheap rate window from Octopus Energy current_day_rates event.
        Finds the contiguous block of minimum-rate half-hour slots.
        On success: updates self.cheap_rate_start/end and persists to memory.
        On failure: restores last good values from memory, falls back to apps.yaml defaults.
        Returns (start_hour, end_hour) as integers.
        """
        import_mpan = "17p0308861_1414235320008"
        entity_id   = f"event.octopus_energy_electricity_{import_mpan}_current_day_rates"

        try:
            rates = self.get_state(entity_id, attribute="rates")
            if not rates or not isinstance(rates, list):
                raise ValueError("rates attribute unavailable")

            # Find minimum rate value
            min_rate = min(r["value_inc_vat"] for r in rates)

            # Collect all half-hour slot start hours at minimum rate
            cheap_slots = []
            for r in rates:
                if abs(r["value_inc_vat"] - min_rate) < 0.001:
                    start_str = r.get("start", "")
                    if start_str:
                        dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                        local_dt = dt.astimezone()
                        cheap_slots.append(local_dt.hour + local_dt.minute / 60)

            if not cheap_slots:
                raise ValueError("no cheap slots found in rate data")

            cheap_slots = sorted(set(cheap_slots))
            start_h = int(cheap_slots[0])
            end_h   = int(cheap_slots[-1]) + 1  # end hour is exclusive

            # Sanity check
            if not (1 <= end_h - start_h <= 6):
                raise ValueError(f"implausible window {start_h}-{end_h}h")

            # Success — persist to memory as fallback for future failures
            changed = (start_h != self.cheap_rate_start or end_h != self.cheap_rate_end)
            self.cheap_rate_start = start_h
            self.cheap_rate_end   = end_h
            self.memory["cheap_rate_start"]       = start_h
            self.memory["cheap_rate_end"]         = end_h
            self.memory["cheap_rate_detected_at"] = datetime.now().isoformat()
            self.memory["cheap_rate_p_per_kwh"]   = round(min_rate * 100, 4)
            self._save()

            if changed:
                self.log(f"Tariff auto-detect: cheap rate {start_h:02d}:00-{end_h:02d}:00 "
                         f"@ {min_rate*100:.2f}p/kWh (saved to memory)")
            else:
                self.log(f"Tariff auto-detect: {start_h:02d}:00-{end_h:02d}:00 "
                         f"@ {min_rate*100:.2f}p/kWh confirmed", level="DEBUG")

            return start_h, end_h

        except Exception as e:
            # Detection failed — try memory fallback before apps.yaml defaults
            mem_start = self.memory.get("cheap_rate_start", 0)
            mem_end   = self.memory.get("cheap_rate_end", 0)
            detected_at = self.memory.get("cheap_rate_detected_at", "")

            if mem_start and mem_end and detected_at:
                self.cheap_rate_start = mem_start
                self.cheap_rate_end   = mem_end
                self.log(f"Tariff auto-detect failed ({e}) — using last known "
                         f"{mem_start:02d}:00-{mem_end:02d}:00 (detected {detected_at[:10]})",
                         level="WARNING")
                return mem_start, mem_end
            else:
                self.log(f"Tariff auto-detect failed ({e}) — no memory fallback, "
                         f"using apps.yaml {self.cheap_rate_start:02d}:00-{self.cheap_rate_end:02d}:00",
                         level="WARNING")
                return self.cheap_rate_start, self.cheap_rate_end


    def _is_cheap_rate_hour(self, h=None):
        """Safe cheap rate check — handles overnight windows (e.g. 23:00-06:00)."""
        if h is None:
            h = datetime.now().hour
        start, end = self.cheap_rate_start, self.cheap_rate_end
        if end > start:          # normal window e.g. 02:00-05:00
            return start <= h < end
        else:                    # overnight crossing e.g. 23:00-06:00
            return h >= start or h < end

    def cheap_rate_watchdog(self, kwargs):
        h = datetime.now().hour
        if not self._is_cheap_rate_hour(h):
            return
        last = self.memory.get("last_charge_decision_detail", {})
        if not last.get("charge_needed", False):
            return
        soc      = self._f(self.s_soc, 50)
        bat      = self._f(self.s_battery_power, 0)
        schedule = self._get_state(self.e_charge_switch, "off")
        target   = last.get("charge_target_soc", 100)
        if soc >= target:
            self.log("Watchdog: target reached - disabling")
            self.call_service("switch/turn_off", entity_id=self.e_charge_switch)
            return
        if schedule == "on" and bat > -100:
            self.log("Watchdog: not charging - re-applying", level="WARNING")
            self._apply_decision(True, target, self.charge_power_kw)
            try:
                self.call_service("notify/notify",
                    title="PRISM: Charge watchdog triggered",
                    message=f"Re-applied. SOC={soc}% target={target}%")
            except Exception as e:
                self.log(f"Watchdog notify: {e}", level="WARNING")

    # ITEM 18: Export manager with PI controller and anti-windup
    def export_soc_watchdog(self, kwargs):
        """
        Active export manager — runs every 2 minutes.
        ITEM 18: PI controller with anti-windup (integral term Ki added).
        """
        mode       = self._get_state(self.e_mode, "Eco")
        bat_power  = self._f(self.s_battery_power, 0)
        was_managing = self.memory.get("export_managing", False)

        in_managed_export = (
            mode in ["Timed Export", "Timed Demand"] and
            bat_power > 200
        )

        if not in_managed_export:
            if was_managing:
                self.log("Export event ended - resetting discharge rate")
                self.call_service("number/set_value",
                    entity_id=self.e_discharge_rate,
                    value=5000)

                try:
                    soc        = self._f(self.s_soc, 0)
                    start_soc  = self.memory.get("export_start_soc", 0)
                    start_time = self.memory.get("export_start_time", "")
                    duration_mins = 0
                    if start_time:
                        try:
                            start_dt = datetime.fromisoformat(start_time)
                            duration_mins = int((datetime.now() - start_dt).total_seconds() / 60)
                        except Exception:
                            pass
                    battery_used = round(max(0, start_soc - soc) * self.battery_capacity_kwh / 100, 2)
                    self.call_service("notify/notify",
                        title="PRISM: Export event ended",
                        message=(f"Duration: {duration_mins} mins | "
                                 f"Battery used: {battery_used:.1f}kWh | "
                                 f"SOC: {start_soc:.0f}% -> {soc:.0f}%"))
                except Exception as e:
                    self.log(f"Export end notify: {e}", level="WARNING")

                self.memory["export_managing"]    = False
                self.memory["export_correction"]  = 0
                self.memory["export_integral"]    = 0   # ITEM 18: reset integral
                self.memory["export_start_soc"]   = 0
                self.memory["export_start_time"]  = ""
                self._save()
            return

        if not was_managing:
            soc = self._f(self.s_soc, 0)
            self.memory["export_managing"]   = True
            self.memory["export_start_soc"]  = round(soc, 1)
            self.memory["export_start_time"] = datetime.now().isoformat()
            self.memory["export_integral"]   = 0  # ITEM 18: initialise integral
            export_rate = self._f(
                "sensor.octopus_energy_electricity_17p0308861_1470001817084_export_current_rate", 0)
            solcast_now = self._f("sensor.solcast_pv_forecast_power_now", 0)
            try:
                self.call_service("notify/notify",
                    title="PRISM: Export event started",
                    message=(f"SOC: {soc:.0f}% | "
                             f"Solar now: {solcast_now:.0f}W | "
                             f"Export rate: {export_rate*100:.1f}p/kWh"))
            except Exception as e:
                self.log(f"Export start notify: {e}", level="WARNING")

        soc           = self._f(self.s_soc, 50)
        load_w        = self._f(self.s_load_power, 0)
        actual_export = self._f(self.s_export_power, 0)
        target_w      = self.export_limit_kw * 1000

        if soc <= self.soc_min_floor:
            self.log(f"EXPORT SOC FLOOR: {soc}% - stopping export", level="WARNING")
            self.call_service("select/select_option",
                entity_id=self.e_mode, option="Eco")
            self.call_service("number/set_value",
                entity_id=self.e_discharge_rate, value=5000)
            self.memory["export_managing"]   = False
            self.memory["export_correction"] = 0
            self.memory["export_integral"]   = 0
            self._save()
            try:
                self.call_service("notify/notify",
                    title="PRISM: Export stopped - battery floor",
                    message=f"SOC {soc:.0f}% reached floor during export. Returned to Eco.")
            except Exception as e:
                self.log(f"Notify: {e}", level="WARNING")
            return

        solcast_now  = self._f("sensor.solcast_pv_forecast_power_now", 0)
        solcast_next = self._f("sensor.solcast_pv_forecast_forecast_next_hour", 0)
        minute       = datetime.now().minute
        blend        = minute / 60.0
        predicted_solar = solcast_now * (1 - blend) + solcast_next * blend

        max_solar_contrib = target_w + load_w
        effective_solar   = min(predicted_solar, max_solar_contrib)
        solar_surplus     = max(0, effective_solar - load_w)
        predicted_discharge = max(500, target_w - solar_surplus)

        # PI controller with anti-windup — tuned for 4.5kW DNO limit
        # Kp=0.5 (was 0.3) for faster response to export shortfall
        # Ki=0.08 (was 0.05) for stronger steady-state correction
        # Minimum discharge floor = target - load (ensure we're always pushing enough)
        export_error = target_w - actual_export
        Kp = 0.5
        Ki = 0.08

        integral = self.memory.get("export_integral", 0)
        integral += export_error * Ki
        # Anti-windup clamp — wider to allow more steady-state correction
        integral = max(-2000, min(2000, integral))
        self.memory["export_integral"] = integral

        correction = export_error * Kp + integral
        correction = max(-2000, min(2000, correction))
        self.memory["export_correction"] = correction

        discharge_w = int(predicted_discharge + correction)
        # Floor: always discharge at least (target - load) so solar shortfall doesn't starve export
        min_discharge = max(500, int(target_w - load_w))
        discharge_w = max(min_discharge, min(int(self.inverter_limit_kw * 1000), discharge_w))

        self.call_service("number/set_value",
            entity_id=self.e_discharge_rate,
            value=discharge_w)

        self.log(f"Export PI: predicted={predicted_solar:.0f}W "
                 f"surplus={solar_surplus:.0f}W discharge={discharge_w}W "
                 f"actual_export={actual_export:.0f}W error={export_error:.0f}W "
                 f"P={export_error * Kp:.0f}W I={integral:.0f}W SOC={soc:.0f}%")

    def _bms_soc_monitor(self, kwargs):
        soc = self._f(self.s_soc, 0)
        if soc >= 99.0:
            today = datetime.now().strftime("%Y-%m-%d")
            last  = self.memory.get("last_full_charge_date")
            if last != today:
                self.memory["last_full_charge_date"] = today
                self._save()
                self.log(f"BMS monitor: full charge detected at {soc}% "
                         f"- timer reset to {today}")


    # -- AXLE ENERGY VPP INTEGRATION --------------------------

    def _poll_axle_api(self, kwargs=None):
        """
        Read Axle VPP event from sensor.axle_vpp_event — populated by HA REST sensor
        in configuration.yaml. HA core has full network access; AppDaemon does not.
        Runs every 10 minutes to check for new/changed events.
        """
        state = self.get_state("sensor.axle_vpp_event")
        if state in (None, "unknown", "unavailable", ""):
            self.log("Axle: sensor.axle_vpp_event unavailable — check HA REST sensor config", level="WARNING")
            return

        start_str = self.get_state("sensor.axle_vpp_event", attribute="start_time")
        end_str   = self.get_state("sensor.axle_vpp_event", attribute="end_time")
        direction = self.get_state("sensor.axle_vpp_event", attribute="import_export") or "export"

        self.log(f"Axle: sensor state={state} start={start_str} end={end_str}", level="DEBUG")

        data = {
            "start_time":    start_str,
            "end_time":      end_str,
            "import_export": direction,
        }
        self._axle_handle_response(data)

    def _axle_handle_response(self, data):
        """Process Axle API response — called from poll thread."""
        try:
            start_str  = data.get("start_time")
            end_str    = data.get("end_time")
            direction  = data.get("import_export", "export")

            if start_str and end_str:
                event = {
                    "start_time":    start_str,
                    "end_time":      end_str,
                    "import_export": direction,
                    "pence_per_kwh": self.axle_pence_per_kwh,
                    "fetched_at":    datetime.now().isoformat(),
                }
                existing = self.memory.get("axle_next_event", {})

                # Only act on genuinely new events
                if event["start_time"] != existing.get("start_time"):
                    self.log(f"Axle API: new event {direction} {start_str} -> {end_str}")
                    self.memory["axle_next_event"] = event

                    # Add to history if not already present
                    self._axle_add_history(event)

                    # Send advance notification if event is in the future
                    self._axle_notify_upcoming(event)

                    # Schedule the event start/end handlers
                    self._axle_schedule_event(event)

                else:
                    self.log(f"Axle API: same event {start_str} — no change", level="DEBUG")

            else:
                # No event scheduled
                if self.memory.get("axle_next_event"):
                    self.log("Axle API: no upcoming event")
                    self.memory["axle_next_event"] = {}

            self._axle_publish_sensor()
            self._save()

        except Exception as e:
            self.log(f"Axle handle response error: {e}", level="WARNING")

    def _axle_notify_upcoming(self, event):
        """Send push notification for a new upcoming event, once per event."""
        notified = self.memory.get("axle_event_notified", "")
        if notified == event["start_time"]:
            return  # already notified for this event

        try:
            start_dt = datetime.fromisoformat(event["start_time"].replace("Z", "+00:00"))
            end_dt   = datetime.fromisoformat(event["end_time"].replace("Z", "+00:00"))
            now      = datetime.now(start_dt.tzinfo)

            if start_dt <= now:
                return  # event already started, no pre-notification needed

            mins_until = int((start_dt - now).total_seconds() / 60)
            duration   = int((end_dt - start_dt).total_seconds() / 60)
            start_local = start_dt.strftime("%H:%M")
            end_local   = end_dt.strftime("%H:%M")

            self.call_service("notify/notify",
                title=f"PRISM: Axle export event in {mins_until} mins",
                message=(f"Export {start_local}–{end_local} ({duration} mins) | "
                         f"{event['pence_per_kwh']}p/kWh | "
                         f"SOC now: {self._f(self.s_soc, 0):.0f}%"))
            self.memory["axle_event_notified"] = event["start_time"]
            self.log(f"Axle: upcoming event notification sent — {mins_until} mins away")

        except Exception as e:
            self.log(f"Axle notify error: {e}", level="WARNING")

    def _axle_schedule_event(self, event):
        """Schedule run_at callbacks for event start and end."""
        try:
            start_dt = datetime.fromisoformat(event["start_time"].replace("Z", "+00:00"))
            end_dt   = datetime.fromisoformat(event["end_time"].replace("Z", "+00:00"))
            now      = datetime.now(start_dt.tzinfo)

            # Schedule start if still in the future
            if start_dt > now:
                delay_s = int((start_dt - now).total_seconds())
                self.run_in(self._axle_event_start, delay_s, event=event)
                self.log(f"Axle: event start scheduled in {delay_s}s ({start_dt.strftime('%H:%M')})")

            # Schedule end if still in the future
            if end_dt > now:
                delay_s = int((end_dt - now).total_seconds())
                self.run_in(self._axle_event_end, delay_s, event=event)
                self.log(f"Axle: event end scheduled in {delay_s}s ({end_dt.strftime('%H:%M')})")

        except Exception as e:
            self.log(f"Axle schedule error: {e}", level="WARNING")

    def _axle_event_start(self, kwargs):
        """Called at event start time — switch inverter to Timed Export."""
        event = kwargs.get("event", {})
        soc   = self._f(self.s_soc, 0)
        self.log(f"Axle: export event starting — SOC={soc:.0f}%", level="WARNING")

        # Switch inverter to Timed Export
        self.call_service("select/select_option",
            entity_id=self.e_mode, option="Timed Export")

        # Set discharge rate to full — PI controller will regulate from here
        self.call_service("number/set_value",
            entity_id=self.e_discharge_rate,
            value=int(self.inverter_limit_kw * 1000))

        # Flag export as PRISM-initiated so watchdog picks it up correctly
        self.memory["export_managing"]   = True
        self.memory["export_start_soc"]  = round(soc, 1)
        self.memory["export_start_time"] = datetime.now().isoformat()
        self.memory["export_integral"]   = 0
        self._save()

        try:
            end_str = event.get("end_time", "")
            end_local = ""
            if end_str:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                end_local = end_dt.strftime("%H:%M")
            self.call_service("notify/notify",
                title="PRISM: Axle export event started",
                message=(f"Exporting until {end_local} | "
                         f"{event.get('pence_per_kwh', self.axle_pence_per_kwh)}p/kWh | "
                         f"SOC: {soc:.0f}%"))
        except Exception as e:
            self.log(f"Axle start notify: {e}", level="WARNING")

        self._axle_publish_sensor()

    def _axle_event_end(self, kwargs):
        """Called at event end time — return inverter to Eco."""
        event   = kwargs.get("event", {})
        soc     = self._f(self.s_soc, 0)
        start_soc = self.memory.get("export_start_soc", soc)
        start_time = self.memory.get("export_start_time", "")
        duration_mins = 0
        if start_time:
            try:
                start_dt = datetime.fromisoformat(start_time)
                duration_mins = int((datetime.now() - start_dt).total_seconds() / 60)
            except Exception:
                pass

        battery_used = round(max(0, start_soc - soc) * self.battery_capacity_kwh / 100, 2)
        estimated_earnings = round(battery_used * event.get("pence_per_kwh", self.axle_pence_per_kwh) / 100, 2)

        self.log(f"Axle: export event ended — {duration_mins} mins | "
                 f"{battery_used:.1f}kWh used | £{estimated_earnings:.2f} earned")

        # Return to Eco
        self.call_service("select/select_option",
            entity_id=self.e_mode, option="Eco")
        self.call_service("number/set_value",
            entity_id=self.e_discharge_rate, value=5000)

        self.memory["export_managing"]   = False
        self.memory["export_integral"]   = 0
        self.memory["export_correction"] = 0
        self.memory["axle_next_event"]   = {}
        self._save()

        try:
            self.call_service("notify/notify",
                title="PRISM: Axle export event ended",
                message=(f"Duration: {duration_mins} mins | "
                         f"Battery: {start_soc:.0f}% → {soc:.0f}% | "
                         f"Used: {battery_used:.1f}kWh | "
                         f"Est. earned: £{estimated_earnings:.2f}"))
        except Exception as e:
            self.log(f"Axle end notify: {e}", level="WARNING")

        self._axle_publish_sensor()

    def _axle_add_history(self, event):
        """Add event to 7-day rolling history, deduplicated by start_time."""
        history = self.memory.get("axle_event_history", [])
        cutoff  = (datetime.now() - timedelta(days=7)).isoformat()

        # Prune old events
        history = [h for h in history if h.get("start_time", "") > cutoff]

        # Add if not already present
        if not any(h["start_time"] == event["start_time"] for h in history):
            history.append(event)
            self.log(f"Axle: event added to history ({len(history)} total)")

        self.memory["axle_event_history"] = history

    def _axle_headroom_needed(self):
        """
        Return extra SOC% to reserve for an upcoming Axle export event today/tonight.
        If an event is scheduled within the next 24h, reserve enough battery headroom
        to export for the full event duration at the export limit.
        Returns 0.0 if no event scheduled or event already passed.
        """
        event = self.memory.get("axle_next_event", {})
        if not event or not event.get("start_time"):
            return 0.0
        try:
            start_dt = datetime.fromisoformat(event["start_time"].replace("Z", "+00:00"))
            end_dt   = datetime.fromisoformat(event["end_time"].replace("Z", "+00:00"))
            now      = datetime.now(start_dt.tzinfo)

            # Only reserve headroom for events within next 24h
            if start_dt < now or (start_dt - now).total_seconds() > 86400:
                return 0.0

            # kWh needed = export_limit * duration_hours
            duration_h = (end_dt - start_dt).total_seconds() / 3600
            kwh_needed = self.export_limit_kw * duration_h
            soc_needed = round((kwh_needed / self.battery_capacity_kwh) * 100, 1)
            self.log(f"Axle headroom: event {start_dt.strftime('%H:%M')} duration={duration_h:.1f}h "
                     f"kwh={kwh_needed:.1f} soc_reserve={soc_needed:.1f}%")
            return soc_needed

        except Exception as e:
            self.log(f"Axle headroom calc error: {e}", level="WARNING")
            return 0.0

    def _axle_publish_sensor(self):
        """Publish sensor.prism_axle_next_event to HA."""
        event   = self.memory.get("axle_next_event", {})
        history = self.memory.get("axle_event_history", [])
        active  = self.memory.get("export_managing", False)

        if event and event.get("start_time"):
            try:
                start_dt  = datetime.fromisoformat(event["start_time"].replace("Z", "+00:00"))
                end_dt    = datetime.fromisoformat(event["end_time"].replace("Z", "+00:00"))
                now       = datetime.now(start_dt.tzinfo)
                mins_away = max(0, int((start_dt - now).total_seconds() / 60))
                duration  = int((end_dt - start_dt).total_seconds() / 60)
                state = "ACTIVE" if active else ("UPCOMING" if start_dt > now else "PAST")
                start_local = start_dt.strftime("%H:%M")
                end_local   = end_dt.strftime("%H:%M")
            except Exception:
                state = "UNKNOWN"
                mins_away = 0
                duration  = 0
                start_local = event.get("start_time", "")
                end_local   = event.get("end_time", "")
        else:
            state       = "NONE"
            mins_away   = 0
            duration    = 0
            start_local = ""
            end_local   = ""

        self.set_state("sensor.prism_axle_next_event", state=state, attributes={
            "start_time":    event.get("start_time", ""),
            "end_time":      event.get("end_time", ""),
            "start_local":   start_local,
            "end_local":     end_local,
            "duration_mins": duration,
            "mins_until":    mins_away,
            "pence_per_kwh": event.get("pence_per_kwh", self.axle_pence_per_kwh),
            "import_export": event.get("import_export", "export"),
            "event_history": history[-10:],  # last 10 events
            "friendly_name": "PRISM Axle Next Event"
        })

    # -- GROWATT BOOTSTRAP ------------------------------------

    # ITEM 4/5: Derive correction factors from historicals; age-weighted decay
    def attempt_growatt_bootstrap(self, kwargs):
        if not GROWATT_AVAILABLE:
            self.log("Growatt bootstrap: growattServer not available - skipping",
                     level="DEBUG")
            return
        if len(self.memory.get("solar_corrections", {})) >= 6:
            self.log("Growatt bootstrap: sufficient data - skipping")
            return
        # Skip permanently if API was already found to be unavailable
        if self.memory.get("growatt_bootstrap_api_unavailable", False):
            self.log("Growatt bootstrap: API previously returned 403 - skipping "
                     "(set growatt_bootstrap_api_unavailable=false in memory to retry)",
                     level="DEBUG")
            return
        username = self.args.get("growatt_username", "")
        password = self.args.get("growatt_password", "")
        plant_id = self.args.get("growatt_plant_id", "")
        if not username or not password or not plant_id:
            self.log("Growatt bootstrap: credentials not configured - skipping",
                     level="DEBUG")
            return
        try:
            api = growattServer.GrowattApi()
            api.login(username, password)
            monthly = {}
            month_ages = {}  # track age in months for decay
            for m in range(12):
                target = datetime.now() - timedelta(days=m * 30)
                ms     = target.strftime("%Y-%m")
                mk     = str(target.month)
                try:
                    data = api.plant_detail(plant_id, 2, ms)
                    if data and "datas" in data:
                        total = sum(float(d.get("epvtotal", 0))
                                    for d in data["datas"] if d.get("epvtotal"))
                        monthly.setdefault(mk, []).append(total)
                        month_ages[mk] = m  # 0 = current month, older = higher
                        self.log(f"Growatt bootstrap: {ms}={total:.1f}kWh")
                except Exception as e:
                    self.log(f"Growatt bootstrap {ms}: {e}", level="WARNING")

            if monthly:
                self.memory["growatt_monthly_totals"] = monthly

                # ITEM 4: Derive correction factors by comparing to MONTHLY_SOLAR_PRIOR
                # ITEM 5: Age-weight corrections — decay toward 1.0 for older months
                corrections = self.memory.get("solar_corrections", {})
                for mk, totals in monthly.items():
                    if mk in corrections:
                        continue  # Don't override live-learned corrections
                    prior = MONTHLY_SOLAR_PRIOR.get(int(mk), 0)
                    if prior <= 0:
                        continue
                    # Use most recent reading for this month
                    actual = totals[-1] if totals else 0
                    if actual <= 0:
                        continue
                    # Days in month approximation: scale Growatt monthly to daily average
                    # then compare to prior daily average
                    raw_correction = actual / max(prior * 30, 1)
                    raw_correction = max(0.5, min(2.0, raw_correction))

                    # ITEM 5: Age decay toward 1.0
                    age_months = month_ages.get(mk, 0)
                    decay = max(0.0, 1.0 - age_months * 0.08)  # 8% decay per month
                    blended_correction = round(raw_correction * decay + 1.0 * (1 - decay), 3)

                    corrections[mk] = blended_correction
                    self.log(f"Growatt bootstrap correction m{mk}: "
                             f"actual={actual:.0f} prior*30={prior*30:.0f} "
                             f"raw={raw_correction:.3f} age={age_months}mo "
                             f"-> {blended_correction:.3f}")

                self.memory["solar_corrections"] = corrections
                self._save()
                self.log(f"Growatt bootstrap: {len(monthly)} months stored, "
                         f"{len(corrections)} corrections derived")
        except Exception as e:
            err_str = str(e)
            if "403" in err_str or "Forbidden" in err_str or "401" in err_str:
                self.log("Growatt bootstrap: API returned 403/Forbidden — "
                         "newTwoLoginAPI.do is deprecated. Bootstrap disabled permanently. "
                         "Remove growatt_username/password from apps.yaml to suppress this.",
                         level="WARNING")
                self.memory["growatt_bootstrap_api_unavailable"] = True
                self._save()
            else:
                self.log(f"Growatt bootstrap failed: {e}", level="WARNING")

    # -- MANUAL TRIGGER HANDLER -------------------------------

    # ITEM 21: force_charge / force_no_charge manual overrides (item 54)
    def _handle_trigger_event(self, event_name, data, kwargs):
        action = data.get("action", "")
        self.log(f"Manual trigger received: action={action}")
        if action == "midday_recalculation":
            self.midday_recalculation({})
        elif action == "startup_check":
            self.startup_check({})
        elif action == "health_check":
            self._run_health_check({})
        elif action == "force_charge":
            target = data.get("target_soc", 100)
            self.log(f"Manual force_charge: target={target}%", level="WARNING")
            self._apply_decision(True, target, self.charge_power_kw)
            try:
                self.call_service("notify/notify",
                    title="PRISM: Manual charge override",
                    message=f"force_charge applied. Target={target}%")
            except Exception:
                pass
        elif action == "force_no_charge":
            self.log("Manual force_no_charge applied", level="WARNING")
            self._apply_decision(False, 0, self.charge_power_kw)
            try:
                self.call_service("notify/notify",
                    title="PRISM: Manual no-charge override",
                    message="force_no_charge applied. Charge schedule disabled.")
            except Exception:
                pass
        elif action == "overnight_decision":
            self.overnight_charge_decision({})
        elif action == "axle_poll":
            self._poll_axle_api({})
        else:
            self.log(f"Unknown trigger action: {action}", level="WARNING")

    # -- MEMORY -----------------------------------------------

    def _load_memory(self):
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE) as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception as e:
                self.log(f"Memory load error: {e}", level="ERROR")
                try:
                    backup = f"{MEMORY_FILE}.corrupted.{datetime.now():%Y%m%d_%H%M%S}"
                    os.rename(MEMORY_FILE, backup)
                    self.log(f"Corrupted memory saved as: {backup}", level="WARNING")
                except Exception as be:
                    self.log(f"Memory backup failed: {be}", level="ERROR")
        self.log("Starting with fresh memory", level="WARNING")
        return {
            "observations":                      [],
            "daily_loads":                       {},
            "solar_corrections":                 {},
            "solar_corrections_by_weather":      {},
            "solar_corrections_weather_samples": {},
            "decision_history":                  [],
            "accuracy_score":                    0,
            "last_error":                        0,
            "last_charge_decision":              None,
            "last_observation_date":             None,
            "last_full_charge_date":             None,
            "live_state":                        {},
            "export_managing":                   False,
            "export_correction":                 0,
            "export_integral":                   0,
            "export_start_soc":                  0,
            "export_start_time":                 "",
            "last_health_notification_date":     "",
            "last_health_fault_date":            "",
            "growatt_monthly_totals":            {},
            "schema_version":                    1,
            "axle_next_event":                   {},
            "cheap_rate_start":                  2,
            "cheap_rate_end":                    5,
            "cheap_rate_detected_at":            "",
            "cheap_rate_p_per_kwh":              0.0,
            "axle_event_history":                [],
            "axle_event_notified":               "",
        }

    def _save(self):
        # Atomic write: temp file + fsync + rename prevents corruption on crash
        tmp = MEMORY_FILE + ".tmp"
        try:
            self.memory["schema_version"] = 1
            with open(tmp, "w") as f:
                json.dump(self.memory, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, MEMORY_FILE)
        except Exception as e:
            self.log(f"Memory save error: {e}", level="ERROR")
            try:
                os.remove(tmp)
            except Exception:
                pass

    # -- HELPERS ----------------------------------------------

    def _f(self, entity_id, default=0.0, level="DEBUG"):
        try:
            s = self.get_state(entity_id)
            return float(s) if s not in (None, "unknown", "unavailable", "") \
                   else default
        except Exception as e:
            self.log(f"_f({entity_id}): {e}", level=level)
            return default

    def _get_state(self, entity_id, default="unknown"):
        try:
            s = self.get_state(entity_id)
            return s if s not in (None, "") else default
        except Exception:
            return default
