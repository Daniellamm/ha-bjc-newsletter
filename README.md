# BJC Newsletter — Home Assistant Integration

A custom Home Assistant integration that automatically fetches the weekly [Boca Jewish Center](https://www.bocajewishcenter.org/) newsletter from Flipsnack, extracts the full daily schedule using Google Gemini AI, and exposes it as sensors for your dashboard.

## Features

- Polls the BJC website **every hour** for a new newsletter edition
- Detects changes automatically — only processes when a new edition is published (~weekly)
- Uses **Browserbase** (a cloud browser service) to download the newsletter PDF — works on **HA Green** and all other Home Assistant installations with no add-ons required
- Assembles the newsletter pages into a PDF and sends it to Google Gemini AI
- Extracts the complete day-by-day schedule including prayer times, classes, and events
- Excludes Yahrzeit notices, sponsorships, and advertisements
- Exposes **today's** and **tomorrow's** schedule as separate sensors
- Falls back to a manual PDF watch folder if Browserbase is not configured
- Persists the schedule across Home Assistant restarts

## Sensors

| Entity | State | Attributes |
|--------|-------|------------|
| `sensor.bjc_today_schedule` | Today's date (ISO) | `schedule` — full markdown for today |
| `sensor.bjc_tomorrow_schedule` | Tomorrow's date (ISO) | `schedule` — full markdown for tomorrow |
| `sensor.bjc_newsletter_status` | `ready` / `processing` / `error` / `idle` | `newsletter_url`, `last_processed`, `last_checked`, `last_error` |

## Requirements

- Home Assistant 2024.1.0 or newer
- A [Google Gemini API key](https://aistudio.google.com/apikey)
- A [Browserbase account](https://www.browserbase.com) (free, no credit card required) for automatic PDF fetching

## Installation via HACS

1. In HACS, go to **Integrations** → click the three-dot menu → **Custom repositories**
2. Add `https://github.com/Daniellamm/ha-bjc-newsletter` as an **Integration**
3. Search for **BJC Newsletter** and install it
4. Restart Home Assistant
5. Go to **Settings → Devices & Services → Add Integration** and search for **BJC Newsletter**
6. Enter your Gemini API key when prompted
7. After setup, go to **Configure** and add your Browserbase credentials (see below)

## Manual Installation

1. Copy the `custom_components/bjc_newsletter/` folder into your HA config's `custom_components/` directory
2. Restart Home Assistant
3. Go to **Settings → Devices & Services → Add Integration → BJC Newsletter**
4. Enter your Gemini API key
5. After setup, go to **Configure** and add your Browserbase credentials (see below)

## Browserbase Setup (Required for Automatic Fetching)

Browserbase runs a real Chromium browser in their cloud. The integration connects to it remotely — no browser binary is installed on your Home Assistant device.

**Free tier: 1 browser hour/month. The integration uses ~3 minutes/month (4 newsletters × 45 sec). No credit card required.**

1. Go to [browserbase.com](https://www.browserbase.com) and sign up for a free account
2. From your dashboard, copy your **API Key** and **Project ID**
3. In Home Assistant: **Settings → Devices & Services → BJC Newsletter → Configure**
4. Paste both values into the Browserbase fields and save

That's it — the integration will use Browserbase automatically whenever a new newsletter is detected.

## Configuration

During initial setup you will be asked for:

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| Gemini API Key | Yes | — | Your Google Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey) |
| Gemini Model | No | `gemini-2.5-flash` | Model to use for schedule extraction |

After setup, go to **Configure** to also set:

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| Browserbase API Key | No | — | Enables automatic PDF fetching via cloud browser |
| Browserbase Project ID | No | — | Your Browserbase project ID |

To update any setting, go to **Settings → Devices & Services → BJC Newsletter → Configure**.

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
    title: "Today's Schedule"
    content: >
      {{ state_attr('sensor.bjc_today_schedule', 'schedule') | default('No schedule available') }}
  - type: markdown
    title: "Tomorrow's Schedule"
    content: >
      {{ state_attr('sensor.bjc_tomorrow_schedule', 'schedule') | default('No schedule available') }}
```

## How It Works

1. Every hour, the integration scrapes the BJC homepage for the current newsletter Flipsnack URL
2. If the URL is unchanged from last time, nothing happens (no API calls are made)
3. When a new newsletter is detected:
   - Browserbase opens the Flipsnack page in a cloud Chromium browser and captures signed CDN tokens
   - All newsletter pages are downloaded as images using those tokens
   - The images are assembled into a PDF (~4 MB)
   - The PDF is uploaded to the Google Gemini Files API
   - Gemini reads the schedule section and outputs structured markdown organized by day
   - The schedule is parsed into per-date entries and cached locally in your HA config
4. Sensors update to reflect today's and tomorrow's entries immediately

## Manual PDF Fallback

If Browserbase is not configured, you can manually place the newsletter PDF in your HA config directory:

```
<HA config folder>/bjc_newsletter_pdfs/
```

The integration scans this folder every hour and automatically processes any PDF placed there.

To use this fallback:
1. Open the newsletter at [flipsnack.com/7BBDB688B7A](https://www.flipsnack.com/7BBDB688B7A/)
2. Click the Download button to save the PDF
3. Copy the PDF into `<config>/bjc_newsletter_pdfs/`
4. The integration will pick it up within the hour

## Troubleshooting

| Symptom | Solution |
|---------|----------|
| `sensor.bjc_newsletter_status` shows `error` | Check `last_error` attribute for details |
| Schedule not updating | Verify Gemini API key is valid and Browserbase credentials are set |
| Browserbase session errors in logs | Verify your API key and Project ID in **Configure** |
| Wrong times / missing days | Delete `<config>/bjc_newsletter_cache.json` and restart HA to force reprocess |
| Want to force an immediate reprocess | Go to **Settings → Devices & Services → BJC Newsletter → Configure** and save |

Check HA logs under **Settings → System → Logs** and filter by `bjc_newsletter` for detailed diagnostics.

## License

MIT
