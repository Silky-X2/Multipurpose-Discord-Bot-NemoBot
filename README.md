
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
- **Web Dashboard:** Token-protected dashboard for leaderboards, level formula, automod, bot settings, and restart.

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
3. **Configure your bot token:**
	 - Create a `.env` file in the root directory:
		 ```env
		 Token=YOUR_DISCORD_BOT_TOKEN
		 ```
4. **Run the bot:**
	 ```bash
	 python bot.py
	 ```

### Optional Dashboard Environment Variables

Add these to `.env` if you want the web dashboard enabled with secure tokens:

```env
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8080
DASHBOARD_VIEW_TOKEN=change-this-view-token
DASHBOARD_ADMIN_TOKEN=change-this-admin-token
DASHBOARD_ENABLE_CONSOLE=false
LEVEL_CARD_BACKGROUND=assets/level_card_bg.png
```

### Dashboard Access Guide

The dashboard cog loads automatically when you run the bot (because every `.py` file in `cogs/` is loaded).

1. Start the bot:
	```bash
	python bot.py
	```
2. Open the dashboard in your browser:
	- Local machine: `http://127.0.0.1:8080/?token=<your-token>`
	- Remote/VPS host: `http://<server-ip>:8080/?token=<your-token>`

Use one of these tokens in `?token=`:
- `DASHBOARD_VIEW_TOKEN`: read-only access
- `DASHBOARD_ADMIN_TOKEN`: edit settings, restart bot, and use console (if enabled)

If only one token is set, it is used for both roles. If no tokens are set, the bot falls back to `change-me` for both (change this immediately in production).

#### Dashboard Pages

- `/` Home
- `/leaderboard` Leaderboard view
- `/level-formula` Level curve preview and admin updates
- `/automod` Automod settings per guild
- `/settings` Presence settings and restart action
- `/console` Host command runner (admin + `DASHBOARD_ENABLE_CONSOLE=true` only)

#### Alternative Auth Method

Instead of `?token=...`, you can send an HTTP header:

`Authorization: Bearer <your-token>`

#### Quick Troubleshooting

- `401 Unauthorized`: missing/invalid token
- `403 Admin permission required`: you opened an admin action with the viewer token
- Dashboard not reachable: check `DASHBOARD_HOST`/`DASHBOARD_PORT`, firewall rules, and that the bot process is running
- Port already in use: change `DASHBOARD_PORT` to another free port

Security note: never share your admin token publicly.

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
