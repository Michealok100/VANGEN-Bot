# 🔐 Vanity ETH Address — Telegram Bot

A Telegram bot that generates vanity Ethereum wallet addresses with live progress updates.

---

## ✨ Features

- `/vanity prefix <pattern>` — find address starting with your pattern
- `/vanity suffix <pattern>` — find address ending with your pattern
- `/cancel` — stop an active search
- Live progress updates every few seconds (attempts, speed, elapsed time)
- 📋 Copy Address button on result
- 🔗 View on Etherscan button on result
- Multi-core parallel search for maximum speed

---

## 🗂 Project Structure

```
vanity-telegram-bot/
├── bot.py           # Telegram bot + async orchestration
├── generator.py     # Worker process (key gen + matching)
├── requirements.txt
├── .env.example
├── railway.json
└── README.md
```

---

## 🚀 Quick Start (Local)

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env and add your TELEGRAM_BOT_TOKEN
```

### 3. Run
```bash
python bot.py
```

---

## ☁️ Deploy on Railway

1. Push to GitHub
2. Railway → New Project → Deploy from GitHub
3. Variables tab → add `TELEGRAM_BOT_TOKEN`
4. Railway auto-deploys via `railway.json`

---

## 💬 Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome message + usage guide |
| `/vanity prefix abc` | Find address starting with `0xabc…` |
| `/vanity suffix dead` | Find address ending with `…dead` |
| `/vanity prefix 0xdead` | `0x` prefix is stripped automatically |
| `/cancel` | Cancel active search |

---

## 📊 Difficulty Guide

| Pattern Length | Expected Attempts | Approx Time (4 cores) |
|---|---|---|
| 3 chars | ~4K | < 1 second |
| 4 chars | ~65K | ~2 seconds |
| 5 chars | ~1M | ~1-2 minutes |
| 6 chars | ~16M | ~30-60 minutes |

> Each extra character is **16× harder**. Max pattern length is 6 characters.

---

## 🛡 Security Notes

- Private keys are **never logged** unless a match is found.
- Keys use Python's `secrets` module (cryptographically secure).
- **Never share your private key** with anyone.
