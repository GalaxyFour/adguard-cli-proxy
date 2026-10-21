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
  --json                 Machine-readable output
```

Location resolution accepts:

```bash
adguard-cli-proxy up US/LAS-VEGAS
adguard-cli-proxy up "Las Vegas"
adguard-cli-proxy up las-vegas
adguard-cli-proxy up US              # fastest US city
adguard-cli-proxy up "United States/Las Vegas"
```

### `down`

Disconnects AdGuard VPN and removes the active endpoint state.

### `status`

```bash
adguard-cli-proxy status
adguard-cli-proxy status --json
adguard-cli-proxy status --probe      # re-check egress IP through the proxy
adguard-cli-proxy status --check      # exit 3 when disconnected
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
```

## JSON output

`up`, `down`, `status`, `list`, `resolve`, and `doctor` support `--json`.

```bash
adguard-cli-proxy up US/LAS-VEGAS --json
```

```json
{
  "ok": true,
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

## systemd

Copy [`contrib/adguard-cli-proxy.service`](contrib/adguard-cli-proxy.service),
replace `YOUR_USER`, then install it:

```bash
sudo cp adguard-cli-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now adguard-cli-proxy
```

The service runs `up US/LAS-VEGAS` on start and `down` on stop. Keep the listen
host at `127.0.0.1` unless you have configured authentication and a firewall.

## How it works

1. `adguardvpn-cli list-locations` is parsed into `(ISO, country, city)` records.
2. The location query is resolved and formatted as `ISO/CITY-SLUG`.
3. The CLI is switched to SOCKS mode and configured with the requested
   host/port, plus credentials when the bind is not loopback.
4. `adguardvpn-cli connect` selects the city.
5. The wrapper waits for the SOCKS listener to accept TCP connections.
6. A SOCKS5 health check fetches the public IP through the proxy.
7. The endpoint label and proxy URL are saved to the state file and printed.

## Limitations

- AdGuard VPN CLI can be connected to **one location at a time**. This tool
  exposes one active endpoint; it is not a multi-location proxy pool.
- The `/US/LAS-VEGAS` endpoint is a human-readable label. Use the accompanying
  `socks5h://` URL in proxy clients.
- AdGuard's SOCKS listener and subscription limits still apply.
- Public IP detection can be wrong behind CGNAT, multiple NATs, or a
  load-balanced ingress. Use `--advertise` to override it.
- The tool manages the user's AdGuard VPN CLI configuration (`SOCKS` mode,
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
