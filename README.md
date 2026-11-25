# OneChain

A live options-chain dashboard for Indian indices and stocks on Dhan market data: the chain with Greeks, gamma exposure and alerts, intraday charts, and playback of stored sessions.

It knows 86 instruments out of the box — five indices (Nifty, Bank Nifty, Midcap Nifty, Sensex, Bankex) and 81 NSE stocks. You pick an instrument and one or more expiries, press Fetch, and the chain, charts and alerts appear in your browser at `http://localhost:8053`, refreshing on their own if you switch on auto-refresh.

It runs entirely on your own computer, in Docker, behind a login. Nothing is sent anywhere except requests to Dhan for market data and, only if you set it up, alert messages to your own Telegram chat. The page's stylesheet and fonts are part of the app, not fetched from the internet.

A walkthrough video on YouTube covers installation, setup and a tour of every screen.

**This is not official Dhan software and not investment advice.** It is a data and analytics tool; every trading decision is yours.

---

## Contents

- [What you need](#what-you-need)
- [Setup](#setup)
- [Day-to-day use](#day-to-day-use)
- [Three files you keep up to date](#three-files-you-keep-up-to-date)
- [The screen, piece by piece](#the-screen-piece-by-piece)
- [Reading the chain](#reading-the-chain)
- [Alerts](#alerts)
- [The interpretor](#the-interpretor)
- [Terms used](#terms-used)
- [What runs automatically](#what-runs-automatically)
- [MCX](#mcx)
- [Where your data lives](#where-your-data-lives)
- [Fonts](#fonts)
- [Running without Docker, and the tests](#running-without-docker-and-the-tests)
- [Troubleshooting](#troubleshooting)
- [Licence](#licence)

---

## What you need

- **Docker Desktop** (Mac or Windows), or Docker with Docker Compose on Linux — https://www.docker.com/products/docker-desktop/
- **A Dhan trading account with API access**: you need its client ID and an access token, from your Dhan account's API settings.
- An internet connection: the first start downloads the Python base image and packages, and the app reaches Dhan for data.

---

## Setup

### 1. Fill in the settings file

In the project folder, copy `.env.example` to a new file named `.env`, open it in a text editor and fill in the four required lines:

```env
CLIENT_ID=your Dhan client ID
ACCESS_TOKEN=your Dhan access token
DASHBOARD_USER=any username you choose
DASHBOARD_PASSWORD=any password you choose
```

The username and password are your own choice: the browser asks for them when you open the dashboard, and the app will not start without them. Files starting with a dot can be hidden — on a Mac, `Cmd + Shift + .` shows them in Finder; on Windows, turn on "Hidden items" in File Explorer's View menu.

`.env.example` also lists the optional settings, each with a line on what it does. The two worth setting are the **risk-free rates**: without `RISK_FREE_RATE_NSE` (a decimal, `0.065` for 6.5%) the app computes no Black-Scholes prices, so the model-price hovers, the over/under-valued borders, the straddle markers and the reversal curves stay empty. The others — a fallback VIX, Telegram, connection tuning — can wait until you want them.

`.env` stays on your computer. It is listed in `.gitignore` and is not built into the Docker image. The app rewrites the `ACCESS_TOKEN` line itself when it renews your token (see [What runs automatically](#what-runs-automatically)).

**Is this safe?** The dashboard only listens on your own computer (`127.0.0.1`) — it is not reachable from your home network or the internet, even by other devices on your Wi-Fi — and it only answers requests addressed to `localhost:8053` or `127.0.0.1:8053`. The login is a further layer on top of that.

### 2. Start it

- **Mac:** double-click `start.command`.
- **Windows:** double-click `start.bat`.
- **Linux, or any terminal:** in the project folder, run `docker compose up -d --build`.

The start file checks that Docker is installed and running, checks that `.env` exists and has the four required values — and names any that are missing — then builds and starts the app and opens the dashboard in your browser. The first start takes a few minutes; later starts take seconds unless something changed.

The first time, your computer may refuse to open the start file. **Mac:** right-click (or Control-click) it, choose **Open**, then **Open** again; or go to System Settings → Privacy & Security and click **Open Anyway**. **Windows:** on "Windows protected your PC", click **More info**, then **Run anyway**. Once per file.

### 3. Open the dashboard

Go to `http://localhost:8053` and log in with the username and password from `.env`.

### 4. Stop it

Double-click `stop.command` (Mac) or `stop.bat` (Windows), or run `docker compose down`. Everything in `data/` is kept.

---

## Day-to-day use

| What you want to do | How |
|---|---|
| Start the app | `start.command` / `start.bat`, or `docker compose up -d --build` |
| Stop the app | `stop.command` / `stop.bat`, or `docker compose down` |
| Watch what it's doing | `docker compose logs -f` |
| Pick up an edit to `.env` or `data/holidays.txt` | `docker compose restart` |
| Pick up an edit to `config.json` or `assets/assets_select.csv`, or a code update | the start file, or `docker compose up -d --build` (these files are built into the image) |

**Start it in the morning and stop it after the end-of-day run.** The app renews your Dhan token every time it starts, and at 15:30 on trading days it stores the day's snapshot, cleans up and renews the token again; that run takes about ten minutes, so keep the app running until about 15:45. If it is not running at 15:30, that day is missing from the history the "yesterday" figures and the volatility analytics are built from.

---

## Three files you keep up to date

### `data/holidays.txt` — market holidays

One date per line as `dd/mm/yyyy`; lines starting with `#` are notes. On a listed date:

- the 15:30 end-of-day run does not happen — no snapshot, no cleanup, no token renewal;
- auto-refresh does not fetch (a manual Fetch still does);
- the day is not counted as a trading day in time to expiry, which the Black-Scholes prices, the Advanced tab's gamma profile and the reversal curves use.

**The file ships with the NSE trading holidays for 2026 only. Add each new year's dates** as soon as NSE publishes its trading-holiday list for that year — it does so late in the year, on nseindia.com. **Nothing warns you if you don't:** a holiday missing from the list is treated as a trading day, so the 15:30 run stores the closed market's unchanged chains in the history the volatility figures are computed from, and time to expiry counts one day too many. A missing file, or a line that is not a date, is not reported either — the app then treats only weekends, plus whatever dates did parse, as closed.

Restart the app after editing it (it is read once, at start).

### `assets/assets_select.csv` — the instruments

One row per instrument. The columns the app uses:

| Column | What it is for |
|---|---|
| `asset_id`, `segment_code` | Dhan's security ID and segment (`IDX_I` for indices, `NSE_EQ` for stocks). Together they identify the instrument — the same ID can mean different things in different segments. |
| `asset_name`, `display_name`, `exchange` | The names shown on the page; the exchange. |
| `strike_size` | The gap between strikes — 50 for Nifty, 2.5 for ITC. The pain points, the alert margins and the strike grid read it from here, never guess it from the chain. |
| `lot_size` | Contracts per lot. Every count on the page (OI, volume, OI change) is shown in lots. |
| `number_expiries` | How many expiries the 15:30 snapshot stores for this instrument (blank means 1). The day's own expiry is skipped. Set it so the stored expiries reach past 30 days — the volatility figures interpolate to a constant 30-day maturity and need an expiry on either side. |

The other columns (`segment_id`, `index`, `depth`, `heavy`, `weight_*`) are informational; nothing reads them.

**Keep it current.** Lot sizes and strike steps change, and stocks enter and leave the F&O list. The app does not update this file. Rebuild after editing (the start file does it).

### `config.json` — alert tuning

Three blocks, all optional to touch:

- `rate_alert_thresholds` and `rate_alert_floors` — what counts as a fast change for the [rate alerts](#rate-alerts-fast-changes-per-strike). The app refuses to start if this file is missing or malformed, rather than run with made-up values.
- `interpretor` — switch [the interpretor](#the-interpretor) off with `"enabled": false`, or tune it.
- `telegram_forward` — which kinds of alert go to Telegram, one true/false per kind. Shipped: spot crossing a wall, and clusters of fast OI changes.

Rebuild after editing (the start file does it).

---

## The screen, piece by piece

### Controls (top)

| Control | What it does |
|---|---|
| **Mode** | *Single Instrument*: one instrument, one or more expiries. *Multi Instrument*: a basket of instruments, one expiry each — add them with **Add Request** (a second expiry of an instrument already in the basket is refused: Dhan serves several expiries of one instrument, or one expiry each of several, but not both at once). |
| **Asset** | The instrument. Indices first, then stocks. |
| **Expiries** | One or more expiries. **⟳** fetches a fresh expiry list from Dhan. |
| **Strike** | How many strikes either side of the current price the chain shows: ±8, ±10 (default), ±12, ±16, ±32 or All. It changes only what is shown; every total, regime and alert is computed on the whole chain. |
| **Auto** | Off, or fetch again every 5s, 15s, 30s, 1m or 5m. |
| **Fetch** | Fetch now, and make this selection the one auto-refresh repeats. |

Fetching happens inside the app, not in your browser tab: once you press Fetch, auto-refresh carries on with the tab in the background or closed. Auto-refresh runs on trading days until 15:30, then stops. **A manual Fetch works at any time** — before the open, after the close, at weekends.

### The tab bar

**Chain**, **Charts** and **Advanced**, and on the right: the lot size, the India VIX in use (`VIX 13.2% @ 10:41`; `(default)` when your fallback `DEFAULT_VIX` is being used; `No VIX available yet`), the time of the chain on screen, and four controls:

- **AGG** — aggregate: with one instrument and more than one expiry, sum OI, volume and gamma exposure across the expiries into one chain. IV, Greeks, skew, pinning and the "yesterday" references are not shown in this view — they have no meaning summed across expiries.
- **GREEKS** — show delta, gamma, theta and vega for each strike instead of the usual columns.
- **SURFACE** — show the rate-alert grid instead of the chain (see [Alerts](#rate-alerts-fast-changes-per-strike)).
- **★ Mark** — bookmark this moment (see [Playback](#playback-and-bookmarks)).

With more than one instrument or expiry loaded, a second row of tabs switches between them.

### Chain tab

The option chain — see [Reading the chain](#reading-the-chain). Two cells open side panels:

- **Click an option's LTP** to pin its reversal curve in the right-hand drawer.
- **Click the current-price cell** (the row starting `₹`) to open the left-hand drawer of time-series charts.

### Charts tab

How the chain is spread across strikes, for the instrument on screen: open interest and OI change by strike, volume by strike, the IV smile (call and put IV per strike), net gamma exposure by strike (green above zero, red below), and the delta, gamma, vega and theta curves. A dashed line marks the current price. This tab refreshes when you open it, switch instrument or change the strike range — not on every fetch.

### Advanced tab

A deeper, slower read across several expiries of one instrument. Choose **3, 4 or 5 expiries** and press **Run Advanced**: the app fetches those expiries (±16 strikes) and draws:

- **3D implied-volatility surfaces** — calls and puts, IV across strike and days to expiry.
- **Gamma exposure profile** — net gamma exposure across a range of prices around the current one, with each option's gamma recomputed at every price, and the **zero-gamma level**: the price at which net gamma exposure would cross zero.
- **Volatility term structure** — at-the-money call and put IV by days to expiry.
- **Gamma exposure term structure** — gamma exposure per expiry.
- **Expiry migration heatmap** — open interest by strike and expiry, combined or split into calls and puts.

The result stays until you run it again; it is not saved and does not refresh. Live fetching pauses while it runs. It works in Single Instrument mode only.

### Analytics panel (bottom)

Click **▲ Analytics Panel**. Its header carries a scrolling ticker of the instrument's alerts. Inside, for the instrument on screen:

- **Positioning · Greeks** — GEX regime (Stabilizing or Destabilizing), vol regime, average call and put delta, delta skew, the strike with the most vega, average theta, vega skew.
- **Skew — 25Δ risk reversal** — the risk reversal, the 25-delta call and put IVs, the at-the-money call and put IVs, the gap between them, and whether the skew is towards puts or calls.
- **Pinning pressure** — the strike gamma is most concentrated near, how strong that pull is (it rises towards expiry), and the price's distance from it.
- **Advanced report** — after a Run on the Advanced tab, for the same instrument: the multi-expiry GEX regime, the zero-gamma level and the distance to it, and each expiry's at-the-money IVs.
- **Vol analytics** — realised volatility, call and put IV, IV percentile, IV skew and the IV-minus-RV spreads, for the window chosen with the **22D / 90D / 180D / 252D** buttons. These come from the daily 15:30 snapshots, so they fill in as history builds up; the percentile is computed for 180D and 252D only.
- **Four charts** — today's price in 5-minute candles (a 3-hour view; drag to see more), with dotted lines at the pain points of the highest-OI call strike (red) and put strike (green) within the strike range shown; IV against realised volatility over the history; today's aggregate gamma exposure (total, calls, puts); and today's 25-delta risk reversal.

The right-hand **Changes** column lists the day's alerts for the instrument, newest first, under the interpretor's summary.

The panel and the playback drawer take turns: opening one closes the other.

### Left drawer — time-series charts

Two charts, each with a metric (LTP, BS Price, Volume, OI, OI Chg, IV, GEX, VEX, Delta, Gamma, Theta, Vega) and any options of the instrument on screen, through today's session. Each instrument remembers its own picks.

### Right drawer — reversal curves

Up to two options per instrument, pinned by clicking their LTP. For each: the option's actual price through the day (blue), and the price the same option would have if the underlying stood at a **pain point** — the option's own (amber), the highest-OI call strike's (red), the highest-OI put strike's (green), and the second-highest strikes' (dotted) when their OI is at least 75% of the highest. They are Black-Scholes prices with the option's own IV, so they drift down through the day as time passes. Needs the risk-free rate in `.env`. **Clear** unpins the instrument's options.

### Playback and bookmarks

The small arrow tab at the bottom centre opens the **playback drawer**. Pick an instrument, expiry, date and moment and press **Load Moment** to put any stored fetch back on screen — chain, charts and panels; step with **Prev** / **Next** or the slider. Live fetching continues behind it, and the drawer counts the newer moments you are not looking at. **Back to Live** returns to the present; closing the drawer does not.

**★ Mark** on the tab bar saves the moment on screen as a bookmark, named with the current time unless you type a name. Bookmarks show as ★ on the playback slider and in **Jump to Bookmark**. Stored moments and bookmarks are kept for 7 days.

---

## Reading the chain

Calls on the left, puts on the right, strikes in the middle. All counts are **in lots**. A number in brackets after a count is its size against the largest in that column.

| Column | Shows |
|---|---|
| GEX | Net gamma exposure at the strike (see [Terms used](#terms-used)). |
| PCR | Put-call ratio of OI, and of volume in brackets. |
| VEX | Vega exposure: vega × open interest. |
| IV | Implied volatility, as Dhan reports it. |
| OI Chg, OI, Volume | Change in open interest since the previous day, open interest, today's volume. |
| LTP | Last traded price, and in brackets the option's risk ÷ reward over one strike: what it loses if the underlying moves one strike against it, divided by what it gains if it moves one strike in its favour. |
| STRIKE | The strike, the straddle's market price (call + put LTP) in brackets, and 🔺 / 🔺🔺 when that is 10% / 20% above its Black-Scholes price, 🔹 / 🔹🔹 when below. |
| Straddle | Call + put OI at the strike, and the average of their IVs in brackets. |

**The current-price row** (starting `₹`) sits between the strikes either side of the price. It shows the price, put OI minus call OI, and the IV skew — put minus call — over the strikes shown, then the same at the money. Its other cells hold the totals over the whole chain; its IV cells show the average IV over the strikes shown and, in brackets, the at-the-money IV.

**Colours and marks:**

- Shading: in-the-money strikes are lighter, out-of-the-money darker.
- Top three by OI, volume, VEX and straddle OI on each side: 1st amber, 2nd teal, 3rd a dashed blue border.
- OI Chg borders: today's two biggest increases on each side in green, the two biggest decreases in red (solid for the largest, dashed for the second).
- LTP borders: green when the market price is below its Black-Scholes price, red when above — dashed from 10%, solid from 20%.
- A violet border marks the **max-pain** strike.
- 🥇🥈🥉 on OI: the three strikes with the most open interest at the previous day's close. 🥇+ 🥈+ / 🥇- 🥈- on OI Chg: the previous day's two biggest OI increases and decreases, from the app's own end-of-day snapshot.

**Hover for more:** the Black-Scholes price on an LTP; the call (🔴) and put (🟢) pain points and the Black-Scholes straddle on a strike; volume ÷ OI on volume; the previous day's totals on the current-price row; the 09:20 values on IV, PCR, OI and straddle cells (captured from a fetch landing between 09:20 and 09:21, if one did); and the interpretor's confidence on the strikes it tracks.

---

## Alerts

### Level and regime alerts

Checked on every fetch, for every instrument and expiry loaded, not just the one on screen:

| Alert | When | Severity |
|---|---|---|
| Spot crosses a wall | The price crosses the **resistance** (the pain point of the highest-OI call strike) or the **support** (the highest-OI put strike's) by more than half a strike | critical |
| GEX regime flips | Net gamma exposure changes sign, held for two fetches in a row | critical towards Destabilizing, info towards Stabilizing |
| Risk-reversal flip | The 25-delta risk reversal moves from put skew to call skew or back, by more than 0.2 vol points | warning |
| PCR leaves its band | The put-call ratio goes above 1.30 or below 0.80 | warning |
| Strike moves | The highest or second-highest OI or volume strike changes, on either side | info |

They scroll in the Analytics panel's header and list in its Changes column for the instrument on screen. A critical alert on the instrument on screen also beeps — browsers allow sound only after you have clicked somewhere on the page once. The day's alerts survive a restart and are cleared the next day.

### Rate alerts (fast changes per strike)

From 09:45, while auto-refresh runs, the app samples every contract within 32 strikes of the price once a minute and compares its OI, IV and volume with 1, 5 and 30 minutes earlier. A change beyond the threshold in `config.json` counts — shipped: OI ±2.5% / ±10% / ±25%, IV ±10% / ±25% / ±50%, volume up 5% / 20% / 50% — ignoring strikes with less than 1,000 lots of OI or volume, or IV under 5.

- **In the chain:** a thin column beside each OI, IV and Volume column lights up — white for a jump, sky blue for a drop.
- **SURFACE** on the tab bar shows all nine checks per side as a grid.
- **A beep** (and a Telegram message, if enabled) when three neighbouring strikes on the same side cross the same check in the same direction.

The samples are held in memory: after a restart they take up to 30 minutes to build up again.

### Telegram

Put a bot token and a chat id in `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — create the bot with Telegram's BotFather) and the app sends new alerts for every loaded instrument to that chat; `telegram_forward` in `config.json` chooses which kinds. A failed or refused message is noted in the log and never interrupts the app.

---

## The interpretor

A read of how the open-interest walls move through the day, side by side for calls and puts.

- **Walls.** On each side, the strike with the highest OI is the main wall; the second-highest counts too when its OI is at least 75% of the main one's.
- **Shift.** When a new strike takes over as the main wall and holds for two minutes, that side reads **Bullish** (the wall moved up) or **Bearish** (it moved down). 🚩 marks a change still waiting out its two minutes. The **streak** counts shifts in the same direction in a row.
- **Bias.** A second wall appearing above the main one reads bullish (🟢), below it bearish (🔴); one disappearing reverses that. For an hour after a shift, the bias is not shown.
- **Hits.** Each time the price comes within a tenth of a strike of a tracked wall's pain point, that wall records a hit and its confidence drops.

**Where it shows:** the current-price row's call and put LTP cells show each side's read; its straddle cell shows the combined read (`S.Bull`, `Bullish`, `Undecided`, `In-Range`, …); hovering a tracked strike's OI shows its confidence and hits; and the top of the Changes column shows each side's streak and its walls at 09:20.

It starts from the first fetch after 09:15, is cleared each day, and can be switched off in `config.json`.

---

## Terms used

| Term | Meaning here |
|---|---|
| **GEX** (gamma exposure) | gamma × open interest × price × 1%, per lot — positive for calls, negative for puts. Summed over the chain: above zero, dealers are taken to be long gamma and their hedging damps moves (**Stabilizing**); below zero, it amplifies them (**Destabilizing**). |
| **Pain point** | The app's estimate of the price at which an option's sellers start to hurt: the strike, moved by how heavily the option trades against its open interest — above the strike for calls, below it for puts. |
| **Max pain** | The expiry price at which option holders, all together, would collect the least. |
| **Pin strike** | The strike where gamma is most concentrated near the current price — where the price tends to be pulled near expiry. |
| **25Δ risk reversal** | IV of the 25-delta call minus IV of the 25-delta put. Negative: puts are dearer (put skew). |
| **At-the-money IV** | The average IV of the two strikes either side of the price, for calls and puts separately. |
| **Realised volatility (RV)** | How much the price actually moved, from daily closes, annualised. |
| **IV percentile** | Where today's IV sits among the window's days, on a constant 30-day maturity. |
| **Black-Scholes price** | The model price of an option from the price, strike, time to expiry, risk-free rate and India VIX — a common yardstick, not a fair value. |

---

## What runs automatically

**Live fetching** runs inside the app, so it does not depend on the browser tab. It stops for the 15:30 run and for listed holidays and weekends; a manual Fetch works at any time.

**Every trading day at 15:30 IST** (it takes about ten minutes; keep the app running until about 15:45):

1. Auto-refresh is switched off.
2. **Snapshot:** every instrument in `assets/assets_select.csv` is fetched (its `number_expiries` expiries) and stored as the day's record, along with its volatility figures. The next day's "yesterday" hovers and medals and the Vol analytics come from these. Rate-limit warnings from Dhan in the log during this run are normal.
3. **Cleanup:** stored moments and bookmarks older than 7 days are deleted and the database is compacted.
4. **Token:** your Dhan access token is renewed and written back into `.env`.

A Fetch pressed while this runs waits and goes through as soon as it finishes; after that, fetching by hand works as usual.

**Every time the app starts**, it renews the Dhan token first.

**Dhan tokens last 24 hours.** If the app has been stopped for longer — over a weekend, for example — the token in `.env` will have expired: get a fresh one from Dhan, paste it in, then start the app.

---

## MCX

OneChain is set up for NSE: its instrument list holds NSE indices and stocks, and its automatic parts follow NSE's hours. MCX option chains work too — fetched by hand:

1. **Add the commodity** to `assets/assets_select.csv`: `asset_id` = Dhan's security ID of the futures contract whose options you want, `segment_code` = `MCX_COMM`, `exchange` = `MCX`, its `strike_size` and `lot_size`, and a name. Dhan publishes these IDs in its instrument list (linked from its API documentation), kept up to date. An option chain belongs to one futures contract, so when that contract expires, put in the next one's ID. Rebuild (the start file does it).
2. **For Black-Scholes prices**, set `RISK_FREE_RATE_MCX` and `MCX_VOLATILITY_<NAME>` in `.env` — for example `MCX_VOLATILITY_CRUDEOIL=0.30` — where `<NAME>` is the `asset_name` from the CSV in capitals. MCX does not use India VIX.
3. **Fetch it by hand** — at any time, including after 15:30 once the end-of-day tasks have finished.

What stays on NSE's timetable: auto-refresh stops at 15:30 for every instrument; the 15:30 snapshot stores an MCX instrument at 15:30, in the middle of its session, so its "yesterday" figures are 15:30 figures; and MCX days follow the NSE holiday list.

---

## Where your data lives

Everything is under `data/`, which is kept when the app stops or is rebuilt:

| File | Holds | Kept for |
|---|---|---|
| `market.db` | Every fetched chain, its summary, the VIX readings, and your bookmarks — what playback and the intraday charts read. It can reach a few gigabytes. | 7 days |
| `snapshot.db` | The 15:30 snapshot of every instrument and its daily volatility figures | indefinitely |
| `expiry_cache.json` | Expiry lists from Dhan | until refreshed with ⟳ |
| `alerts_cache.json`, `interpretor.json`, `hover.json` | Today's alerts, interpretor state and 09:20 references, so a restart keeps them | cleared each new day |
| `holidays.txt` | Market holidays (see above) | — |
| `logs/onechain.log` | What the app reports — the same lines `docker compose logs` shows | a new file each day, 14 kept |

The databases are created on the first start. To start from nothing, stop the app and delete the `.db` files (and any `-wal` and `-shm` files beside them).

---

## Fonts

The interface font, **General Sans**, is not included: its licence (the ITF Free Font License) does not allow the font files to be shared through a repository. Without it the app uses Helvetica Neue on a Mac and Segoe UI on Windows, and everything works the same.

For the original look, download General Sans free from https://www.fontshare.com/fonts/general-sans and put its web-font files for four weights into `assets/fonts/` with these names: `GeneralSans-Regular.woff2`, `GeneralSans-Medium.woff2`, `GeneralSans-Semibold.woff2` and `GeneralSans-Bold.woff2`. Then rebuild (the start file does it). `.gitignore` keeps them out of git.

The numbers use **Geist Mono**, which is included (see `THIRD_PARTY_LICENSES`).

---

## Running without Docker, and the tests

Python 3.12 (the version the Docker image uses) or 3.13, in the project folder:

```bash
python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.lock    # every package at the tested version
python3 app.py                      # http://localhost:8053
```

It reads `.env` from the project folder and, as under Docker, listens on your computer alone. Without Docker nothing restarts it if it stops.

Run the tests from the project folder with `pytest` (they need no `.env`, no market data and no network), and `python3 validate.py` for a check of the code's layering and the app's readiness. `requirements.txt` lists the direct dependencies; `requirements.lock` pins everything, and the Docker image installs the lock.

How it is built: `app.py` assembles a Dash app (with Plotly, Bootstrap and Mantine components) from `features/` (one folder per part of the screen), `services/` (the Dhan client, the live fetcher, the 15:30 run, the token renewal), `analytics/` (the chain calculations, alerts and interpretor — no I/O), `repositories/` (the two SQLite databases), `state/` and `ui/`. Fetching and the 15:30 run are two background threads started with the app.

---

## Troubleshooting

- **The start file says the settings file is missing or incomplete** — see [Setup](#1-fill-in-the-settings-file).
- **The login won't accept my username or password** — check `DASHBOARD_USER` and `DASHBOARD_PASSWORD` in `.env` for typos, then `docker compose restart`.
- **Fetch brings nothing** — check `CLIENT_ID` and `ACCESS_TOKEN` in `.env`. After more than a day stopped, the token has expired: paste a fresh one from Dhan and restart. `docker compose logs -f` shows Dhan's answer.
- **No Black-Scholes prices, no reversal curves** — set `RISK_FREE_RATE_NSE` in `.env`. The NSE prices also need an India VIX: one is fetched with every Fetch; if Dhan returns none, set `DEFAULT_VIX`. After the day's close the reversal curves have no session left to draw over.
- **No "yesterday" hovers, no medals, empty Vol analytics** — they come from the 15:30 snapshots, so they appear after the app has run through its first 15:30; the longer windows need months of them.
- **No beep** — click anywhere on the page once; browsers block sound until you do.
- **The rate markers never light** — they need auto-refresh running, and start at 09:45.
- **The font looks different from the video** — General Sans is not included; see [Fonts](#fonts).
- **The start file says "compose" is not a Docker command** — update Docker Desktop.
- **Port 8053 is in use** — stop the other program, or set `PORT=8055` in `.env` and change the port line in `docker-compose.yml` to `"127.0.0.1:8055:8055"` (both numbers — the app only answers on its own port), then open `http://localhost:8055`.

---

## Licence

MIT — see `LICENSE`. The bundled Bootstrap stylesheet is MIT licensed and the Geist Mono typeface is under the SIL Open Font License — see `THIRD_PARTY_LICENSES`. The Python packages are installed by pip from `requirements.lock` and keep their own licences.

---

From ChorBazaar — https://chorbazaar.co.in
