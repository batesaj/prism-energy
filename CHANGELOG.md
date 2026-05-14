# CHANGELOG

## PRISM v1.0 — May 2026

PRISM v1.0 is a major evolution from AXLE v3.7, with a full rename, inverter abstraction layer, and significant model improvements.

### Renamed
- Class `AxleV3Engine` → `PrismEngine`
- App module `axle_v3` → `prism`
- Memory file `axle_memory.json` → `prism_memory.json`
- All virtual sensors `sensor.axle_*` → `sensor.prism_*`
- Event listener `axle_trigger` → `prism_trigger`

### New — Model correctness
- **Discharge efficiency 0.94** applied in simulation when net is negative
- **Blended solar correction** — 0→100% blend over 10 local month observations, preventing over-correction on sparse data
- **Separate forecast helpers** — `_solar_forecast_tomorrow()` for overnight decisions, `_solar_forecast_today_remaining()` for midday recalculation (trims to current hour)
- **Growatt bootstrap** — derives correction factors from historical monthly totals vs `MONTHLY_SOLAR_PRIOR`, with age-decay toward 1.0 at 8%/month
- **Simulation keys renamed** — `pv_kw/load_kw/net_kw` → `pv_kwh/load_kwh/net_kwh` with backwards-compatible curve publishing

### New — Intelligence
- **Exponential decay bias** — decision bias uses `[1,2,4,8,16]` weights favouring recent history over simple average
- **Dynamic charge buffer** — `buffer = 5 + (10 × (1 - confidence)) + max(0, error_trend × 2)` replacing fixed 5% constant
- **`sensor.prism_mae_7d`** — rolling 7-day mean absolute error published after every validation
- **`sensor.prism_confidence_trend`** — 7-day rolling accuracy % refreshed after every validation close

### New — Sensors
- **`sensor.prism_evening_min_soc_actual`** — tracks rolling minimum SOC from 17:00, resets daily
- **`sensor.prism_savings_today`** — estimated savings vs full grid import, updated hourly
- **`sensor.prism_mae_7d`** — 7-day MAE
- **`sensor.prism_confidence_trend`** — 7-day accuracy trend

### New — Operations
- **Manual overrides** — `force_charge` and `force_no_charge` events via dashboard or Developer Tools
- **`overnight_decision` trigger** — run the 01:30 decision manually at any time
- **Config validation** on startup — logs ERROR for each misconfigured parameter
- **Forecast freshness check** in daily health check — validates Solcast `detailedForecast` has ≥24 intervals
- **Startup vs scheduled health check** — startup check (45s) skips time-sensitive checks; full 10-check suite runs at 08:05
- **Growatt 403 suppression** — permanently skips bootstrap after detecting deprecated API endpoint, logs once

### New — Inverter abstraction (items 23/24)
- `_load_sensor_map()` builds `self.s_*` (9 read sensors) and `self.e_*` (7 controls) from `inverter_prefix`
- Any individual entity overridable via `apps.yaml` sensor map
- All hardware calls in codebase use abstracted references

### New — Export manager
- PI controller replacing pure proportional: `Kp=0.3`, `Ki=0.05`, anti-windup clamp ±1500W
- Integral term persisted in memory, reset on export event end

---

## AXLE v3.7 — April/May 2026 (patch series)

Production-hardening patch series applied to AXLE v3.6.

- **PATCH 1:** Solcast hourly parsing — dict access fix in `_simulate()`
- **PATCH 2:** Double correction fix — removed correction from Solcast path in `_simulate()`
- **PATCH 3:** Validation date fix — added `tomorrow` to `self_validate()` date check
- **PATCH 4:** Export state recovery — added `export_power > 100` check on restart
- **PATCH 5:** `record_daily_observation()` — uses observation date month, not `datetime.now().month`
- **PATCH 6:** `_get_shift_type()` — consistent `BANK_HOLIDAY` logic for today and tomorrow
- **PATCH 7:** Empty list guard in `_classify_weather()` — enforced before all list operations
- **PATCH 8:** Solcast partial data — coverage ratio scaling if < 48 intervals
- **PATCH 9:** Memory corruption backup — renames corrupted file before fresh start
- **PATCH 10:** Month key standardisation — always `str(month)` everywhere
- **PATCH 11:** Weather classification — minimum precipitation threshold (1.0mm) for RAINY
- **PATCH 12:** Health check `_chk` — consistent error handling
- **PATCH 13:** `_apply_weather_correction()` — only logs when switching correction type
- **PATCH 14:** `_calculate_confidence()` — extracted as named helper
- **PATCH 15:** `_f()` — added `level` parameter for WARNING vs DEBUG

---

## AXLE v3.6 — March 2026

- Weather classification: CLEAR / PARTLY_CLOUDY / OVERCAST / RAINY / STORMY
- Per-weather-class solar correction factors (minimum 6 samples before activation)
- Precipitation probability and rainfall thresholds
- Open-Meteo REST sensor upgraded to include precipitation fields
- Export window sensors updated to include Timed Export mode
- Solcast tomorrow capped zero-division guard
- Net Energy Tomorrow sensor fix

---

## AXLE v3.5 — February 2026

- Seasonal prior blending — p50 blended with `MONTHLY_SOLAR_PRIOR` when < 5 month observations
- Confidence-weighted SOC floor adjustment
- Rolling decision bias engine (±5% cap, correction-deviation aware)
- BMS balance trigger — forced 100% every N days for cell equalisation
- Export event start/end push notifications
- PI-style export discharge controller (proportional only in v3.5)
- `FIX 21`: Re-simulation with actual charge decision before storing
- `FIX 22`: `deepcopy` before storing simulation curve
- `FIX 25`: Extracted `_publish_preview_sensors()` helper

---

## AXLE v3.4 — January 2026

- Three-scenario simulation: p10 / p50 / p90 from Solcast detailedHourly
- Evening minimum SOC target (h17+) replacing whole-day minimum
- SOC taper above 85% during charging
- Charge efficiency 0.93 in simulation
- Winter full-charge trigger below solar threshold in winter months
- Midday recalculation preview (08:00, 12:00, 16:00) — no inverter touch
- Weekly health check with push notification

---

## AXLE v3.3 — December 2025

- Solcast detailedHourly PV shape in simulation
- Open-Meteo physics model as Solcast fallback
- Monthly solar correction factors with exponential moving average
- Shift-pattern-aware hourly load weights
- Atomic memory save via `.tmp` + `os.replace()`

---

## AXLE v3.2 — November 2025

- Shift pattern integration (21-day cycle)
- Per-shift-type load history (`daily_loads`)
- Degree-day heating load adjustment
- Safe mode on unavailable critical sensors

---

## AXLE v3.1 — October 2025

- Initial AppDaemon architecture
- Overnight charge decision at 01:30
- Daily observation recording at 23:50
- Self-validation at 23:55
- Basic Solcast p50 forecast
- GivEnergy inverter schedule control
