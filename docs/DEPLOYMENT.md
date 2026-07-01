# Deployment & Maintenance SOP — GCP `e2-micro` (Ubuntu)

Copy-paste runbook for running **portfolio-dash** on a Google Cloud `e2-micro` VM
(shared 0.25–2 vCPU, **1 GB RAM**) running **Ubuntu 24.04 LTS**. No Docker — the app is a
plain pip-installable Python 3.12 package served by `uvicorn`, storing everything in one
SQLite file. Tuned for the app's design point: **1–2 users, private, not publicly exposed.**

> Replace placeholders: `<USER>` = your Linux login, `<VM_EXTERNAL_IP>` = the VM's IP.
> Run every block as your login user; `sudo` is shown where needed.

---

## 0. Security model — read first (30 seconds)

The app starts in **guest mode** (no login) until you add a user. So **do NOT open port
8400 to the internet** while in guest mode — anyone could read/edit your portfolio. This
SOP keeps the app **private** (bound to localhost, reached over Tailscale or an SSH
tunnel). Public HTTPS is an optional appendix and requires adding a login user first.

---

## 1. Create the VM (GCP console or gcloud)

- Machine type **`e2-micro`**, **Ubuntu 24.04 LTS** (x86-64), 30 GB standard disk.
- Region: pick a US free-tier region (`us-west1` / `us-central1` / `us-east1`) if you want
  the always-free `e2-micro`.
- Firewall: **leave only SSH (22) open. Do NOT check "Allow HTTP/HTTPS".**

`gcloud` one-liner (optional):
```bash
gcloud compute instances create portfolio-dash \
  --machine-type=e2-micro --zone=us-central1-a \
  --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB
```
SSH in: `gcloud compute ssh portfolio-dash --zone=us-central1-a` (or your usual SSH).

---

## 2. One-time machine prep (swap + Python 3.12 + git)

**1 GB RAM is tight — add swap first** (prevents OOM during `pip install` and gives runtime
headroom):
```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h            # confirm 2.0Gi swap
```

System packages (Ubuntu 24.04 already ships Python 3.12):
```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3.12 python3.12-venv git
sudo apt -y install unattended-upgrades && sudo dpkg-reconfigure -plow unattended-upgrades  # auto security patches
```

---

## 3. Get the app + install (prod deps only)

```bash
cd ~ && git clone https://github.com/hefenglim/invest-portfolio.git
cd ~/invest-portfolio
git checkout v0.1.1                       # pin to a released tag
python3.12 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -e .              # EDITABLE; prod deps only — NO playwright/pytest
```
Use **`-e` (editable)**: the app locates its static frontend as `web/` next to the source
(`portfolio_dash/api/app.py` → `parents[2]/web`). An editable install keeps the package in
the cloned repo, so `web/` is found; a plain `pip install .` copies the package into
`site-packages` **without** `web/` and the frontend would 404. It installs just
`[project.dependencies]` (fastapi, uvicorn, pydantic, yfinance, FinMind, litellm,
APScheduler, pyxirr…); the dev/e2e/probe extras are NOT installed — small footprint for
`e2-micro`.

> If `pip install` gets OOM-killed, confirm swap is on (step 2) and retry.

---

## 4. Configure (optional)

Basic portfolio tracking needs **no config**. Settings load from environment / a `.env`
file in the working directory. Create one only if you want to override defaults or enable
the LLM later:
```bash
cat > ~/invest-portfolio/.env <<'EOF'
# DB_PATH=data/portfolio.db      # default; relative to the working dir
# APP_ENV=prod
# LLM keys etc. are managed in the in-app Settings page, not here (AI is OFF until set).
EOF
```
Leave VM timezone as **UTC** (default) — the app handles exchange timezones internally
(scheduler crons are tz-aware in code).

**First run auto-creates everything** (v0.1.1): the lifespan builds all SQLite tables and
seeds the 4 broker accounts on an empty `data/portfolio.db`. Nothing to migrate by hand.

---

## 5. Run it as a service (auto-restart + start on boot)

