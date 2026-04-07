# BJC Newsletter — Home Assistant Integration

A custom Home Assistant integration that automatically fetches the weekly [Boca Jewish Center](https://www.bocajewishcenter.org/) newsletter from Flipsnack, extracts the full daily schedule using Google Gemini AI, and exposes it as sensors for your dashboard.

## Features

- Polls the BJC website **every hour** for a new newsletter edition
- Detects changes automatically — only processes when a new edition is published (~weekly)
- Uses a **real headless browser** (Playwright + Chromium) to download the newsletter PDF, bypassing all CDN restrictions
- Compresses page images before sending to Gemini (~4 MB)
- Extracts the complete day-by-day schedule including prayer times, classes, and events
- Excludes Yahrzeit notices, sponsorships, and advertisements
- Exposes **today's** and **tomorrow's** schedule as separate sensors
- Persists the schedule across Home Assistant restarts

## Sensors

| Entity | State | Attributes |
|--------|-------|------------|
| `sensor.bjc_today_schedule` | Today's date (ISO) | `schedule` — full markdown for today |
| `sensor.bjc_tomorrow_schedule` | Tomorrow's date (ISO) | `schedule` — full markdown for tomorrow |
| `sensor.bjc_newsletter_status` | `ready` / `processing` / `error` / `idle` | `newsletter_url`, `last_processed`, `last_checked`, `last_error` |

## Requirements

- Home Assistant 2024.1.0 or newer
- A [Google Gemini API key](https://aistudio.google.com/apikey) (paid tier recommended for reliable production use)

## Installation via HACS

1. In HACS, go to **Integrations** → click the three-dot menu → **Custom repositories**
2. Add `https://github.com/Daniellamm/ha-bjc-newsletter` as an **Integration**
3. Search for **BJC Newsletter** and install it
4. Restart Home Assistant
5. Go to **Settings → Devices & Services → Add Integration** and search for **BJC Newsletter**
6. Enter your Gemini API key when prompted

> **Note:** The first time the integration fetches a newsletter, it will automatically download the Chromium browser (~100 MB). This takes about 1–2 minutes and only happens once. Home Assistant may appear to be processing for longer than usual on the very first run.

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
    title: "📅 Today's Schedule"
    content: >
      {{ state_attr('sensor.bjc_today_schedule', 'schedule') | default('No schedule available') }}
  - type: markdown
    title: "📅 Tomorrow's Schedule"
    content: >
      {{ state_attr('sensor.bjc_tomorrow_schedule', 'schedule') | default('No schedule available') }}
```

## Configuration

During setup you will be asked for:

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| Gemini API Key | Yes | — | Your Google Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey) |
| Gemini Model | No | `gemini-2.5-flash` | Model to use for schedule extraction |

To update the API key or model after setup, go to **Settings → Devices & Services → BJC Newsletter → Configure**.

## How It Works

1. Every hour, the integration scrapes the BJC homepage for the current newsletter Flipsnack URL
2. If the URL is unchanged from last time, nothing happens (no API calls are made)
3. When a new newsletter is detected:
   - A headless Chromium browser opens the Flipsnack page and captures signed CDN tokens
   - All newsletter pages are downloaded as images using those tokens
   - The images are assembled into a PDF (~4 MB)
   - The PDF is uploaded to the Google Gemini Files API
   - Gemini reads the schedule section and outputs structured markdown organized by day
   - The schedule is parsed into per-date entries and cached locally in your HA config
4. Sensors update to reflect today's and tomorrow's entries immediately

## Manual PDF Fallback

If automatic downloading ever fails, you can manually place the newsletter PDF in your HA config directory:

```
<HA config folder>/bjc_newsletter_pdfs/
```

The integration scans this folder every hour and automatically processes any PDF placed there. This folder is created automatically on first run.

To use this fallback:
1. Open the newsletter at [flipsnack.com/7BBDB688B7A](https://www.flipsnack.com/7BBDB688B7A/)
2. Click the Download button to save the PDF
3. Copy the PDF into `<config>/bjc_newsletter_pdfs/`
4. The integration will pick it up within the hour

## Troubleshooting

| Symptom | Solution |
|---------|----------|
| `sensor.bjc_newsletter_status` shows `error` | Check `last_error` attribute for details |
| Schedule not updating | Verify the Gemini API key is valid and has quota |
| "Playwright browser" errors in logs | The browser download may have failed — restart HA to retry |
| Wrong times / missing days | Delete `<config>/bjc_newsletter_cache.json` and restart HA to force reprocess |
| Want to force an immediate reprocess | Go to **Settings → Devices & Services → BJC Newsletter → Configure** and save |

Check HA logs under **Settings → System → Logs** and filter by `bjc_newsletter` for detailed diagnostics.

## License

MIT
