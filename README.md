# BJC Newsletter — Home Assistant Integration

A custom Home Assistant integration that automatically fetches the weekly [Boca Jewish Center](https://www.bocajewishcenter.org/) newsletter, extracts the full daily schedule using Google Gemini AI, and exposes it as sensors for your dashboard.

## Features

- Polls the BJC website **every hour** for a new newsletter
- Detects changes automatically — only processes when a new edition is published (~weekly)
- Compresses the newsletter PDF (40+ MB → ~4 MB) before sending to Gemini
- Extracts the complete day-by-day schedule including prayer times, classes, and events
- Excludes Yahrzeit notices and sponsorship announcements
- Exposes **today's** and **tomorrow's** schedule as separate sensors
- Persists the schedule across Home Assistant restarts

## Sensors

| Entity | State | Attribute |
|--------|-------|-----------|
| `sensor.bjc_today_schedule` | Today's date (ISO) | `schedule` — full markdown for today |
| `sensor.bjc_tomorrow_schedule` | Tomorrow's date (ISO) | `schedule` — full markdown for tomorrow |
| `sensor.bjc_newsletter_status` | `ready` / `processing` / `error` | `newsletter_url`, `last_processed`, `last_checked`, `last_error` |

## Requirements

- Home Assistant 2024.1.0 or newer
- A [Google Gemini API key](https://aistudio.google.com/apikey) (paid tier recommended for production use)

## Installation via HACS

1. In HACS, go to **Integrations** → click the three dots menu → **Custom repositories**
2. Add `https://github.com/Daniellamm/ha-bjc-newsletter` as an **Integration**
3. Search for **BJC Newsletter** and install it
4. Restart Home Assistant
5. Go to **Settings → Devices & Services → Add Integration** and search for **BJC Newsletter**
6. Enter your Gemini API key when prompted

## Manual Installation

1. Copy the `custom_components/bjc_newsletter/` folder into your HA config's `custom_components/` directory
2. Restart Home Assistant
3. Go to **Settings → Devices & Services → Add Integration → BJC Newsletter**
4. Enter your Gemini API key

## Dashboard Usage

Add a **Markdown card** to display today's schedule:

```yaml
type: markdown
content: >
  {{ state_attr('sensor.bjc_today_schedule', 'schedule') | default('No schedule available') }}
```

For a two-card layout showing today and tomorrow:

```yaml
type: vertical-stack
cards:
  - type: markdown
    title: Today's Schedule
    content: >
      {{ state_attr('sensor.bjc_today_schedule', 'schedule') | default('No schedule available') }}
  - type: markdown
    title: Tomorrow's Schedule
    content: >
      {{ state_attr('sensor.bjc_tomorrow_schedule', 'schedule') | default('No schedule available') }}
```

## Configuration

During setup you will be asked for:

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| Gemini API Key | Yes | — | Your Google Gemini API key |
| Gemini Model | No | `gemini-2.5-flash` | Model to use for extraction |

## How It Works

1. Every hour, the integration scrapes the BJC homepage for the current newsletter link
2. If the link is unchanged from last time, nothing happens
3. If a new newsletter is detected, the PDF is downloaded and compressed using `pikepdf`
4. The compressed PDF is uploaded to the Gemini Files API
5. Gemini reads the schedule section and outputs structured markdown organized by day
6. The schedule is parsed into per-date entries and cached locally (`bjc_newsletter_cache.json` in your HA config directory)
7. The sensors update to reflect today's and tomorrow's entries

## Troubleshooting

- Check `sensor.bjc_newsletter_status` — if `last_error` is set it will describe what failed
- The cache file at `<config>/bjc_newsletter_cache.json` can be deleted to force a full reprocess on next poll
- To force an immediate reprocess, go to **Settings → Devices & Services → BJC Newsletter → Configure** and re-enter your API key

## License

MIT