Create a systemd unit (binds to **127.0.0.1** — private by default):
```bash
sudo tee /etc/systemd/system/portfolio-dash.service >/dev/null <<EOF
[Unit]
Description=portfolio-dash
After=network-online.target
Wants=network-online.target

[Service]
User=<USER>
WorkingDirectory=/home/<USER>/invest-portfolio
ExecStart=/home/<USER>/invest-portfolio/.venv/bin/python -m uvicorn \
  portfolio_dash.api.app:create_app --factory --host 127.0.0.1 --port 8400
Restart=always
RestartSec=3
# 1 GB box: cap memory so a runaway never takes down the VM (restarts instead).
MemoryMax=700M

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now portfolio-dash
sudo systemctl status portfolio-dash --no-pager      # should be "active (running)"
curl -s http://127.0.0.1:8400/api/health             # -> {"status":"ok"}
```

---

## 6. Reach it from your laptop (pick ONE — both keep it private)

**Option A — Tailscale (recommended: private, auto-HTTPS, zero firewall changes).**
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale serve --bg 8400        # proxies localhost:8400 onto your tailnet over HTTPS
tailscale serve status                # shows the https://<machine>.<tailnet>.ts.net URL
```
Install Tailscale on your laptop/phone (same account) → open that `https://…ts.net` URL.
Only your devices can reach it; nothing is exposed to the public internet.

**Option B — SSH tunnel (no extra account, nothing installed on the VM).**
On your laptop:
```bash
ssh -L 8400:127.0.0.1:8400 <USER>@<VM_EXTERNAL_IP>
```
Then browse **http://localhost:8400/**. Close the SSH session to close access.

> **Optional — add a login user (protected mode).** Even when private, you can require a
> login: in the app go to **設定 → 帳戶/使用者**, add a user. Once ≥1 user exists the app
> switches to protected mode (login required; sessions + lock/unlock). Recommended if you
> ever expose it beyond a private tunnel.

You're live: open the dashboard → **交易輸入** to record trades. Quotes refresh via the
in-process scheduler post-market (needs outbound internet, which the VM has).

---

# Maintenance SOP

### Update to a new version
```bash
cd ~/invest-portfolio
git fetch --tags && git checkout v0.1.x           # the tag you want
./.venv/bin/pip install -e .                       # refresh deps (editable: source already updated)
sudo systemctl restart portfolio-dash
curl -s http://127.0.0.1:8400/api/health
```
Read `CHANGELOG.md` for what changed; the DB is forward-compatible (idempotent bootstrap,
append-only ledgers — your data is never rewritten).

### Backups (built-in + off-VM copy)
- **Automatic:** the scheduler runs a **daily backup at 01:30 (Asia/Taipei)** →
  `data/backups/portfolio_YYYY-MM-DD.db.gz` (keeps the latest 30; runs `PRAGMA
  integrity_check`).
- **Manual snapshot anytime:**
  ```bash
  cp ~/invest-portfolio/data/portfolio.db ~/portfolio-$(date +%F).db
  ```
- **Off-VM (do this — a VM can die):** copy the newest backup to a GCS bucket weekly.
  ```bash
  # one-time: gcloud storage buckets create gs://<your-bucket> --location=us
  gcloud storage cp "$(ls -t ~/invest-portfolio/data/backups/*.db.gz | head -1)" gs://<your-bucket>/
  ```
  (Optionally put that line in a weekly `crontab -e` entry.)

### Restore from a backup
```bash
sudo systemctl stop portfolio-dash                 # release the DB lock FIRST
cd ~/invest-portfolio
gunzip -k data/backups/portfolio_YYYY-MM-DD.db.gz  # if restoring from a .gz
make restore FILE=data/backups/portfolio_YYYY-MM-DD.db    # copies over data/portfolio.db
sudo systemctl start portfolio-dash
```
(`make` uses the repo venv; if `make` isn't installed: `sudo apt -y install make`, or just
`cp <backup> data/portfolio.db` while the service is stopped.)

