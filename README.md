<p align="center">
  <img src="assets/prism_main_logo.png" alt="PRISM" width="600"/>
</p>

# PRISM
### Predictive · Resilient · Intelligent · Solar · Manager

> A self-learning probabilistic energy management engine for Home Assistant, built for homes with solar generation and battery storage on Octopus Flux.

PRISM runs as an [AppDaemon](https://appdaemon.readthedocs.io/) application inside Home Assistant. Each night at 01:30 it analyses your Solcast solar forecast, learned correction factors, shift-pattern load prediction, weather classification, and decision history to decide whether — and how much — to charge your battery from the grid during the Octopus Flux cheap rate window (02:00-05:00).

The prism metaphor is intentional: one solar forecast enters, three probability rays emerge.

```
☀️  Solar forecast  ──▶  ◁ PRISM ▷  ──▶  p90  optimistic  🟢
                                     ──▶  p50  expected    🔵
                                     ──▶  p10  pessimistic 🟠
```

---

## Features

| | |
|---|---|
| **Probabilistic decisions** | Runs p10/p50/p90 simulations from Solcast detailedHourly data — protects against pessimistic outcomes without over-charging for optimistic ones |
| **Self-learning corrections** | Learns per-month and per-weather-class solar correction factors from daily observations, blended over 10+ readings |
| **Shift-pattern load model** | Predicts daily load from a configurable 21-day shift cycle (OFF / DAYS / LATES / BANK_HOLIDAY) with degree-day heating adjustment |
| **Weather classification** | Classifies each day as CLEAR / PARTLY_CLOUDY / OVERCAST / RAINY / STORMY from Open-Meteo data, with separate correction factors per class |
| **Exponential bias engine** | Tracks signed prediction errors with exponential decay weighting and applies a rolling SOC floor correction |
| **Dynamic charge buffer** | Safety buffer adjusts automatically based on forecast confidence and recent error trend |
| **Export event management** | PI controller manages battery discharge rate during Octopus export events, targeting the DNO export limit |
| **Health monitoring** | Daily health check across inverter sensors, Solcast freshness, and decision validity — notifies on fault |
| **Manual overrides** | `force_charge` / `force_no_charge` events from the dashboard for exceptional circumstances |
| **Inverter abstraction** | All hardware entity IDs configurable via `apps.yaml` — not hardcoded |

---

## Hardware requirements

PRISM was developed for and tested on the following hardware. It may work on similar setups with configuration changes.

| Component | Tested hardware |
|---|---|
| Inverter | GivEnergy AIO (AC-coupled) |
| Battery | 13 kWh LFP |
| Solar | Growatt string inverter, AC-coupled, independent of GivEnergy |
| Tariff | Octopus Flux (cheap 02:00-05:00, peak 16:00-19:00) |
| HA | HAOS VM, AppDaemon 4.5.x, HA Core 2026.x |

**Key constraint:** PRISM controls battery charge/discharge rate only. It cannot curtail solar generation — your AC-coupled solar inverter is entirely independent. The GivEnergy 4.5 kW DNO export limit is enforced and must not be removed.

---

## Dependencies

- **Home Assistant** with AppDaemon addon
- **[Solcast integration](https://github.com/BJReplay/ha-solcast-solar)** — provides p10/p50/p90 hourly forecast
- **[GivTCP / GivEnergy integration](https://github.com/britishgas-engineering/givenergy-local)** — inverter control
- **[Octopus Energy integration](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy)** — tariff rates
- **Open-Meteo** REST sensor — weather data (free, no API key, configured in `configuration.yaml`)
- **Workday binary sensor** — HA built-in, configured via UI for bank holiday awareness
- **growattServer** Python package — optional, for bootstrap of historical corrections

---

## Quick start

### 1. Install AppDaemon

Install the AppDaemon addon from the HA addon store. Ensure it can see your HA instance.

### 2. Copy files

```
/addon_configs/a0d7b954_appdaemon/apps/prism.py
/addon_configs/a0d7b954_appdaemon/apps/apps.yaml
/homeassistant/templates/energy_sensors.yaml
/homeassistant/configuration.yaml        (merge recorder + helpers sections)
/homeassistant/automations.yaml          (merge PRISM automations)
/homeassistant/scripts.yaml              (merge PRISM scripts)
/homeassistant/dashboards/prism_dashboard.yaml
```

### 3. Configure apps.yaml

Copy `apps.yaml.example` to `apps.yaml` and set at minimum:

```yaml
prism:
  module: prism
  class: PrismEngine
  inverter_prefix: "your_inverter_serial"    # GivEnergy entity prefix
  battery_capacity_kwh: 13.0
  num_panels_se: 10
  num_panels_nw: 10
  panel_rating_w: 440
  cheap_rate_start: 2
  cheap_rate_end: 5
  shift_cycle_ref: "2026-06-01"              # any Monday anchoring your cycle
```

### 4. Configure Open-Meteo

In `configuration.yaml`, replace `YOUR_LATITUDE` and `YOUR_LONGITUDE` with your location coordinates.

### 5. Create UI helpers

In HA Settings → Helpers, create two Number helpers:
- `input_number.prism_soc_forecast_error` (range -50 to 50, step 0.1)
- `input_number.prism_soc_accuracy_score` (range 0 to 100, step 0.1)

### 6. Configure dashboard

In `dashboard.yaml.example`, replace all `YOUR_INVERTER_SERIAL`, `YOUR_MPAN_*`, and `YOUR_GROWATT_SERIAL` placeholders with your actual entity IDs. Add the dashboard via HA Settings → Dashboards → Add Dashboard → YAML mode.

### 7. Restart AppDaemon

PRISM will start, validate config, load (or create) memory, and run its first startup check. The first overnight charge decision runs at 01:30.

---

## Shift pattern

PRISM uses a 21-day repeating shift cycle to predict daily load. Configure `shift_cycle_ref` to any Monday that anchors your pattern. The default pattern:

```
Day  0-2:  OFF          (home all day, ~13 kWh)
Day  3-5:  DAYS         (leaves early, returns evening, ~8.5 kWh)
Day  6:    SUNDAY_WORK  (~9 kWh)
Day  7-8:  LATES        (home morning, leaves midday, ~9.5 kWh)
Day  9-13: OFF
Day 14-16: DAYS
Day 17-19: LATES
Day 20:    OFF
```

Edit `SHIFT_PATTERN` and `SHIFT_LOAD_BOOTSTRAP` in `prism.py` to match your own pattern.

---

## Memory file

PRISM stores all learned state in `/homeassistant/prism_memory.json`. This file persists across restarts and contains:

- Daily observations (load, PV, weather, shift type)
- Per-month and per-weather-class solar correction factors
- Decision history with validation closure (last 60 decisions)
- Accuracy and MAE scores
- Export event state

Back this file up periodically. If migrating from AXLE, simply copy `axle_memory.json` to `prism_memory.json` — the format is identical.

---

## Sensors published

PRISM publishes the following sensors to Home Assistant:

| Sensor | Description |
|---|---|
| `sensor.prism_status` | Engine state and metadata |
| `sensor.prism_charge_decision` | Tonight's charge target % and full decision detail |
| `sensor.prism_health_status` | Health check result and fault list |
| `sensor.prism_shift_today` | Shift type for today |
| `sensor.prism_solar_correction_factor` | Current month's learned correction |
| `sensor.prism_mae_7d` | 7-day mean absolute error % |
| `sensor.prism_confidence_trend` | 7-day rolling accuracy % |
| `sensor.prism_savings_today` | Estimated savings vs full grid import (GBP) |
| `sensor.prism_evening_min_soc_actual` | Rolling minimum SOC from 17:00 (resets daily) |
| `sensor.prism_soc_simulation_curve` | Full 24-hour SOC simulation with solar and load curves |
| `sensor.prism_tomorrow_solar_p10/p50/p90` | Probabilistic solar forecasts |
| `sensor.prism_tomorrow_eve_p10/p50/p90` | Predicted evening minimum SOC |
| `sensor.prism_energy_deficit_kwh` | Predicted energy deficit tomorrow |

---

## Manual triggers

Fire a `prism_trigger` event from the dashboard or Developer Tools:

```yaml
event_type: prism_trigger
event_data:
  action: force_charge        # or force_no_charge, midday_recalculation,
  target_soc: 100             # health_check, startup_check, overnight_decision
```

---

## Architecture

See [docs/architecture.md](docs/architecture.md) for a full description of the probabilistic simulation engine, weather correction system, bias engine, and decision logic.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the full evolution from AXLE v3.1 to PRISM v1.0.

---

## Licence

MIT — see [LICENSE](LICENSE)
