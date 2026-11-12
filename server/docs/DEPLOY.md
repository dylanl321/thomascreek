# Running the hub on TrueNAS

## 1. Datasets

Create two datasets (names are examples):

- `POOL/apps/thomascreek-hub/data`: config, vault, database, TLS key, house assets. Include it in snapshots.
- `POOL/apps/thomascreek-hub/secrets`: only the master key file. Back up the key **offline**, separately from the data snapshots; the vault is useless without it, and the key alone is useless without the vault.

## 2. Install as a custom app

Apps > Discover Apps > Custom App > Install via YAML, paste `compose.yaml` with the
two host paths filled in (and the branch in the build URL if it is not `main`).
TrueNAS builds the image straight from GitHub; no registry is involved.
Host networking is used so Bonjour works on home Wi-Fi and the hub sees each
phone's real address.

On first start the hub writes the master key into the secrets folder (root-only,
never printed), makes its TLS certificate and an empty vault.

The config lives in `data/config.yaml` (start from `config.example.yaml`), or in a
file named by `HUB_CONFIG_FILE`. `dry_run: true` is practice mode: reads are real
and writes are only logged. `dry_run: false` makes every action live.

## 3. Secrets

Open a shell in the container (Apps > thomascreek-hub > Shell, or
`docker exec -it ix-thomascreek-hub-hub-1 sh`) and store each credential. Each
command prompts without echo and never prints the value back:

```sh
hub secrets set home_assistant.token
hub secrets set plex.token
hub secrets set truenas.api_key
hub secrets set jellyseerr.api_key
hub secrets set sonarr.api_key
hub secrets set radarr.api_key
hub secrets set mealie.token
hub secrets set homebox.password
hub secrets set show_player.app_token
hub secrets set frigate.password          # only if using Frigate's authenticated port
hub secrets set firewalla.msp_token
hub secrets set guest_wifi.password
hub secrets set apns.auth_key_p8          # paste the .p8 contents, then Ctrl-D
hub secrets list
```

Recommended: give the hub its own **non-admin Home Assistant user** and token, a
**read-only TrueNAS API key** for a dedicated user, and a Plex token for a managed user.

### Or: from 1Password

Instead of pasting each value, keep them in a 1Password vault named
`Thomas Creek Hub`, one item per secret, titled with the secret's name above
(`home_assistant.token`, `plex.token`, ...). Put the value in the item's
password or credential field; store the APNs `.p8` as a Document item titled
`apns.auth_key_p8`. Create a service account with read access to that vault,
set `onepassword.enabled: true`, and store its token once:

```sh
hub secrets set onepassword.service_account_token
hub secrets sync          # copies now; the hub also syncs on start and hourly
```

The values are copied into the hub's own vault, so the hub keeps working if
1Password is unreachable, and a token rotated in 1Password reaches the hub
within the hour. Names and counts are logged, never values.

## 4. Check every connection (read-only)

```sh
hub check
```

## 5. Make Dylan the admin

Open the app on home Wi-Fi; it enrolls itself. Then:

```sh
hub devices list
hub bootstrap-admin Dylan dev_xxxxxxxx
```

Every later phone shows up as a "New iPhone joined" notice on Dylan's phone.

## Hub web page

`https://<hub>:8443/admin` from the home network shows each service's health and lists every key and
token (set or missing, never the value). Replace one by pasting or choosing a file; for Apple push,
choose the `AuthKey_XXXXXXXXXX.p8` file and the key ID is read from its name. Sign in with the app
(Systems, Hub web page): type the code the page shows and confirm with Face ID. The browser warns
about the hub's own certificate the first time; that is expected (the app pins it instead).

## 6. Turn things on, one at a time

With `dry_run: true`, add action names to `hub.live_actions` and restart the app,
for example `[light.set, scene.run]` first. With `dry_run: false` everything is live. Names: see docs/API.md, plus `lights.control`,
`media.approve`, `chores.done`, `chores.redeem`, `shopping.add`.

## VPN through the Firewalla (recommended)

The Firewalla stays the VPN server; the hub only hands its client configs to phones.

1. In the Firewalla app: VPN Server, WireGuard, turn it on, then add one client per
   phone you expect away from home (five is plenty). Name them anything.
2. Export each client's config file (Share) and save it as `vpn.pool.1.conf`,
   `vpn.pool.2.conf`, ...
3. Put the files in the hub's drop folder (`data/import/`). Within 15 seconds they
   are moved into the vault and wiped.
4. In `config.yaml` set `vpn.provider: pool`, `allowed_ips` to the home ranges (so
   only home traffic uses the tunnel) and `home_ssids`. Restart.

Each approved phone gets a free client. When a phone is removed, its client is
deleted from the vault, because that phone still has its key: delete the same
client on the Firewalla, make a new one, and drop it in under the same name.

## Built-in VPN (alternative: the hub is the server)

1. In `config.yaml` set `vpn.provider: wg-cli`, `vpn.endpoint` (public name or IP and port), `allowed_ips`, `dns`, `home_ssids`.
2. In `compose.yaml` set `HUB_VPN_NAT: "1"` (keeps `cap_add: NET_ADMIN`).
3. On the Firewalla, forward UDP 51820 to the NAS.
4. Restart. The hub creates `wg0`, makes its server key in the vault, and re-adds every active phone.

## Family Command Center

Set `family_board.url` and `enabled: true`. On the LAN no credential is needed:
completing chores and redeeming rewards are the board's "family actions", which it
accepts from a private address. Link each person once, in the app (People) or with
`PATCH /v1/people/{id} {family_board_id}`. Turn writes on with `chores.done` and
`chores.redeem` in `live_actions`.

## Kids' internet time (Firewalla MSP)

The hub calls the Firewalla MSP API directly. Create a personal access token in MSP
(Account Settings, Create New Token), then:

```sh
hub secrets set firewalla.msp_token
```

Set `firewalla.enabled: true` and `firewalla.msp_domain`, and add `network.setup`,
`network.pause` and `network.grant` to `live_actions`. In the app, open a child under
People and pick their devices from the Firewalla list. For each device the hub creates
one rule (block, target internet, scoped to that MAC, noted "Thomas Creek hub") and
pauses it at once, so nothing changes until someone pauses that child's internet.
The MSP API has no delete, so unlinking a device pauses its rule and leaves it to be
deleted in the Firewalla app.

## House assets

Put `house-model-web.json` and the house photo under `data/assets/` (they stay out of the public repo).
