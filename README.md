
# NemoBot — Multipurpose Discord Bot

> A modern, fully customizable Discord bot with a robust leveling system, voice XP, giveaways, interactive leaderboards, and role rewards. Built with [py-cord](https://github.com/Pycord-Development/pycord).

---

## ✨ Features

- **Leveling System:** Earn XP by sending messages, being active in voice, and reacting to messages.
- **Voice XP:** Gain XP for time spent in voice channels.
- **Giveaways:** Run and manage server giveaways.
- **Leaderboards:** Interactive leaderboards for level, messages, and voice activity.
- **Role Rewards:** Automatically assign roles at certain levels.
- **XP Boosts:** Grant XP multipliers to users with specific roles.
- **Reaction XP:** Earn XP for adding reactions (with cooldown).

---

## 🚀 Getting Started

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

---

## ⚙️ Configuration

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

## 🏆 Leaderboards

- Use the `/leaderboard` command to view the interactive leaderboard.
- Toggle between Level, Message, and Voice leaderboards with buttons (see code for details).

---

## 📄 License

MIT — see [LICENSE](LICENSE)
