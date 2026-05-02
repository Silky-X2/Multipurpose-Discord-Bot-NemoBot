
# NemoBot — Multipurpose Discord Bot

> A modern, fully customizable Discord bot with a robust leveling system, voice XP, giveaways, interactive leaderboards, and role rewards. Built with [py-cord](https://github.com/Pycord-Development/pycord).

---

## Features

- **Leveling System:** Earn XP by sending messages, being active in voice, and reacting to messages.
- **Fancy Level Card:** `/level` now returns an image-based rank card.
- **Level Card Modes:** Configure default-only, user-choice (with level gate), or auto-unlock-by-level card behavior.
- **Voice XP:** Gain XP for time spent in voice channels.
- **Giveaways:** Run and manage server giveaways with flexible durations like `1d 2m 5min`.
- **Leaderboards:** Interactive leaderboards for level, messages, and voice activity.
- **Role Rewards:** Automatically assign roles at certain levels.
- **XP Boosts:** Grant XP multipliers to users with specific roles.
- **Reaction XP:** Earn XP for adding reactions (with cooldown).
- **Temp Roles:** Grant temporary roles and auto-revoke them after the selected duration.
- **Birthdays:** Let users store birthdays, list upcoming ones, and auto-post birthday greetings.
	- Supports per-server timezone and optional birthday role assignment for the birthday day.
- **Web Dashboard:** Username + passcode dashboard with permission levels (`viewer`, `admin`, `dev`), logs, and restart controls.

---

## Getting Started

1. **Clone the repository:**
	 ```bash
	 git clone https://github.com/Silky-X2/Multipurpose-Discord-Bot-NemoBot.git
	 cd NemoBot
	 ```
2. **Install dependencies:**
	 ```bash
	 pip install -r requirements.txt
	 ```
3. **Configure `.env`:**
	- Create a `.env` file in the root directory and set the following values:

	  | Variable                  | Description                                                      |
	  |---------------------------|------------------------------------------------------------------|
	  | `Token`                   | **Required.** Your Discord bot token.                            |
	  | `DASHBOARD_HOST`          | Host for the dashboard (default: `0.0.0.0`).                     |
	  | `DASHBOARD_PORT`          | Port for the dashboard (default: `8080`).                        |
	  | `DASHBOARD_PUBLIC_URL`    | Optional full public URL used for DM links (e.g. `https://dash.example.com/`). |
	  | `DASHBOARD_PUBLIC_HOST`   | Optional public host/domain used for DM links when no full URL is set. |
	  | `DASHBOARD_PUBLIC_IP`     | Optional public IP fallback used for DM links when host is not set. |
	  | `DASHBOARD_PUBLIC_PORT`   | Optional public port for DM links (defaults to `DASHBOARD_PORT`). |
	  | `DASHBOARD_PUBLIC_SCHEME` | Optional scheme for generated links (`http` or `https`, default: `http`). |
	  | `DASHBOARD_PUBLIC_IP_ENDPOINT` | Optional endpoint used to auto-detect public IP when needed. |
	  | `DASHBOARD_PUBLIC_IP_CACHE_TTL_SECONDS` | Cache duration (seconds) for detected public IP (default: `300`). |
	  | `DASHBOARD_VIEW_TOKEN`    | Passcode for `viewer` dashboard login.                           |
	  | `DASHBOARD_ADMIN_TOKEN`   | Passcode for `admin` dashboard login.                            |
	  | `DASHBOARD_DEV_TOKEN`     | Passcode for `dev` dashboard login.                              |
	  | `DASHBOARD_SESSION_TTL_SECONDS` | Session TTL in seconds (default: `43200`).                 |
	  | `DASHBOARD_ENABLE_CONSOLE` | Enable the console route (`true`/`false`).                     |

4. **Configure `settings.json`:**
	- The bot reads presence and leveling settings from `settings.json`.
	- You can edit this file directly or use the dashboard Settings page to update it.
	- A default `settings.json` is created if missing.

5. **Run the bot:**
	 ```bash
	 python bot.py
	 ```

### Dashboard Settings In .env

Dashboard access uses usernames with matching passcodes from `.env`:

```env
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8080
# Preferred: set a full public URL (domain or IP)
DASHBOARD_PUBLIC_URL=https://dashboard.example.com/
# Alternative host/IP based setup (used when DASHBOARD_PUBLIC_URL is empty)
# DASHBOARD_PUBLIC_HOST=dashboard.example.com
# DASHBOARD_PUBLIC_IP=203.0.113.42
# DASHBOARD_PUBLIC_PORT=8080
# DASHBOARD_PUBLIC_SCHEME=https
# Optional IP detection tuning
# DASHBOARD_PUBLIC_IP_ENDPOINT=https://api.ipify.org
# DASHBOARD_PUBLIC_IP_CACHE_TTL_SECONDS=300
DASHBOARD_VIEW_TOKEN=change-this-view-token
DASHBOARD_ADMIN_TOKEN=change-this-admin-token
DASHBOARD_DEV_TOKEN=change-this-dev-token
DASHBOARD_SESSION_TTL_SECONDS=43200
DASHBOARD_ENABLE_CONSOLE=false
```

### Leveling And Presence Settings In settings.json

```json
{
	"presence": {
		"text": "NemoBot",
		"type": "watching"
	},
	"leveling": {
		"level_card_background": "assets/level_card_bg.png",
		"level_card_storage_dir": "assets/level_cards",
		"inactivity_decay": {
			"enabled": false,
			"start_after_days": 30,
			"percent_per_day": 2.0
		},
		"rolling_decay": {
			"enabled": false,
			"expire_days": 30
		}
	}
}
```

### Dashboard Access Guide

The dashboard cog loads automatically when you run the bot (because every `.py` file in `cogs/` is loaded).

Quick access in Discord:
- Enable debug mode with `/debug`, then use `%dashboard`.
- The bot replies in channel with a short confirmation and sends dashboard URLs via DM.
- The DM includes links for `Same PC`, `Same Wi-Fi/LAN`, and `Public/Internet`.

1. Start the bot:
	```bash
	python bot.py
	```
2. Open the dashboard in your browser:
	- Local machine: `http://127.0.0.1:8080/`
	- Remote/VPS host: `http://<server-ip>:8080/`

For links sent via `%dashboard`, the bot now prefers:
- `DASHBOARD_PUBLIC_URL` (best option, supports domain or IP)
- else `DASHBOARD_PUBLIC_HOST`
- else `DASHBOARD_PUBLIC_IP`
- else auto-detected public IP (if host is local/private)
- else no public link is returned

So yes: you do not have to define a fixed public IP manually. The bot can auto-detect it.
For reliable production setups, using `DASHBOARD_PUBLIC_URL` is still recommended.

If you want access from any external network (for example from anywhere in Germany), ensure your host is publicly reachable:
- Open/forward the dashboard port in firewall/router/NAT
- Use a public static IP or domain (or dynamic DNS)
- If your ISP uses CGNAT, expose the dashboard via reverse proxy/tunnel/VPS

Login credentials:
- Username: `viewer` and passcode: value of `DASHBOARD_VIEW_TOKEN`
- Username: `admin` and passcode: value of `DASHBOARD_ADMIN_TOKEN`
- Username: `dev` and passcode: value of `DASHBOARD_DEV_TOKEN`

If only one of view/admin is set, it is mirrored to keep both roles available. If no tokens are set, defaults are `change-me` (viewer/admin) and `change-me-dev` (dev). Change these immediately in production.

Dashboard home shows spoiler blocks with credentials for your own permission level and lower levels.

#### Dashboard Pages

- `/` Home
- `/leaderboard` Leaderboard view
- `/level-formula` Formula preview, admin updates, and full recalculation action
- `/level-cards` Card mode setup, user equip management, admin enable/disable controls, and dev-only upload/layout/delete controls (with delete confirmation + layout reset)
- `/automod` Automod settings per guild
- `/settings` Presence settings and dev-only restart action
- `/logs` Dev-only dashboard logs
- `/console` Legacy route that redirects to `/logs`

#### Quick Troubleshooting

- Login keeps failing: verify username (`viewer`, `admin`, `dev`) and the matching token in `.env`
- `403 Admin permission required`: you opened an admin action while logged in as `viewer`
- `403 Dev permission required`: restart/log pages require `dev`
- Dashboard not reachable: check `DASHBOARD_HOST`/`DASHBOARD_PORT`, firewall rules, and that the bot process is running
- DM link points to wrong host/IP: set `DASHBOARD_PUBLIC_URL` (or `DASHBOARD_PUBLIC_HOST`/`DASHBOARD_PUBLIC_IP`)
- Port already in use: change `DASHBOARD_PORT` to another free port

### Level Card Background Storage

- Default background image path is read from `leveling.level_card_background` (default: `assets/level_card_bg.png`).
- Built-in selectable backgrounds are auto-generated in `assets/level_cards/builtins` (or your `leveling.level_card_storage_dir`).
- Available built-ins: `default (legacy)`, `Aurora`, `Sunset`, `Nebula`, and `Forest`.
- Uploaded custom backgrounds are saved under `leveling.level_card_storage_dir/custom` (default: `assets/level_cards/custom`).

Security note: never share your dashboard passcodes publicly.

---

## Configuration

- **XP Boost Roles:**
	- File: `cogs/level.py`, variable: `XP_BOOST_ROLES` (top of class)
	- Example:
		```python
		XP_BOOST_ROLES = {
				111111111111111111: 1.50,  # 1.50x boost
				222222222222222222: 1.75,  # 1.75x boost
				333333333333333333: 2.00,  # 2.00x boost
				444444444444444444: 1.25,  # 1.25x boost
		}
		```
	- Replace the placeholder IDs with your server's role IDs.

- **Reaction XP:**
	- File: `cogs/level.py`, method: `on_reaction_add`
	- Users earn 0.1 XP per reaction, with a 1-hour cooldown per user.

- **Level Role Rewards:**
	- File: `cogs/level.py`, variable: `level_roles`
	- Assign role IDs to levels for automatic rewards.

---

## Leaderboards

- Use the `/leaderboard` command to view the interactive leaderboard.
- Toggle between Level, Message, and Voice leaderboards with buttons (see code for details).

## New Commands

- `/levelcard_list`
	- Shows your currently active level card and unlocked card keys
- `/levelcard_equip`
	- Equips a card key for your profile when dashboard mode is `user_choice`

- `/giveaway_start`
	- Duration now supports mixed units: `30min`, `2h`, `1d 2m 5min`
	- `m` is interpreted as month, `min` as minutes
- `/temprole_add`
	- Give a role with duration and auto-revoke
- `/temprole_remove`
	- Remove temporary role immediately

- `/birthday_set`
	- Save your birthday with day + month
- `/birthday`
	- Show a saved birthday for yourself or another member
- `/birthdays`
	- List upcoming birthdays in the server (default next 30 days)
- `/birthday_remove`
	- Remove your stored birthday
- `/birthday_settings show`
	- Shows current birthday channel, timezone, and role configuration
- `/birthday_settings channel_set`
	- Admin command to choose the birthday announcement channel
- `/birthday_settings channel_clear`
	- Admin command to reset to system channel fallback
- `/birthday_settings timezone_set`
	- Admin command to set the server birthday timezone (for example `Europe/Berlin`)
- `/birthday_settings role_set`
	- Admin command to set a temporary birthday role that is removed after the birthday day
- `/birthday_settings role_clear`
	- Admin command to remove the configured birthday role

---

## License

MIT — see [LICENSE](LICENSE)
