# Android phone → crawlfast worker node

Turn spare Android phones into pull-based crawl nodes. Phones are actually *great* nodes for this
workload: always-on, silent, ~2W at the wall, built-in UPS (the battery), and the worker is
**pure Python** (`requests` + `PyYAML` — no browser, no Docker, no root needed).

> **TL;DR — you do NOT need to root the phone.** The worker runs under
> [Termux](https://termux.dev) as a normal app. Root/rooting is an *optional* advanced path
> (Path C) that buys you a real Linux and slightly better reliability, at the cost of hours per
> device and possibly bricking it. Start every phone on Path A; only root the ones you want to
> commit to permanently.

The existing fleet tooling was built for Ubuntu boxes and does **not** map 1:1 to Android:

| Ubuntu tooling | On Android |
|---|---|
| `setup-node.sh --service` (systemd) | ❌ no systemd → Termux:Boot script + wake-lock instead |
| `bootstrap.sh` (Docker container) | ❌ no Docker without a custom kernel → plain Python |
| avahi / `<name>.local` mDNS | ❌ Termux can't resolve `.local` → use prod URL or a LAN **IP** |
| `advertcafe nodes onboard` (SSH, sudo) | ⚠️ partially — Termux sshd is port **8022**, single user, no sudo |
| Key minting (`POST /api/v1/cli/nodes`) | ✅ identical — server side doesn't care what the node is |
| Worker loop, claims, heartbeats | ✅ identical — same repo, same `config.yaml` |

---

## Path A (recommended, no root): Termux

Works on any Android 7+ phone. ~15 minutes per device.

### 1. Prep the phone (on the phone)

1. Factory reset (optional but nice for old phones). Skip Google account sign-in if you can.
2. Connect to your Wi-Fi. In the Wi-Fi network settings, note the phone's **IP** and ideally set a
   **static IP / DHCP reservation** on your router — Termux can't do mDNS, so a stable IP is your
   substitute for `<name>.local`.
3. **Install Termux from F-Droid or GitHub, NOT Google Play** (the Play build is abandoned and
   broken): https://f-droid.org/packages/com.termux/ . Also install **Termux:Boot** (same source)
   for start-on-boot.
4. Disable battery optimization for Termux and Termux:Boot:
   *Settings → Apps → Termux → Battery → Unrestricted* (wording varies per vendor). On aggressive
   vendors (Xiaomi/MIUI, Samsung, Huawei) also check https://dontkillmyapp.com for your model —
   this is the #1 cause of "my phone node went offline overnight".
5. *Settings → Display*: screen timeout short is fine; the wake-lock below keeps the CPU alive
   with the screen off.

### 2. Mint the node's key (on your machine, same as any node)

Same as the Ubuntu runbook / `onboard-crawlfast-node` skill — the server doesn't care that the
node is a phone. Name phones distinctly, e.g. `crawlfast-phone1`:

```bash
# prod
curl -sX POST https://api.crawlfa.st/api/v1/cli/nodes \
  -H "X-Crawlfast-Internal-Secret: $EXTERNAL_WORKER_ADMIN_SECRET" \
  -H 'Content-Type: application/json' -d '{"name":"crawlfast-phone1"}'
# → copy .data.api_key (cfw_...) — shown ONCE

# or, if the advertcafe CLI is configured (~/.advertcaferc):
advertcafe nodes create --name crawlfast-phone1     # if the subcommand exists in your CLI build;
                                                    # otherwise use the curl above — `nodes onboard`
                                                    # itself won't work against a phone (see §SSH)
```

### 3. Install the worker (in Termux, on the phone)

Paste this block into Termux:

```bash
pkg update -y && pkg install -y python git openssh
git clone https://github.com/pamfilico/crawlfast-external-worker.git
cd crawlfast-external-worker
pip install -r requirements.txt
```

Write the config (same file as every other node — this is the node's whole world):

```bash
cat > config.yaml <<'YAML'
api_base_url: "https://api.crawlfa.st"     # or http://<dev-box-LAN-IP>:5099 — IP, not .local!
api_key: "cfw_PASTE_THE_KEY_HERE"
worker_name: "crawlfast-phone1"
poll_interval_seconds: 5
YAML
chmod 600 config.yaml
```

> **Dev suite note:** the cfext_lanproxy socat trick still applies on the server side, but point
> the phone at the dev box's **IP** (`http://192.168.1.x:5099`), not `my-macbook.local` — Termux
> has no mDNS resolver.

### 4. Smoke-test it

```bash
python -m crawlfast_external_worker.worker --health    # ping the API
python -m crawlfast_external_worker.worker --once      # one full cycle
python -m crawlfast_external_worker.worker             # poll loop (Ctrl-C to stop)
```

Verify from your machine exactly like any node — `last_seen` within seconds of now:

```bash
curl -s https://api.crawlfa.st/api/v1/cli/nodes -H "X-Crawlfast-Internal-Secret: $SEC" \
  | jq -r '.data[]|select(.name=="crawlfast-phone1")|"\(.name): last_seen=\(.last_seen_at)"'
# or: advertcafe nodes list
```

### 5. Make it survive reboots + screen-off (the systemd substitute)

Termux:Boot runs everything in `~/.termux/boot/` at boot. Create a start script that takes a
wake-lock (keeps the CPU running with the screen off), self-updates, and supervises the worker
(the `Restart=always` substitute):

```bash
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/crawlfast-worker.sh <<'SH'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd ~/crawlfast-external-worker
while true; do
  git pull --ff-only >/dev/null 2>&1 || true          # poor-man's self-update.sh
  pip install -q -r requirements.txt >/dev/null 2>&1 || true
  python -m crawlfast_external_worker.worker >> ~/worker.log 2>&1
  sleep 5                                             # crashed? restart, like RestartSec=5
done
SH
chmod +x ~/.termux/boot/crawlfast-worker.sh
```

Reboot the phone once to confirm: it should heartbeat within ~a minute of booting, with the
screen locked, untouched. (Open Termux:Boot once after installing it so Android registers it.)
Because the loop `git pull`s before every worker start, pushing a fix to the worker repo heals
phones the same way `self-update.sh` heals the Docker fleet — the phone picks it up on its next
crash/restart/reboot; force it by rebooting the phone or `pkill -f crawlfast_external_worker`
over SSH.

### 6. (Optional) SSH into the phone for remote management

```bash
# on the phone, in Termux:
passwd                 # set a password (or drop your pubkey in ~/.ssh/authorized_keys)
sshd                   # listens on port 8022
echo "sshd" > ~/.termux/boot/00-sshd.sh && chmod +x ~/.termux/boot/00-sshd.sh   # sshd on boot

# from your machine:
ssh -p 8022 <phone-ip>          # any username works; Termux has a single user
ssh-copy-id -p 8022 <phone-ip>  # make it key-based
```

**Why `advertcafe nodes onboard` doesn't work as-is against a phone:** it assumes port 22,
`sudo`/`CRAWLFAST_SUDO_PASS`, apt, Docker, and systemd — none of which exist in Termux. Use it
for laptops/Pis; onboard phones with this doc. (If the phone fleet grows, the natural move is a
`setup-node-termux.sh` in this repo — same flags, Termux packages, writes the boot script above —
and an `--android` flag / port-8022 support in the CLI.) Recording the phone's SSH coordinates
server-side still works and keeps the fleet inventory honest:
`POST /api/v1/cli/nodes/<id>/ssh {host: "<phone-ip>", port: 8022, username: "termux"}`.

### Per-phone checklist (repeat for each spare phone)

1. Wi-Fi + DHCP reservation → note IP
2. F-Droid → Termux + Termux:Boot; battery optimization OFF for both
3. Mint key server-side (`crawlfast-phoneN`)
4. Termux: install block + `config.yaml` + smoke test
5. Boot script + wake-lock; reboot; verify `last_seen`
6. Optional sshd; record SSH coords server-side
7. Leave it plugged in, screen off, on a shelf

---

## Path B (no root): proot-distro Ubuntu inside Termux

If you want the node to look like the rest of the Ubuntu fleet (so more of the existing scripts
work verbatim), run a real Ubuntu userland inside Termux — still **no root**:

```bash
pkg install -y proot-distro
proot-distro install ubuntu
proot-distro login ubuntu
# now you're in Ubuntu: apt works, and setup-node.sh mostly works:
apt update && apt install -y git python3 python3-pip python3-venv
git clone https://github.com/pamfilico/crawlfast-external-worker.git
cd crawlfast-external-worker
./setup-node.sh --api-url https://api.crawlfa.st --api-key cfw_XXX --name crawlfast-phone1
#              ^ foreground mode. --service will FAIL: proot has no systemd. No avahi either.
```

Autostart is still the Termux:Boot script from Path A, just wrapped:
`proot-distro login ubuntu -- bash -lc 'cd ~/crawlfast-external-worker && exec python3 -m crawlfast_external_worker.worker'`.

Verdict: **only worth it if script-parity matters to you.** It adds a ~1GB rootfs and ~10–20%
syscall overhead (proot ptrace) for zero functional gain — the worker needs nothing Ubuntu-specific.
Path A is the better default.

---

## Path C (root / advanced): when and how

Root is **not required** for anything above. What rooting actually buys a node:

- **Kill vendor battery managers for good** — on the worst OEM skins (MIUI etc.) even
  "unrestricted" apps get murdered after days; root lets you disable the manager itself.
- **chroot instead of proot** — full-speed Linux userland (vs proot's ptrace tax).
- **Real Linux on the phone** — postmarketOS (best for old devices, if yours is
  [supported](https://wiki.postmarketos.org/wiki/Devices)) turns the phone into a genuine Alpine
  Linux box: real init, sshd on 22, and `setup-node.sh` / the CLI work like on any Linux node.
- Underclocking/charge-limit control (battery longevity for a 24/7 device).

What it costs: bootloader unlock **wipes the device**, the procedure is *per-manufacturer* (some —
recent Huawei, many carrier-locked US models — can't be unlocked at all), and a mistake can brick
the phone. General shape, details vary by model — look yours up on XDA Forums first:

1. **Unlock the bootloader**: enable Developer Options → *OEM unlocking* + *USB debugging*; then
   `adb reboot bootloader` and `fastboot flashing unlock` (Pixels) / `fastboot oem unlock` — or
   the vendor's unlock-token flow (Xiaomi has a waiting period; Samsung uses Download Mode, and
   note Samsung has **no** fastboot — Odin/Heimdall instead).
2. **Root with Magisk** (if staying on Android): grab your exact firmware's `boot.img`, patch it
   in the Magisk app, `fastboot flash boot magisk_patched.img`. Then Termux can `pkg install tsu`
   and you can chroot / kill battery managers.
3. **Or replace Android entirely** (the nicest end-state for a dedicated node): flash
   **postmarketOS** if the device is supported — after that the phone *is* an Ubuntu-class node
   and the standard `onboard-crawlfast-node` flow applies (sshd on 22, real user, doas/sudo,
   `setup-node.sh --service` works via OpenRC's equivalent or a simple init script).

Recommendation: run each phone on Path A for a week. If it holds a heartbeat 24/7 (most do once
battery optimization is off), **don't root it** — you'd be spending an evening per device to fix a
problem you don't have. Root/postmarketOS only the ones that keep getting killed by the vendor OS,
or if you want the fleet uniform under the existing SSH tooling.

---

## Ops notes for a phone shelf

- **Power**: leave them plugged in. A 24/7-charging Li-ion ages fast; if the phone (or root) offers
  a charge limit (~80%), use it. A cheap smart plug on a cycle (e.g. on 1h / off 3h) also works —
  the battery doubles as a UPS during the off phase.
- **Heat**: no cases, don't stack them, screen brightness 0 / screen off. The BFS crawler is
  network-bound, so thermals are rarely an issue.
- **Wi-Fi sleep**: the wake-lock covers CPU, and modern Android keeps Wi-Fi up while charging; if a
  phone still drops off Wi-Fi, look for *"Keep Wi-Fi on during sleep"* / disable "Wi-Fi power
  saving" in developer options.
- **Throughput**: one Termux worker per phone is right-sized. All nodes share the same atomic
  claim queue (`FOR UPDATE SKIP LOCKED`), so 5 phones ≈ 5× `crawl_all` throughput with zero
  coordination — parallelism is per-site, same as the rest of the fleet.
- **Same critical rules as every node**: pure REST, only `api_base_url` + `api_key` +
  `worker_name` on the device, key shown once at mint and lives only in `config.yaml` (chmod 600).
  Never put DB/Spaces/app secrets on a phone.
