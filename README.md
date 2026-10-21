# adguard-cli-proxy

Location-aware SOCKS5 proxy manager for the [AdGuard VPN CLI](https://adguard-vpn.com/en/linux/overview.html).

`adguard-cli-proxy` connects the AdGuard VPN CLI to a chosen city, exposes its
SOCKS5 listener, verifies the egress IP, and prints a canonical endpoint label:

```text
{ip}:{port}/{ISO}/{CITY-SLUG}

127.0.0.1:9111/US/LAS-VEGAS
203.0.113.10:9111/US/LAS-VEGAS
```

The `/US/LAS-VEGAS` suffix is a location label. The actual proxy URL is the
SOCKS5 address printed next to it (`socks5h://…`), because SOCKS URLs do not
carry a path.

## Why

AdGuard VPN CLI already has a SOCKS mode, but its commands are stateful and do
not expose a stable, location-labelled endpoint. This wrapper makes the
workflow repeatable:

- resolve `US/LAS-VEGAS`, `Las Vegas`, `US`, or `United States` to a real
  AdGuard VPN location;
- switch the CLI to SOCKS mode with a predictable host/port;
- connect, wait for the listener, and check the public egress IP;
- generate authentication automatically for non-loopback binds;
- store the active endpoint in `~/.config/adguard-cli-proxy/state.json`;
- provide `status`, `list`, `resolve`, and `doctor` commands for automation.

## Endpoint examples

| Where the proxy runs | Endpoint label | Proxy URL |
| --- | --- | --- |
| Local machine | `127.0.0.1:9111/US/LAS-VEGAS` | `socks5h://127.0.0.1:9111` |
| Public VPS | `203.0.113.10:9111/US/LAS-VEGAS` | `socks5h://adg:<password>@203.0.113.10:9111` |
| LAN server | `192.168.1.20:9111/US/LAS-VEGAS` | `socks5h://adg:<password>@192.168.1.20:9111` |

For public binds, a random username/password is generated unless you pass
`--username` and `--password`. AdGuard VPN CLI refuses to expose its SOCKS
listener without authentication.

## Requirements

- Linux or macOS
- [AdGuard VPN CLI](https://adguard-vpn.com/en/linux/overview.html), logged in
  and able to connect (a subscription is required for most locations)
- Python 3.9+

## Install

```bash
git clone https://github.com/GalaxyFour/adguard-cli-proxy.git
cd adguard-cli-proxy

python3 -m venv .venv
.venv/bin/pip install .

# Optional: make the command available on your PATH
mkdir -p ~/.local/bin
ln -sf "$PWD/.venv/bin/adguard-cli-proxy" ~/.local/bin/adguard-cli-proxy
```

You can also run it directly from the checkout without installing:

```bash
./adguard-cli-proxy --help
```

## Quick start

```bash
# Connect to Las Vegas and expose SOCKS5 on 127.0.0.1:9111
adguard-cli-proxy up US/LAS-VEGAS

# Example output
# Connected: US/LAS-VEGAS (Las Vegas, United States)
# Listen:    127.0.0.1:9111
# Endpoint:  127.0.0.1:9111/US/LAS-VEGAS
# Proxy:     socks5h://127.0.0.1:9111
# Egress:    185.242.5.198
```

Use it:

```bash
curl --socks5-hostname 127.0.0.1:9111 https://api.ipify.org
```

Check and stop it:

```bash
adguard-cli-proxy status
adguard-cli-proxy down
```

## Public / remote access

```bash
adguard-cli-proxy up US/LAS-VEGAS --host 0.0.0.0 --port 9111
```

The tool advertises the host's public IP when it can detect it. If you are
behind NAT, pass the address clients should use explicitly:

```bash
adguard-cli-proxy up US/LAS-VEGAS \
  --host 0.0.0.0 \
  --port 9111 \
  --advertise 203.0.113.10
```

Then allow TCP port `9111` in the firewall and connect with the generated
credentials shown in the `Proxy:` line:

```bash
curl --proxy socks5h://adg:<password>@203.0.113.10:9111 https://api.ipify.org
```

> **Security:** a public SOCKS listener is an open door unless it is
> authenticated and firewalled. Keep `--host 127.0.0.1` if you do not need
> remote access, or put the proxy behind WireGuard/SSH/a private network.
> The state file contains the generated password and is written with mode
> `0600`.

If automatic public-IP detection is undesirable, use `--no-public-ip` with a
wildcard bind to advertise the LAN address instead:

```bash
adguard-cli-proxy up US/LAS-VEGAS --host 0.0.0.0 --no-public-ip
```

## Multiple locations at once

AdGuard VPN CLI normally supports one active tunnel per data directory. This
tool can run **one independent AdGuard daemon per named instance**, each with
its own data directory, account session, SOCKS port, and state file.

For example, run Las Vegas and Jakarta simultaneously:

```bash
adguard-cli-proxy up US/LAS-VEGAS --instance us-las-vegas --port 9111
adguard-cli-proxy up ID/JAKARTA  --instance id-jakarta  --port 9112
```

```text
Instance:  us-las-vegas
Endpoint:  127.0.0.1:9111/US/LAS-VEGAS
Proxy:     socks5h://127.0.0.1:9111

Instance:  id-jakarta
Endpoint:  127.0.0.1:9112/ID/JAKARTA
Proxy:     socks5h://127.0.0.1:9112
```

Your application then uses:

```bash
curl --socks5-hostname 127.0.0.1:9111 https://api.ipify.org   # Las Vegas
curl --socks5-hostname 127.0.0.1:9112 https://api.ipify.org   # Jakarta
```

Show all active instances:

```bash
adguard-cli-proxy status --all
adguard-cli-proxy status --all --probe --json
```

How instances work:

- `--instance NAME` uses
  `~/.local/share/adguard-cli-proxy/instances/NAME` as AdGuard's
  `AGVPN_CLI_DATA_PATH`.
- On first use, the existing default AdGuard login session
  (`adguardvpn-cli.conf`) is copied into the new instance directory. Use
  `--no-copy-login` to opt out, or `--data-dir` to choose another directory.
- Each instance launches its own AdGuard VPN daemon and SOCKS listener.
- Stop one instance with `down --instance NAME`; other instances stay running.
- Each simultaneous tunnel may count toward your AdGuard plan/device limit.
  Check your subscription before running many instances.

> The instance path is independent from the default `adguardvpn-cli` state.
> Running `adguard-cli-proxy down` without `--instance` does not stop named
> instances.

## Autostart with systemd

The repository includes `contrib/adguard-cli-proxy@.service`, a user-level
systemd template that starts one instance per environment file.

For a stable install that survives moving the source checkout:

```bash
python3 -m venv ~/.local/share/adguard-cli-proxy/venv
~/.local/share/adguard-cli-proxy/venv/bin/pip install \
  /path/to/adguard-cli-proxy
mkdir -p ~/.local/bin
ln -sf ~/.local/share/adguard-cli-proxy/venv/bin/adguard-cli-proxy \
  ~/.local/bin/adguard-cli-proxy
```

Install the unit and create one environment file per location:

```bash
mkdir -p ~/.config/systemd/user ~/.config/adguard-cli-proxy/instances
cp contrib/adguard-cli-proxy@.service ~/.config/systemd/user/

cat > ~/.config/adguard-cli-proxy/instances/us-las-vegas.env <<'EOF'
LOCATION=US/LAS-VEGAS
HOST=127.0.0.1
PORT=9111
EOF

cat > ~/.config/adguard-cli-proxy/instances/id-jakarta.env <<'EOF'
LOCATION=ID/JAKARTA
HOST=127.0.0.1
PORT=9112
EOF

systemctl --user daemon-reload
systemctl --user enable --now adguard-cli-proxy@us-las-vegas.service
systemctl --user enable --now adguard-cli-proxy@id-jakarta.service
```

Check it:

```bash
systemctl --user status adguard-cli-proxy@us-las-vegas.service
systemctl --user status adguard-cli-proxy@id-jakarta.service
adguard-cli-proxy status --all --probe
journalctl --user -u 'adguard-cli-proxy@*' -n 50
```

For boot without an interactive login, enable lingering once:

```bash
sudo loginctl enable-linger "$USER"
```

A system-wide unit is also available in
[`contrib/adguard-cli-proxy.service`](contrib/adguard-cli-proxy.service) for a
single instance. Edit `User=YOUR_USER` before installing it.

## Commands

### `up`

```text
adguard-cli-proxy up LOCATION [options]

  --host HOST            Listen address (default: 127.0.0.1)
  --port PORT            Listen port (default: 9111)
  --advertise HOST       Host/IP to print in the endpoint label
  --no-public-ip         Advertise the LAN IP for a wildcard bind
  --no-auth              Disable SOCKS auth (loopback only)
  --username USER        Explicit SOCKS username
  --password PASS        Explicit SOCKS password
  --wait SECONDS         Tunnel wait timeout (default: 30)
  --skip-egress-check    Do not verify the public IP through the proxy
  --instance NAME        Named daemon/data dir for simultaneous locations
  --data-dir DIR         Override the AdGuard data dir for --instance
  --no-copy-login        Do not seed a new instance from the default login
  --json                 Machine-readable output
```

Location resolution accepts:

```bash
adguard-cli-proxy up US/LAS-VEGAS
adguard-cli-proxy up "Las Vegas"
adguard-cli-proxy up las-vegas
adguard-cli-proxy up US                # fastest US city
adguard-cli-proxy up "United States/Las Vegas"
adguard-cli-proxy up US/LAS-VEGAS --instance us-las-vegas --port 9111
adguard-cli-proxy up ID/JAKARTA --instance id-jakarta --port 9112
```

### `down`

Disconnects AdGuard VPN and removes the active endpoint state.

```bash
adguard-cli-proxy down
adguard-cli-proxy down --instance us-las-vegas
```

### `status`

```bash
adguard-cli-proxy status
adguard-cli-proxy status --json
adguard-cli-proxy status --probe          # re-check egress IP through the proxy
adguard-cli-proxy status --check          # exit 3 when disconnected
adguard-cli-proxy status --all            # every configured instance
adguard-cli-proxy status --all --probe --json
```

### `list`

Lists all AdGuard VPN locations as endpoint labels:

```bash
adguard-cli-proxy list
adguard-cli-proxy list --country US
adguard-cli-proxy list --search vegas
adguard-cli-proxy list --json
```

### `resolve`

Prints only the canonical label, useful in scripts:

```bash
$ adguard-cli-proxy resolve "Las Vegas"
US/LAS-VEGAS
```

### `doctor`

Checks the CLI, version, login, license, VPN state, listener, and optionally the
egress IP:

```bash
adguard-cli-proxy doctor
adguard-cli-proxy doctor --probe
adguard-cli-proxy doctor --instance us-las-vegas --probe
```

## JSON output

`up`, `down`, `status`, `list`, `resolve`, and `doctor` support `--json`.

```bash
adguard-cli-proxy up US/LAS-VEGAS --json
```

```json
{
  "ok": true,
  "instance": null,
  "location": {
    "iso": "US",
    "country": "United States",
    "city": "Las Vegas",
    "ping": 254
  },
  "slug": "US/LAS-VEGAS",
  "listen": "127.0.0.1:9111",
  "endpoint": "127.0.0.1:9111/US/LAS-VEGAS",
  "proxy_url": "socks5h://127.0.0.1:9111",
  "egress_ip": "185.242.5.198",
  "healthy": true,
  "authenticated": false,
  "state_file": "/home/user/.config/adguard-cli-proxy/state.json"
}
```

Extract the proxy URL for another program:

```bash
PROXY=$(adguard-cli-proxy status --json | jq -r .proxy_url)
curl --proxy "$PROXY" https://api.ipify.org
```

`status --all --json` returns a list, one object per instance:

```bash
adguard-cli-proxy status --all --json \
  | jq -r '.[] | "\(.instance // "default")\t\(.proxy_url)"'
```

## How it works

1. `adguardvpn-cli list-locations` is parsed into `(ISO, country, city)` records.
2. The location query is resolved and formatted as `ISO/CITY-SLUG`.
3. `--instance` selects a separate `AGVPN_CLI_DATA_PATH`, so each instance gets
   its own AdGuard daemon, account session, SOCKS port, and state file.
4. The CLI is switched to SOCKS mode and configured with the requested
   host/port, plus credentials when the bind is not loopback.
5. `adguardvpn-cli connect` selects the city.
6. The wrapper waits for the SOCKS listener to accept TCP connections.
7. A SOCKS5 health check fetches the public IP through the proxy.
8. The endpoint label and proxy URL are saved to the state file and printed.

## Limitations

- AdGuard VPN CLI supports one active connection **per data directory**. Use
  `--instance` to run independent locations in parallel; without it, only one
  default endpoint is active.
- Each instance is a separate daemon and may count toward your AdGuard
  subscription's simultaneous-device/connection limit.
- The `/US/LAS-VEGAS` endpoint is a human-readable label. Use the accompanying
  `socks5h://` URL in proxy clients.
- AdGuard's SOCKS listener and subscription limits still apply.
- Public IP detection can be wrong behind CGNAT, multiple NATs, or a
  load-balanced ingress. Use `--advertise` to override it.
- The tool manages each instance's AdGuard VPN CLI configuration (`SOCKS` mode,
  host, port, credentials). It does not touch TUN routing rules while using
  SOCKS mode.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e .

PYTHONPATH=src python3 -m unittest discover -s tests -v
```

No runtime dependencies outside the Python standard library.

## License

MIT © 2026 GalaxyFour
