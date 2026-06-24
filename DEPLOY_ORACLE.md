# Deploy on Oracle Cloud "Always Free" (24/7, $0)

The flaky part was the runtime: GitHub Actions cron drops scheduled runs, so the
bot barely executed. This moves the executor to an always-on Oracle free VM
running a **persistent loop** (reliable every 15 min) under systemd
(auto-restart on crash/reboot). The strategy + risk code is unchanged.

## 1. Create the free VM (one-time, ~10 min)
1. Sign up at **cloud.oracle.com** → "Always Free" eligible (no charge).
2. Compute → Instances → **Create instance**.
   - Image: **Ubuntu 22.04**.
   - Shape: **VM.Standard.A1.Flex** (Ampere ARM, Always Free — pick 1 OCPU /
     6 GB) or **VM.Standard.E2.1.Micro** (x86 Always Free).
   - Add your SSH public key (or let it generate one — save the private key).
3. Create. Note the **public IP**.

## 2. Connect and install
```bash
ssh ubuntu@<PUBLIC_IP>
sudo apt update && sudo apt install -y python3-venv git
git clone https://github.com/Rottenegg360/Trading-automation.git
cd Trading-automation
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 3. Add your Alpaca PAPER keys
```bash
cp .env.example .env
nano .env      # paste ALPACA_API_KEY and ALPACA_SECRET_KEY, save (Ctrl-O, Ctrl-X)
```

## 4. Prime once, then verify a dry cycle
```bash
.venv/bin/python -m backtest.live.run --prime
.venv/bin/python -m backtest.live.run --broker-test    # reads account, no orders
```
You should see it connect, prime 7 cursors, and (in broker-test) print any
`[would bracket]` signals.

## 5. Install the always-on service
```bash
sudo cp deploy/trading-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot
sudo systemctl status trading-bot          # should say "active (running)"
journalctl -u trading-bot -f               # live logs; Ctrl-C to stop watching
```
The loop now runs every 15 min, places paper bracket orders during US market
hours, and restarts itself if it ever crashes or the VM reboots.

## 6. Turn OFF the GitHub Actions cron (avoid double-trading)
The VM is the executor now. In the repo on github.com:
- Actions tab → **paper-trade** → "..." menu → **Disable workflow**.
- Actions tab → **daily-briefing** → **Disable workflow** (the VM will do the
  briefing — see below).

## 7. (Optional) Daily briefing from the VM
Have the VM generate the briefing and push it so your Cowork/app routine keeps
showing it. First give the VM push access (a fine-grained GitHub token with
`contents:write`, or a deploy key), then add a cron:
```bash
crontab -e
# 02:05 UTC = 09:05 Asia/Bangkok — generate + commit the briefing
5 2 * * * cd /home/ubuntu/Trading-automation && .venv/bin/python -m backtest.live.report --daily --broker > briefings/latest.md 2>/dev/null && git add briefings/latest.md paper_state.db && git commit -m "briefing+state [skip ci]" && git push
```

## Notes
- **Equities only** → the bot only needs US market hours; the loop idles
  (no trades) outside them, which is correct.
- **Catch-up replay** is built in: if a cycle is ever missed (restart), the next
  one recovers signals from the last few bars instead of dropping them.
- Check status any time: `journalctl -u trading-bot -n 50` or
  `.venv/bin/python -m backtest.live.run --status`.
- Stop/restart: `sudo systemctl stop|restart trading-bot`.
