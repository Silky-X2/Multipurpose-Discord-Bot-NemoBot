
# NemoBot — Multipurpose Discord Bot

> A modern, fully customizable Discord bot with a robust leveling system, voice XP, giveaways, interactive leaderboards, and role rewards. Built with [py-cord](https://github.com/Pycord-Development/pycord).

---

## Features

- **Leveling System:** Earn XP by sending messages, being active in voice, and reacting to messages.
- **Fancy Level Card:** `/level` now returns an image-based rank card.
- **Voice XP:** Gain XP for time spent in voice channels.
- **Giveaways:** Run and manage server giveaways with flexible durations like `1d 2m 5min`.
- **Leaderboards:** Interactive leaderboards for level, messages, and voice activity.
- **Role Rewards:** Automatically assign roles at certain levels.
- **XP Boosts:** Grant XP multipliers to users with specific roles.
- **Reaction XP:** Earn XP for adding reactions (with cooldown).
- **Temp Roles:** Grant temporary roles and auto-revoke them after the selected duration.
- **Web Dashboard:** Username + passcode dashboard with permission levels (`viewer`, `admin`, `dev`), logs, and restart controls.

---

## Getting Started

1. **Clone the repository:**
	 ```bash
	 git clone https://github.com/yourname/NemoBot.git
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
	  | `DASHBOARD_VIEW_TOKEN`    | Passcode for `viewer` dashboard login.                           |
	  | `DASHBOARD_ADMIN_TOKEN`   | Passcode for `admin` dashboard login.                            |
	  | `DASHBOARD_DEV_TOKEN`     | Passcode for `dev` dashboard login.                              |
	  | `LEVEL_CARD_BACKGROUND`   | (Optional) Path to level card background image.                  |

4. **Run the bot:**
	 ```bash
	 python bot.py
	 ```

### Dashboard Tokens In .env

Dashboard access uses usernames with matching passcodes from `.env`:

```env
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8080
DASHBOARD_VIEW_TOKEN=change-this-view-token
DASHBOARD_ADMIN_TOKEN=change-this-admin-token
DASHBOARD_DEV_TOKEN=change-this-dev-token
LEVEL_CARD_BACKGROUND=assets/level_card_bg.png
```

### Dashboard Access Guide

The dashboard cog loads automatically when you run the bot (because every `.py` file in `cogs/` is loaded).

Quick access in Discord:
- Enable debug mode with `/debug`, then use `%dashboard`.
- The bot replies in channel with a short confirmation and sends the dashboard link via DM.
- The DM states "Only you can see this" and does not post passcodes publicly.

1. Start the bot:
	```bash
	python bot.py
	```
2. Open the dashboard in your browser:
	- Local machine: `http://127.0.0.1:8080/`
	- Remote/VPS host: `http://<server-ip>:8080/`

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
- `/automod` Automod settings per guild
- `/settings` Presence settings and dev-only restart action
- `/logs` Dev-only dashboard logs
- `/console` Legacy route that redirects to `/logs`

#### Quick Troubleshooting

- Login keeps failing: verify username (`viewer`, `admin`, `dev`) and the matching token in `.env`
- `403 Admin permission required`: you opened an admin action while logged in as `viewer`
- `403 Dev permission required`: restart/log pages require `dev`
- Dashboard not reachable: check `DASHBOARD_HOST`/`DASHBOARD_PORT`, firewall rules, and that the bot process is running
- Port already in use: change `DASHBOARD_PORT` to another free port

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

- `/giveaway_start`
	- Duration now supports mixed units: `30min`, `2h`, `1d 2m 5min`
	- `m` is interpreted as month, `min` as minutes
- `/temprole_add`
	- Give a role with duration and auto-revoke
- `/temprole_remove`
	- Remove temporary role immediately

---

## License

MIT — see [LICENSE](LICENSE)
