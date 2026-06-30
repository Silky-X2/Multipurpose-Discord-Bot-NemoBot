
# NemoBot Multipurpose Discord Bot

Ein moderner Discord-Bot mit Fokus auf Leveling, Leaderboards und Server-Utilities.

## Features (kurz)

- **Leveling-System** (Text + Voice XP)
- **Levelkarten** (Bildbasierte Rank Cards)
- **Leaderboards** (Level, Nachrichten, Voice)
- **Giveaways**
- **Temporäre Rollen**
- **Geburtstage** (inkl. automatischer Glückwünsche)
- **Rollen-Belohnungen** & **XP-Boosts**
- **Web-Dashboard** mit Rollen (`viewer`, `admin`, `dev`)

---

## Requirements

- **Python 3.10+** (empfohlen: 3.11)
- Ein **Discord Bot Token**
- Abhängigkeiten aus `requirements.txt`

---

## Installation & Nutzung

### 1) Repository klonen

```bash
git clone https://github.com/Silky-X2/Multipurpose-Discord-Bot-NemoBot.git
cd Multipurpose-Discord-Bot-NemoBot
```

### 2) Abhängigkeiten installieren

```bash
pip install -r requirements.txt
```

### 3) `.env` erstellen

Lege im Projektordner eine `.env` an:

```env
Token=DEIN_DISCORD_BOT_TOKEN

DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8080

DASHBOARD_VIEW_TOKEN=change-this-view-token
DASHBOARD_ADMIN_TOKEN=change-this-admin-token
DASHBOARD_DEV_TOKEN=change-this-dev-token
```

> Wichtig: Tokens/Passcodes niemals öffentlich teilen.

### 4) Bot starten

```bash
python bot.py
```

---

## Bot selbst nutzen

1. Lade den Bot auf deinen Server ein (über das Discord Developer Portal).
2. Starte den Bot lokal oder auf einem Server/VPS.
3. Nutze Slash-Commands direkt in Discord, z. B.:
   - `/level`
   - `/leaderboard`
   - `/giveaway_start`
   - `/birthday_set`

---

## Dashboard (optional)

Nach dem Start erreichbar unter:

- `http://127.0.0.1:8080/` (lokal)
- oder `http://<server-ip>:8080/` (remote)

Login mit:
- `viewer` + `DASHBOARD_VIEW_TOKEN`
- `admin` + `DASHBOARD_ADMIN_TOKEN`
- `dev` + `DASHBOARD_DEV_TOKEN`

---

---
