# Running the hub on TrueNAS

Nothing in these steps changes Home Assistant or any other system: the hub
starts in practice mode (`dry_run: true`), reads real data, and only logs what it
would have done. Turn actions on later, one at a time.

## 1. Datasets

Create two datasets (names are examples):

- `POOL/apps/thomascreek-hub/data`: config, vault, database, TLS key, house assets. Include it in snapshots.
- `POOL/apps/thomascreek-hub/secrets`: only the master key file. Root-only permissions. Back up the key **offline**, separately from the data snapshots; the vault is useless without it, and the key alone is useless without the vault.

## 2. Build the image on the NAS

```sh
git clone https://github.com/dylanl321/thomascreek && cd thomascreek/server
docker build -t thomascreek-hub:0.1.0 .
```

## 3. Master key, config, secrets

```sh
docker run --rm -v /mnt/POOL/apps/thomascreek-hub/secrets:/s thomascreek-hub:0.1.0 init-key /s/hub_master_key
cp config.example.yaml /mnt/POOL/apps/thomascreek-hub/data/config.yaml   # then edit addresses
```

Store each credential (prompts without echo, never printed back):

```sh
alias hub='docker run --rm -it -v /mnt/POOL/apps/thomascreek-hub/data:/data \
  -v /mnt/POOL/apps/thomascreek-hub/secrets/hub_master_key:/run/secrets/hub_master_key:ro thomascreek-hub:0.1.0'
hub secrets set home_assistant.token
hub secrets set plex.token
hub secrets set truenas.api_key
hub secrets set jellyseerr.api_key
hub secrets set mealie.token
hub secrets set homebox.password
hub secrets set show_player.app_token
hub secrets set frigate.password          # only if using Frigate's authenticated port
hub secrets set firewalla.msp_token       # only with Firewalla MSP
hub secrets set guest_wifi.password
hub secrets set apns.auth_key_p8 < AuthKey_XXXXXXXXXX.p8
hub secrets list
```

Recommended: give the hub its own **non-admin Home Assistant user** and token, a
**read-only TrueNAS API key** for a dedicated user, and a Plex token for a managed user.

## 4. Install as a custom app

Apps > Discover Apps > Custom App > Install via YAML, paste `compose.yaml` with the
two host paths filled in. Host networking is used so Bonjour works on home Wi-Fi
and the hub sees each phone's real address.

Then check every connection (read-only):

```sh
docker exec -it thomascreek-hub hub check
```

## 5. Make Dylan the admin

Open the app on home Wi-Fi; it enrolls itself. Then:

```sh
docker exec -it thomascreek-hub hub devices list
docker exec -it thomascreek-hub hub bootstrap-admin Dylan dev_xxxxxxxx
```

Every later phone shows up as a "New iPhone joined" notice on Dylan's phone.

## 6. Turn things on, one at a time

Add action names to `hub.live_actions` in `config.yaml` and restart the app, for
example `[light.set, scene.run]` first. `dry_run: false` makes everything live; leave
it `true` and use the list. Names: see docs/API.md, plus `lights.control`,
`media.approve`, `chores.done`, `chores.redeem`, `shopping.add`.

## Built-in VPN (optional)

1. In `config.yaml` set `vpn.provider: wg-cli`, `vpn.endpoint` (public name or IP and port), `allowed_ips`, `dns`, `home_ssids`.
2. In `compose.yaml` set `HUB_VPN_NAT: "1"` (keeps `cap_add: NET_ADMIN`).
3. On the Firewalla, forward UDP 51820 to the NAS.
4. Restart. The hub creates `wg0`, makes its server key in the vault, and re-adds every active phone.

## Kids' internet time (Firewalla)

Needs Firewalla MSP (the documented API). For each child, create one "block internet"
rule in the Firewalla app scoped to that child's devices, then list its rule id under
`firewalla.person_rules.<person id>`. The hub only pauses and resumes those rules; it
never creates or deletes firewall rules.

## House assets

Put `house-model-web.json` and the house photo under `data/assets/` (they stay out of the public repo).