### Monitor / health
```bash
systemctl status portfolio-dash --no-pager      # running?
journalctl -u portfolio-dash -n 50 --no-pager   # recent service logs
tail -n 50 ~/invest-portfolio/data/logs/app.log # app JSON logs (rotating 10MB x5)
free -h        # RAM/swap pressure
df -h /        # disk (30 GB; watch data/ + logs + backups)
```

### Routine checklist
- **Weekly:** confirm a fresh `data/backups/*.db.gz` exists; run the off-VM GCS copy.
- **Monthly:** `sudo apt update && sudo apt upgrade` (unattended-upgrades handles security
  patches automatically); reboot if a kernel update needs it (`systemctl enable` already
  makes the app auto-start on boot).
- **On any issue:** `journalctl -u portfolio-dash` first; `Restart=always` self-heals most
  transient crashes.

---

## Appendix — public HTTPS (only if you really need it)

Private access (Tailscale/SSH) is strongly preferred. If you must expose it publicly:
1. **Add a login user first** (protected mode) — never expose guest mode.
2. Point a domain's A record at `<VM_EXTERNAL_IP>`.
3. Open **tcp:80,443** in the GCP firewall (NOT 8400).
4. Install Caddy (auto-Let's-Encrypt) reverse-proxying `:443 → 127.0.0.1:8400`:
   ```bash
   sudo apt -y install caddy
   echo 'your.domain.com { reverse_proxy 127.0.0.1:8400 }' | sudo tee /etc/caddy/Caddyfile
   sudo systemctl restart caddy
   ```
Keep the app bound to `127.0.0.1` (step 5) so only Caddy can reach it.

---

## Appendix — Debian 13 (trixie): run on Python 3.12 via `uv` (NOT the system 3.13)

The main SOP assumes **Ubuntu 24.04** (which ships Python 3.12). **Debian 13 (trixie)** ships
**Python 3.13** and has no `python3.12` package — and this project **cannot run on Python 3.13**.

**Why 3.13 fails (verified on a live Debian 13 VM, 2026-07):** `FinMind` pins `lxml<5.0.0`.
lxml < 5 has **no cp313 wheel**, and its source **fails to compile on CPython 3.13**
(`error: too few arguments to function '_PyLong_AsByteArray'` — that C-API signature changed in
3.13). lxml 5.x supports 3.13, but FinMind caps it below 5. So the app must run on **Python 3.12**.

**Fix — provision Python 3.12 with `uv`** (downloads a prebuilt CPython 3.12; no compiling on the
`e2-micro`). This replaces Step 2's `python3.12` apt install and Step 3's venv creation:
```bash
# Step 2: system packages — git only (do the 2 GB swap from the main Step 2 too).
sudo apt update && sudo apt -y install git
curl -LsSf https://astral.sh/uv/install.sh | sh          # installs uv into ~/.local/bin
export PATH="$HOME/.local/bin:$PATH"

# Step 3: build the venv on a prebuilt Python 3.12, then install as usual.
cd ~/invest-portfolio
uv venv --seed --python 3.12 .venv                       # fetches CPython 3.12; --seed adds pip
./.venv/bin/python --version                             # -> Python 3.12.x
./.venv/bin/pip install -e .                             # lxml 4.9.4 installs from a cp312 wheel
```
`--seed` puts `pip` in the venv so the Maintenance "update" flow (`./.venv/bin/pip install -e .`)
keeps working unchanged. `uv` only supplies the interpreter; installation still uses the venv's pip.

Everything else is **distro-agnostic and unchanged**: swap (Step 2), the systemd unit (Step 5 —
its `ExecStart` calls `.venv/bin/python`, so it runs whatever the venv was built with, here 3.12),
Tailscale / SSH access (Step 6), and all of Maintenance. `unattended-upgrades`, `systemctl`,
`journalctl`, `make`, and `caddy` all exist on Debian 13 the same way.

> This appendix is the **Debian 13 delta only** — the Ubuntu path above is kept verbatim as the
> reference. On Debian 13 use the `uv` + Python 3.12 block above; the rest of the SOP is shared.
