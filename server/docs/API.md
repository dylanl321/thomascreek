# Thomas Creek Hub API (v1)

The contract between the iOS app and the hub. It follows the app thread's
requests in `app-contract-requests.md`; differences are called out under
**Notes for the app**. The machine-readable version is `server/openapi.json`
(regenerate with `python scripts/export_openapi.py`).

All JSON is snake_case. Times are ISO 8601 (UTC). The hub speaks HTTPS on
port 8443 with its own certificate (see TLS pinning below).

## Errors

Every error is JSON: `{"error": "<code>", "message": "<plain words>", ...}`.

| Status | `error` | Meaning |
|---|---|---|
| 401 | `unauthorized` | No token or it expired. Get a new one (challenge + token). |
| 401 | `revoked` / `unknown_device` | Device removed. Show "set up again at home". |
| 403 | `forbidden` | Not allowed. `ask_parent: true` means show "Ask a parent" (and offer `POST /v1/requests`). |
| 403 | `not_home` | Enrollment attempted away from home Wi-Fi. |
| 403 | `pending` | Device not yet assigned to a person. |
| 404 | `not_found` | |
| 409 | `already_decided`, `not_linked`, `not_set_up` | |
| 422 | `bad_request` | Validation; `fields` lists the bad fields. |
| 428 | `confirm_required` | Face ID needed. Body has `nonce`. See Secure actions. |
| 502/503 | `failed` / `unavailable` | A home system didn't answer. `message` is plain language. |

## Discovery and TLS pinning

Bonjour `_thomascreek._tcp`, TXT `api=v1`, `id=<hub id>`, `pin=<hex sha256 of the
hub certificate's SubjectPublicKeyInfo>`. Pin that value at enrollment (on home
Wi-Fi) and require it for every later connection, including through the tunnel
and `remote_url`. `GET /v1/ping` (no auth) returns `{ok, id, name, api, version}`.
`GET /v1/hello` (no auth) returns `{name, id, api, version, pin}`, the same pin as
the TXT record, for a phone that reached the hub by a typed address.

## Enrollment and sign-in

`POST /v1/enroll` (home Wi-Fi only)
```
{device_name, model?, platform?, app_version?, public_key, secure_public_key?, apns_token?, apns_env?,
 simulator?: bool, test_key?: "tctest_..."}
-> {device_id, status: "pending" | "active"}
```
- **Test simulators:** with `simulator: true` and a live `test_key` (made on `/admin`, Test simulators) the device is
  `active` at once for the key's person (Dylan), with no approval card, `kind: "test"` (shown as `test: true` in
  `/v1/people`) and no VPN client (`/v1/vpn/config` -> 403 `test_device`). A bad, expired or turned-off key, or a key
  without `simulator: true`, is `403 bad_test_key`. Still home Wi-Fi only. Turning a key off removes its devices.
  A pending device that enrolls again with a key is upgraded. Only the key's SHA-256 is stored.
- `public_key`: Secure Enclave P-256 key, base64 X9.63 uncompressed (`SecKeyCopyExternalRepresentation`) or DER SPKI.
- `secure_public_key`: a second SE key created with `.biometryCurrentSet`. Without it the device can never do secure-tier actions.
- Same key again returns the same `device_id` (idempotent). Dylan gets a push "New iPhone joined" with **Choose person** / **Remove**.

`POST /v1/auth/challenge {device_id}` -> `{nonce, expires_in: 120}`

`POST /v1/auth/token {device_id, nonce, signature}` -> `{access_token, token_type: "bearer", expires_at, person, role}`
- `signature`: ES256 over the UTF-8 bytes of `nonce`, base64, DER (what `SecKeyCreateSignature` gives) or raw r||s.
- Access tokens last 15 minutes. Nonces are single use; a failed attempt burns the nonce.
- Re-sign-in after a role change (the SSE stream sends `snapshot` with `reason: "role_changed"`).

`GET /v1/me`
```
{ person: {id, name, role, avatar?, ui_style: "standard"|"kid"|"little", access_until?, area?} | null,
  role: "admin"|"adult"|"child"|"guest"|"tablet"|"pending",
  capabilities: [..], device: {id, name, status}, at_home: bool, dry_run: bool,
  remote_url?, guest?: {wifi_ssid, wifi_password, guidance:[{icon,title,detail}], host_name},
  child?: {room_id, tv_device_id?, internet: {paused, until?, unknown?}} }
```
Capabilities are exactly the strings in the app contract. `tablet` is the shared wall-screen role.

## Secure actions (Face ID)

Secure tier: garage open, unlock, cameras when away, ovens, steam shower, a child's internet time.
Admin tier: people, device assignment, app restarts.

1. Send the request normally. The hub answers `428 {error: "confirm_required", nonce}`.
2. Sign `"<nonce>.<lowercase hex sha256 of the exact request body bytes>"` with the **biometric** key (this prompts Face ID).
3. Resend the identical body with header `X-TC-Confirm: <nonce>.<base64 signature>`.

An empty body hashes as sha256 of zero bytes. A wrong key, reused nonce or changed body gets a new 428.

## Home

`GET /v1/home` returns the snapshot shape from the contract, already filtered for the caller:
a child only gets their room, shared rooms and their TV; cameras, locks and garage
doors are absent (not greyed) without the capability; `systems` only for admin.
Extra keys: `status.since` (when offline), `systems.services` (each connector's health),
`devices[].actions` lists only actions this person may do on that device.

`GET /v1/events` (SSE). Each event is sent as an `event: <type>` line followed by one
`data: {"type": ..., "data": ...}` line; parsing only the `data:` lines is fine. Events: `hello`, `device` (full device object), `status`,
`inbox` (one inbox item), `snapshot` (re-read `/v1/home`), `tv` (what a house TV is playing; see Thomas Creek TV).
A TV subscription also receives `tv_command`. Keepalive comment every 20 s.

`POST /v1/actions {action, target, params}` -> `{ok, message, dry_run}`

`target` is the device id (an HA entity id) or, for `mode.run`, the mode id; for
`network.pause`/`network.grant` it is the child's person id. Supported:
`light.set {on,brightness,color}`, `fan.set {on,speed}`, `switch.set {on}`,
`climate.set {target_temp,hvac}` (children are clamped to the parent's range),
`lock.set {locked}`, `garage.set {open}`, `media.command {command}`, `media.volume {volume}`,
`speaker.group {members}`, `scene.run`, `mode.run`, `valve.run {minutes}`, `vacuum.start`,
`shower.preheat`, `announce {text, rooms}`, `network.pause {minutes?}`, `network.grant {minutes?}`.

`tv.play` and `tv.stop` tune the Thomas Creek TV app (see Thomas Creek TV). Power and volume stay on
`media.command` / `media.volume`.

`media.command` commands: `power play pause stop prev next vol_up vol_down mute`, `volume {volume:0-100}`,
`source {source}` (one of the TV's `sources`), and the remote keys `up down left right select back home`.
A media device's `state` lists only what that TV supports through Home Assistant in `commands`, plus
`sources`, `source`, `volume` (0-100) and `muted`. Remote keys go to the TV's own remote entity (Apple TV,
Android TV, Roku, Samsung, Sony) or to the LG/ADB key service, in that integration's key names.

While the hub is in practice mode every write returns `dry_run: true` and nothing changes.
Show this quietly (e.g. a "Practice mode" chip from `/v1/me.dry_run`).

## Push and inbox

`POST /v1/devices/push {apns_token, env, prefs?}` -> `{prefs, categories:[{id,title,detail,default}]}`
`POST /v1/devices/prefs {prefs}` -> same
`POST /v1/devices/activity {start_token?, activity_id?, token?}` (send `activity_id` with no `token` when an activity ends)

Categories: `doors, cameras, requests, media, chores, holiday, house, systems, devices`
(the last two only for admin). APNs custom keys: `cat`, `inbox_id`, `ref`. `aps.category`
is `NEW_DEVICE` or `REQUEST` for notices with buttons; `thread-id` is the category.

`GET /v1/inbox` -> `{items:[{id,t,level,cat,title,body,actions?,ref?}]}` sorted act_now, decision, maintenance, fyi.
`POST /v1/inbox/{id}/ack`
`POST /v1/inbox/{id}/act {action, params?}`: for a new-device notice, `assign` needs `params.person_id`.
Admin and device actions through the inbox need the same `X-TC-Confirm` (sign the act body).
Answering a decision clears it from every adult's inbox.

## Holiday lights

`GET|POST /v1/lights/{path}` forwards to the show player with the hub's app token
(`Authorization: Bearer <APP_TOKEN>`), keeping the method (commands are GET).
Allowed paths by role: admin everything; adults `status start stop next play queue unqueue look brightness schedule api/live api/notifications house static`;
children `status look play api/live house static`. Commands (anything but `status`, `api/*`, `house`, `wled`, `static`)
are writes: in practice mode they return `{ok: true, dry_run: true, message}`.
Web pages: open `/v1/lights/house?access_token=<token>` (or `/v1/lights/wled`); the hub sets an
httpOnly `tc_page` cookie with `Path=/` and redirects without the token. With that cookie the hub
also serves the absolute paths those pages load: `/layout.json`, `/frame`, `/house.obj`,
`/house.mtl`, `/js/*`, `/status`, `/api/live` and (admin only) `/wled/api/*`.
The cookie is accepted only by these page routes, never as a bearer for the rest of the API.

## Cameras

`POST /v1/cameras/{id}/pass` -> `{player_url, expires_at}` (5 minutes). `id` is the HA camera
object id (`camera.driveway` -> `driveway`), which must match the Frigate/go2rtc stream name.
Load `player_url` (hub-relative, needs no bearer) in a WKWebView. Away from home this is secure tier.
`GET /v1/cameras/{id}/snapshot.jpg`, `GET /v1/cameras/{id}/events` -> `{items:[{id,label,start,end,thumb_url,clip_url}]}`,
`GET /v1/cameras/events/{event_id}/thumb.jpg`, `.../clip.mp4`.

Frigate only (when Frigate is set up):
`GET /v1/cameras` -> `{cameras:[{id,name,width,height,online,room_id,snapshot_url,last_event:{label,start}|null}]}`
(width/height are Frigate's detect size; /v1/home `cameras` has the same items).
`GET /v1/cameras/events?limit=&before=&camera=&label=` and `GET /v1/cameras/{id}/events?limit=&before=` ->
`{items:[{id,camera,camera_name,label,sub_label,score,zones,start,end,thumb_url,snapshot_url,clip_url}], next_before}`.
`GET /v1/cameras/events/{id}` -> one item. `GET /v1/cameras/events/{id}/snapshot.jpg`.
SSE `camera_event` carries one item when Frigate records a new event.

## Media

`GET /v1/media/library?rated=1&kind=movie|show`, `GET /v1/media/continue`, `GET /v1/media/search?q=` -> `{items:[{id,title,year,kind,poster_url,rating,summary,...}], requestable?:[..]}`
(children only see ratings on the allowed list; `requestable` are Jellyseerr results with `id` like `movie:603`).
`POST /v1/media/play {item_id, target_device_id}` (not via `/v1/actions`).
`GET /v1/media/{id}/stream` -> `{hls_url, expires_at}`: hub-proxied HLS, no Plex token on the device.
`poster_url`, `art_url` and `image_url` values are hub paths; fetch them with the bearer header.

### Downloads for offline viewing
No approval step: anyone who may watch a title may download it (children: rated titles only).
- `GET /v1/media/{id}/episodes` -> `{items}`: a show's episodes (rating inherited from the show), to pick what to download.
- `GET /v1/media/{id}/download` -> `{item, options:[{quality:"original|high|small", label, detail, bytes?, instant}]}`. Films and episodes only. `original` is offered when an iPhone can decode the picture (H.264/HEVC); `high`/`small` are left out when the original is not clearly bigger, and `bytes` are realistic estimates; `instant` means it's already an MP4 and streams straight from Plex.
- `POST /v1/media/{id}/download {quality}` -> job `{id, item_id, quality, status:"preparing|ready|failed", progress 0-1, bytes?, error?, item, url?, expires_at?}`. Original MKV files are rewrapped to MP4 without re-encoding (sound an iPhone can't play, e.g. DTS, is converted to AAC; picture is never re-encoded); `high` (720p, 4 Mbps) and `small` (480p, 1.5 Mbps) are converted by Plex and stitched into one MP4 by ffmpeg. Asking again for the same title and quality returns the same job.
- `GET /v1/media/downloads/{job_id}` -> the job (only to the phone that started it). Each reply carries a fresh `url`.
- `GET /v1/media/file/{pass}` (no bearer; the pass lasts 24 h and is tied to the job and phone) -> `video/mp4`, Range supported for resumable background downloads.
- `POST /v1/media/{id}/progress {offset_s, duration_s?, watched?}` -> `{ok}`: where a downloaded title was stopped, passed on to Plex.
- Prepared files are kept in `<data>/downloads` for 24 hours, at most two prepare at once.

## Requests

`GET /v1/requests?mine=1` -> `{items:[{id,kind,title,poster_url,who,status,progress?,created_at}]}` (adults see all, children their own).
`POST /v1/requests {kind:"media"|"internet"|"reward"|"other", media_id?, text?, minutes?, reward_id?, title?, poster_url?}`.
Adults asking for media are auto-approved. Children's asks go to adults' inboxes.
`POST /v1/requests/{id}/approve|deny`. Approving internet time is secure tier.

## Family

Chores and rewards come from Family Command Center. A hub person is linked to a board person
with `PATCH /v1/people/{id} {family_board_id}` (admin; `GET /v1/family/board-people` lists the board's people).

`GET /v1/chores?person=&date=` ->
`{person:{id,name}, date, points, streak, goal, done, items:[{id,title,icon,word,done,points,time,waiting,locked,kind}], rewards:[{id,title,icon,cost,available?}]}`.
`waiting` means done and waiting for a parent's check; `locked` means another chore comes first.
`GET /v1/chores/all` (adults) -> `{boards:[{person:{id,name,role}, ...same...}]}`.
`POST /v1/chores/{id}/done {person?, date?}`, `POST /v1/rewards/{id}/redeem` (the board asks a parent to approve).
`GET /v1/calendar?days=7` -> `{events:[{id,title,start,end,who,all_day,location}]}`.
`GET /v1/meals/today`, `GET /v1/meals/week`, `GET /v1/meals/recipes/{slug}` ->
`{id,slug,title,image_url,total_time,prep_time,cook_time,servings,ingredients:[str],steps:[str]}`.
`GET /v1/shopping`, `POST /v1/shopping/{list}/items {text}`, `PATCH /v1/shopping/{list}/items/{id} {done}`.
`GET /v1/inventory/search?q=`.

## Trips (TREK)

`GET /v1/trips` (`trips.view`) ->
`{trips:[{id,title,description,start_date,end_date,archived,status,days_until,day_number,day_count,travellers?,plan_url?}], next, plan_url}`.
`status` is `now`, `upcoming`, `someday` (no dates) or `past`; the list is ordered now, soonest, someday, then past newest first.
`travellers:[{name,owner}]` is filled for trips that are not past. `plan_url` (TREK's own page) only for `trips.plan`.
The kitchen tablet gets only `next`. Children don't get trips in `trek.hidden_from_children`.
`GET /v1/trips/{id}` -> the summary plus `today` (ISO date while the trip is on),
`days:[{date,day_number,title,notes,places:[{name,address,lat,lng,time,end_time,duration_minutes,category,notes,transport_mode}],day_notes:[{text,time}],reservations:[{type,title,location,time,end_time,status,notes}]}]`,
`accommodations:[{name,address,lat,lng,start_date,end_date,check_in,check_out,notes}]`, `travellers`,
`unplanned_places` (ideas) and `unscheduled_reservations`. Children get no `notes` on reservations or stays.
`GET /v1/calendar` also lists each trip day as an all-day event (`id` `trek-<trip>-<date>`).

## Kids' internet (Firewalla)

`GET /v1/network/kids` (adults) -> `{kids:[{id,name,managed,paused,until,devices:[{mac,name,rule_id,paused}]}]}`.
Pause or grant with `POST /v1/actions {action:"network.pause"|"network.grant", target:<child person id>, params:{minutes?}}` (secure tier).
Admin: `GET /v1/network/devices` (every device on the Thomas Creek box, with `linked_to`),
`POST /v1/network/kids/{person_id}/devices {macs:[..]}` (admin tier; the hub creates one paused
"block internet" rule per device), `DELETE /v1/network/kids/{person_id}/devices/{mac}`.
Devices, rules and alarms (push alerts) come only from the box named by `firewalla.box`
(its gid or name, default "140Thomas", the Gold Pro at the house; with one box in the account that box is used).
If no box matches, Firewalla shows as down on Systems with the box names it found.

## People, devices, systems (admin unless noted)

`GET /v1/people` -> `{people:[{id,name,role,ui_style,areas,shared_areas,grants,denies,tv_device,access_until,devices:[..]}], unassigned:[..], areas:[..]}`
(non-admins get only themselves). `POST /v1/people`, `PATCH /v1/people/{id}` (anyone may change their own name and avatar).
`POST /v1/devices/{id}/assign {person_id}`, `POST /v1/devices/{id}/revoke` (also allowed on your own device, secure tier).
`GET /v1/activity?before=&person=&limit=&category=doors|lights|climate|media|network|people|cameras|chores|automation` ->
`{items:[{id,t,at,who,action,target,params,outcome,detail,source}], next_before}`. `id` is a string, `t` epoch seconds,
`at` ISO. `source` is `app`, `hub`, `home` (Home Assistant logbook: doors, locks, garage, presence) or `automation`.
`next_before` is an opaque string to pass back as `before`.
`outcome` is `ok`, `dry_run`, `denied` or `error`.
`GET /v1/systems` -> `{pools:[{name,healthy,status,size_tb,used_pct,scrub_pct}], apps:[{name,state,update}], apps_summary, alerts, services, hub}`.
`GET /v1/pipeline` -> `{requests:[{id,kind,title,year,poster_url,status,media_status,requested_by,requested_at}],
downloads:[{title,progress,eta_s,state,source,error?}], recent:[{kind,title,detail,at,source}],
upcoming:[{kind,title,detail,at,has_file,source}], failures:[{source,error}], services:[..]}`.
`POST /v1/push/test` sends a test notification to the calling device.

Hub web page (`https://<hub>:8443/admin`, home network only): status plus write-only editing of keys and
tokens. Sign-in goes through the app, admin tier (Face ID):
`POST /v1/admin/web-approve {code}` approves the 8-character code the page shows (dash optional);
`POST /v1/admin/web-link` -> `{url, expires_in}`: a one-time, one-minute path that opens the page signed in.

## VPN

`POST /v1/vpn/config {wg_public_key}` ->
`{interface:{addresses, dns}, peer:{public_key, endpoint, allowed_ips, persistent_keepalive}, home_ssids}`.
503 `vpn_unavailable` until Dylan turns the VPN on. Only for assigned (active) devices.
With the Firewalla pool (`vpn.provider: pool`) the hub hands out one of the client
configs made on the Firewalla, so the reply also carries `interface.private_key` (and
`peer.preshared_key` when the Firewalla set one): use those instead of the phone's own
key. `wg_public_key` may be left out then. `interface.mtu` may be present. 400
`vpn_unavailable` means every client is in use.

`GET /v1/vpn/pool` (admin) -> `{provider, clients:[{name, address, device_id, device_name}]}`.
`POST /v1/vpn/pool {config}` (admin tier, Face ID) adds a client .conf made on the
Firewalla, for the app's "Open in Thomas Creek" share target. Returns `{name, clients}`.
A client named for a person (`vpn.pool.lauren`) is kept for that person; numbered ones
go to anyone.
A phone connecting through the tunnel counts as "away" (so cameras still ask for Face ID).

### Private (PIA)

The app's second VPN button. The PIA login lives only in the vault: the pair typed on the admin page
(`vpn.pia.admin_username`, `vpn.pia.admin_password`) is used first, else `vpn.pia.username` /
`vpn.pia.password` copied from the 1Password item PIA. Phones never see it or the PIA token.

`GET /v1/vpn/exits` -> `{exits:[{id:"home", available}, {id:"private", available, provider:"pia", problem?}],
regions?:[{id, name, country, auto, probe:"ip:1337"}]}`. `regions` only when Private is available to this
person (`vpn.pia.roles`, default admin and adult). The app times a TCP connect to each `auto` region's
`probe` and asks for the fastest.

`POST /v1/vpn/config {exit:"private", wg_public_key, region?}` -> the same shape as above (no
`private_key`: the phone uses its own), plus `exit` and `region:{id, name, country}`. Everything is
routed through PIA (`allowed_ips` 0.0.0.0/0 and ::/0). The hub signs in to PIA (24-hour token), calls
`addKey` on a server in that region, and returns its settings. PIA forgets keys, so the app asks again on
every Connect. 403 `vpn_unavailable` for a role without Private, 503 until the login is in the vault,
502 `vpn_provider` when PIA refuses. `exit` defaults to `"home"`, so older app builds are unchanged.

## App updates

`GET /v1/app/update?build=16&version=2.0&channel=testflight` (no sign-in needed; with a token the hub records the phone's build for the web page)

`channel` is how the app was installed: `testflight`, `appstore`, `xcode`, `simulator` or `unknown`. When a signed-in
family phone on `testflight`/`appstore` reports a build newer than anything the hub knows, that build becomes the
latest (`source: "phone"`), so the other phones see the Update banner even without the App Store Connect key.
Test simulators, Simulator models and sandbox-push (Xcode) installs never count. Builds before 25 send no channel.

`POST /v1/app/release` `{"build": 26, "version": "2.0"}` with header `X-Test-Key: tctest_…` (an admin's test key):
the Mac's `ios/scripts/announce-build.sh` (run by `upload.sh`) calls this once TestFlight has finished processing
an upload, so phones see the banner before anyone installs it (`source: "upload"`). It only ever raises the latest build.

```json
{"latest_build": 17, "latest_version": "2.0", "released_at": "2026-11-12T16:00:00Z", "min_build": 15,
 "notes": null, "source": "testflight", "apple_id": "6700000001",
 "testflight_url": "itms-beta://beta.itunes.apple.com/v1/app/6700000001", "checked_at": 1794500000.0,
 "update_available": true, "update_required": false}
```

The hub finds the newest TestFlight build itself every 15 minutes when the vault holds an App Store Connect API key
(`appstore.auth_key_p8`, `appstore.key_id`, `appstore.issuer_id`; add them on the web page). Only builds that are
processed, unexpired and installable by internal testers count. On the web page Dylan can also type a latest build
and a required minimum (`min_build`, never above the latest). Below `min_build` the app blocks with "Update required";
below `latest_build` it shows an Update button that opens TestFlight. The event stream sends `app_update` when either
changes. Config: `app_update: {bundle_id, apple_id, min_build, check_minutes}`.

## Assets

`GET /v1/assets/house-model` (the `house-model-web.json` format) and `GET /v1/assets/header`,
both with `ETag` / `If-None-Match`. 404 until the files are placed on the NAS.

## Search

`GET /v1/search?q=&types=&limit=8` runs as the caller. Instant sources (rooms, lights and switches, scenes and
modes, doors and garage, climate, TVs and speakers, ovens, cameras, people, app screens) answer from memory; the
slower ones (Plex and Jellyseerr, YouTube, Mealie, Frigate events, family calendar, chores, TREK trips, alert
history, Homebox) run in parallel for about 1.2 s. Anything slower is listed in `pending` (types); ask again with
`types=<those>` and it answers from a 45 s cache.

```
{query, groups: [{type, title, total, items: [item]}], top: item|null,
 command: {title, subtitle, deep_link?, action}|null, pending: [type], failed: [source], ask: bool}
item = {id: "<type>:<id>", type, title, subtitle, score, source, ref, deep_link, verb, image_url, symbol,
        room_id?, kind?, media?: MediaItem, request?: {media_id, title}, chore?: {id, person, done},
        action: {action, target, params, tier, label, toggle?, on?, steps?}|null}
```

Types: room, light, scene, door, climate, player, appliance, camera, event, media, youtube, recipe, chore,
calendar, trip, person, inventory, alert, screen. Groups come best match first.

`action` is only present when `Policy.decide` allows it, and `POST /v1/actions` accepts it as-is (secure tier
asks for Face ID). `action: "batch"` (room "All off", the "kitchen off" command) is not a hub action: run each
of `steps` through `/v1/actions`. Commands understood: "<room> on|off", "turn on|off <room>", "preheat
[upper|lower] [to] 350". `ask` is true when the words read like a question (or nothing matched) and
`/v1/assistant` exists. Children get their own room, TV, rated films, own chores and trips; no cameras, doors,
ovens, network, alerts beyond house/media, recipes or inventory. Unnamed, unavailable-and-roomless, Plex-cast and
light-segment entities are left out, and several players with one name are folded into one row.

## Settings and favorites (per person)

Stored in `person_settings` (one row per person and key). See `hub/settings.py` for the keys and checks
and `ios/docs/hub-contract.md` for the request shapes.

| Route | Who | Does |
| --- | --- | --- |
| `GET /v1/me/settings` | anyone assigned | version, settings, favorites by kind |
| `PATCH /v1/me/settings` | anyone assigned | change keys; `null` resets |
| `POST /v1/me/settings/migrate` | anyone assigned | first sync from a device's old local choices |
| `PUT` / `DELETE /v1/me/favorites/{kind}/{id}` | anyone assigned | star / un-star, checked against the person's role |
| `POST /v1/me/favorites/{kind}/order` | anyone assigned | reorder |
| `/v1/people/{id}/settings`, `/v1/people/{id}/favorites/...` | adults for children, admin for anyone | the same, for someone else (logged) |
| `GET` / `POST /v1/me/view-as` | admin | who can be previewed; log start/stop |

Each change sends a `settings` event (`{version, device_id}`) to the person's own devices.

**View as.** `X-TC-View-As: <person_id>` from an admin's device makes the hub answer as that person
(`deps.caller`). Writes other than that person's settings and favorites are refused with `preview_only`,
so the preview can never change the house or anyone's devices. A non-admin sending the header gets 403.

## Thermostat history (`hub/climate.py`, `routes/climate.py`)

| Route | Who | Does |
| --- | --- | --- |
| `GET /v1/climate/{climate.entity}/history?range=6h\|24h\|3d\|7d` | anyone who sees the thermostat on Home (children: their rooms) | indoor temperature, set points and heating/cooling runs |

Reads Home Assistant's `/api/history/period/{start}?filter_entity_id=…&end_time=…&significant_changes_only=0`
for that one entity (cached 60 s per entity and range) and Open-Meteo's past hours at the house for the
outdoor line (cached 30 min). HA's recorder keeps 10 days by default, so 7 days is the longest range.
All times are Unix seconds. 503 `unavailable` when HA can't answer.
## House layout (admin, `home.layout`)

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | /v1/home/layout | | `{hidden, rooms, cleanup}`; cleanup = listed devices unavailable for 3+ days |
| POST | /v1/home/hidden | `{ids: [...], hidden: true}` | hide or show for everyone; `device:<id>` hides an appliance card |
| GET | /v1/rooms/{id}/layout | | name, symbol, hidden, ha_name, devices (hidden too), scenes |
| PUT | /v1/rooms/{id}/layout | `{name?, symbol?, hidden?, order?, add?, remove?}` | add/remove move devices into/out of the room |

`GET /v1/home` also gains `appliances` (one card per appliance) and `rooms[].scenes`. Stored in `hub_kv` key `home.layout`.

## Thomas Creek TV (v1)

One contract for the Android TV app and the iOS app. Paths stay under `/v1`. New fields can be added; clients ignore fields they don't know. A break would be a new path prefix, not a silent change to these shapes.

Errors are the usual `{"error", "message"}`. TV-specific codes: `bad_signature`, `clock` (TV clock more than five minutes off), `replay` (that sign-in timestamp was already used), `revoked`, `pending`, `unknown_tv`, `not_home`, `not_a_tv`, `bad_key`, `not_set_up`.

### Who is calling

| Caller | How they prove it | What they can do |
|---|---|---|
| TV, before it has a bearer | P-256 signature over the strings below. Home network only. | Enroll, pull config, mint a token |
| TV, after `POST /v1/tv/token` | Bearer, same audience as a phone. Re-checked on every call: revoked and key changes die immediately, not when the token expires. Home network only. | Its room's lights, scenes and climate; its own TV commands; guide filtered by profile; SSE |
| Phone | Existing `/v1/auth/token` bearer | Guide, library, `tv.play` / `tv.stop` through the same policy as other actions |

A TV token is built with `policy.Subject` and the same `TokenIssuer` as a phone (`hub.access_token_ttl_s`, 15 minutes). It has no biometric key. `decide` refuses secure and admin tiers, and also `lock.set`, `garage.set` and `camera.view`, even if a person's grants would allow them. Capability denies strip `doors`, `cameras` and every whole-house capability (`lights.all`, `climate.all`, `media.all`, …). The token keeps `lights.own_room`, `climate.own_room`, `media.own_tv` and `family.view`, and only inside the TV's room.

A kids' profile is always role `child`. `person_id` on the token can only narrow (a kids' TV cannot name an adult). Person grants are not copied; person denies are. An adult TV with no person is role `tablet` limited to its room, not an admin.

`GET /v1/home`, `POST /v1/actions` and `GET /v1/events` are the existing routes. A TV bearer is a normal caller with `subject.tv` set.

### Device key

`POST /v1/tv/enroll` `{install_id, name, model?, app_version?, public_key, code?}` → `{status, profile, mode}` plus `code` and `approve_url` while pending. `public_key` is the same P-256 encoding as a phone (`crypto.normalize_public_key`). A pending config body stays exactly `{"status":"pending"}`.

`POST /v1/tv/config` `{install_id, ts, signature}`. Signature is ES256 over the UTF-8 string `tvconfig:<install_id>:<ts>` (`crypto.verify_signature`). Approved body:

```json
{"status":"approved","version":3,"name":"Living room","profile":"adult","room":"living_room",
 "media_entity":null,"playlists":[{"type":"xtream","name":"Thomas Creek","server":"…","username":"…","password":"…"}],
 "epg_url":null,"lineup_version":12,"theme":null}
```

`profile: "kids"` also includes `kids_categories`. Pending is exactly `{"status":"pending"}`. `version` counts hub-side edits (profile, room, kids' categories, lineup). The `ETag` is a quoted sha256 of the JSON, so a vault password change still misses the cache when `version` did not move. Send `If-None-Match` to get `304` with an empty body and the same `ETag`. `Cache-Control: private, no-cache`.

`POST /v1/tv/token` `{install_id, ts, signature, person_id?}`. Signature over `tvtoken:<install_id>:<ts>` or, when narrowing, `tvtoken:<install_id>:<ts>:<person_id>`. A timestamp cannot be reused.

```json
{"access_token":"…","token_type":"bearer","expires_at":"…","expires_in":900,
 "role":"tablet","profile":"adult","room":"living_room","person":null,
 "capabilities":["climate.own_room","family.view","lights.own_room","media.own_tv"],
 "tv":{"id":"…","name":"Living room"}}
```

### Lineup and library

Channel keys never contain a URL or a password. Xtream puts the password in the path, so v1 does not hash URLs.

| Key | When |
|---|---|
| `iptv-org:CNN.us` | iptv-org id, preferred when the row has one |
| `xc:thomas-creek:44102` | Xtream `stream_id`. `thomas-creek` is a household label, not the username |
| `tvg:thomas-creek:cnn.us` | M3U `tvg-id` |
| `vod:thomas-creek:99` | movie |
| `series:thomas-creek:12` | series |
| `ep:thomas-creek:555` | episode (`id` from `get_series_info`, not the episode number) |

`GET /v1/tv/lineup` (bearer, `media.all` or `media.own_tv`). ETag / 304 like config.

```json
{"version":1710000000,"channels":[
  {"key":"xc:thomas-creek:44102","name":"CNN","number":202,"group":"News","logo":"https://img.example/cnn.png","kids":false,"catchup_days":2}
]}
```

A child, a guest and a kids' TV only receive `kids: true`. Hidden channels are omitted. Logos with a username, password or secret query are dropped. The list is the provider's live streams (cached) with the admin overlay from `PUT /v1/tvapp/lineup` (`systems`) winning on name, number, group, logo, kids and hidden. An empty cache falls back to that overlay alone.

The provider snapshot is `POST /v1/tvapp/catalog/refresh` (`systems`). Reads refresh it when the list is older than six hours or the guide older than three. `409 not_set_up` when `iptv.xtream_*` is missing and there is no cache.

`GET /v1/tv/guide?start=<unix>&end=<unix>&channel=<key>`

```json
{"from":"…","to":"…","channels":[
  {"key":"xc:thomas-creek:10","name":"CNN","number":20,"group":"News","logo":"https://img.example/cnn.png",
   "kids":false,"catchup_days":2,
   "now":{"start":"…","end":"…","title":"Newsroom","subtitle":null,"desc":"…","icon":"https://img.example/news.jpg","catchup":true},
   "next":{"start":"…","end":"…","title":"Tonight","subtitle":null,"desc":null,"icon":null,"catchup":false},
   "programmes":[]}
]}
```

Default window is from one hour ago to six hours ahead, at most 48 hours. `catchup` is true when the channel has archive days and the programme has already started inside that window (start-over of what's on now counts).

`GET /v1/tv/series?q=&category=&limit=` → `{categories, series:[{id,name,cover,year,genre,rating,category,kids}]}`

`GET /v1/tv/series/series:thomas-creek:12` → the same plus `plot`, `backdrop`, `seasons:[{number, episodes:[{id,season,number,title,plot,still,duration_s}]}]`.

`GET /v1/tv/movies` and `GET /v1/tv/movies/vod:thomas-creek:99` are the movie equivalents (`plot`, `backdrop`, `duration_s` on the one movie).

`GET /v1/tv/library/search?q=` (at least two letters) → `{q, channels, programmes, series, movies}`. Programmes are only those in the cached guide (about seven days back and 36 hours ahead), each with `channel_key` and `channel_name`.

Kids' categories (`PUT /v1/tvapp/kids-categories`) are the allow-list. A kids' caller sees a channel, series or movie only when its category name matches that list, or a lineup row is marked `kids`. An empty list means a kids' caller sees nothing.

### TVs a phone can target

`GET /v1/tv/players`

```json
{"tvs":[
  {"id":"<install_id>","name":"Living room","room":"living_room","room_name":"Living Room",
   "profile":"adult","media_entity":null,"online":true,
   "playing":{"channel_key":"xc:thomas-creek:10","title":"Newsroom","name":"CNN","number":20},
   "last_command":{"id":"tvc_…","ok":false,"error":"no signal"}}
]}
```

`online` means a heartbeat, token or enroll in the last two minutes. The list is the TVs `tv.stop` would allow: an adult sees every approved TV; a child sees a TV in their own room or their own `tv_device`, not a shared room.

### Playing

`POST /v1/actions` with `tv.play` or `tv.stop`. `target` is the install id or the linked `media_entity`. Power and volume stay on `media.command` and `media.volume`.

```json
{"action":"tv.play","target":"<install_id>","params":{"channel_key":"xc:thomas-creek:10","title":"Newsroom"}}
{"action":"tv.play","target":"<install_id>","params":{"kind":"catchup","channel_key":"xc:thomas-creek:10","start":1710000000,"resume":30}}
{"action":"tv.play","target":"<install_id>","params":{"movie_id":"vod:thomas-creek:99","resume":600}}
{"action":"tv.play","target":"<install_id>","params":{"episode_id":"ep:thomas-creek:555","series_id":"series:thomas-creek:12","resume":15}}
{"action":"tv.stop","target":"<install_id>"}
```

`kind` may be omitted: an episode id means `episode`, a movie id means `movie`, a channel plus `start` means `catchup`, a channel alone means `live`. `start` is unix seconds or an ISO time, and must fall inside the channel's catch-up days and not in the future. `resume` is seconds from 0 to 24 hours. A child, or any caller aiming at a kids' TV, can only play a kids' channel, movie or episode.

The response is `{ok, message, dry_run, command_id?}`. In practice mode nothing is delivered and `command_id` is absent. Add `tv.play` and `tv.stop` to `live_actions` (or turn practice mode off) to deliver them.

The TV receives the same object as SSE `tv_command` on `GET /v1/events` (hello includes `kind: "tv"` and `tv: <install_id>`), and can long-poll `GET /v1/tv/commands?timeout=20` (max 25, `0` returns immediately). Commands wait until `POST /v1/tv/commands/ack` `{ids}` or two minutes. A phone calling `/commands` gets `403 not_a_tv`. The command has no stream URL; the TV already has the provider login from config and looks the id up itself.

```json
{"id":"tvc_…","install_id":"…","action":"play","kind":"episode","episode_id":"ep:thomas-creek:555",
 "series_id":"series:thomas-creek:12","name":"Cartoon Show","title":"Pilot","resume":15,"at":"…"}
```

The TV reports the outcome on its next `POST /v1/tv/state` (bearer):

```json
{"playing":{"channel_key":"xc:thomas-creek:10","title":"Newsroom","name":"CNN","number":20},
 "foreground":true,
 "health":{"app_version":"1.4.1","profile":"adult","epg_age_s":120,"last_error":null},
 "command_id":"tvc_…","command_ok":false,"command_error":"no signal"}
```

`playing` may be null. `channel_key`, when set, must be one of the key forms above. URLs in `last_error` are replaced with `[url]`. Phones receive SSE `tv`:

```json
{"type":"tv","data":{"id":"…","name":"Living room","room":"living_room","profile":"adult",
 "media_entity":null,"foreground":true,"playing":{"title":"Newsroom","name":"CNN","channel_key":"xc:thomas-creek:10","number":null},
 "at":"…","command":{"id":"tvc_…","ok":false,"error":"no signal"}}}
```

`command` is present only on the heartbeat that carried the result. A child sees a `tv` event only for their own room or their own TV. A TV subscription does not receive other TVs' `tv` events, `camera_event`, or camera snapshots. It does receive doorbell and act-now house, kitchen, weather and door notices as `inbox` (display only: no `actions`, no image). An adult TV also receives `decision` / `requests`. Doorbell text is `cat: "doorbell"`.

### Admin

Existing `/v1/tvapp` routes, plus `PATCH /v1/tvapp/tvs/{id}` `{room?, media_entity?, status?, profile?, name?}` and `PUT /v1/tvapp/lineup` `{channels:[…]}`. `room` is an area id such as `living_room`. `media_entity` is `media_player.…`. Forgetting a TV (`DELETE`) is how a box gets a new key; revoke rejects the bearer on the next request.

A programme that can be started over also has `catchup_until` (ISO), the end of the archive window.

## Setting up a TV

The person turns on Developer options and network debugging once. Wireless debugging on Android 11 and Google TV also shows a pairing code and a port.

`GET /v1/tvapp/discover` (`systems`) → `{devices:[{ip, name, connect_port?, pairing_port?}], hint?}`. This is `adb mdns services`. An empty list means type the address.

`POST /v1/tvapp/setup` `{ip?, name?, room?, profile?, pairing_port?, pairing_code?, home?, mode?}`. `mode` is `adb` (the default) or `non-adb`. Debugging is optional. Safe to run again: a box that already has this build is not reinstalled. `home: true` runs `cmd package set-home-activity` and leaves the stock launcher enabled. Fire TV is installed but not made Home, and `warning` says so. `needs_allow` means the TV is showing the debugging prompt. If the overlay grant or the home-screen change does not stick, setup still finishes and `steps[].note` is the on-TV instruction (`Settings → Apps`).

`mode: "non-adb"` does not need an address. The status includes `install: {code, path, url}`. `url` is what Downloader or a browser on the home network opens. The phone shows that URL as a QR code. The link lasts two hours.

`GET /v1/tv/apk/{code}` (home network, no bearer) returns the signed APK for that link. `GET /v1/tvapp/setup/code/{code}` is the status for that link.

`POST /v1/tv/enroll` may include `code` (the install link). A valid code approves the TV immediately, copies the room and profile from the link, and records `mode: "non-adb"`. Without a code, a new TV stays pending and the response adds `code` and `approve_url` (`thomascreek://tv/{code}`). The TV shows that code. `POST /v1/tvapp/approve` `{code, room?, profile?}` (`systems`) approves it. Config is still the signed `POST /v1/tv/config`.

`GET /v1/tv/update` (TV bearer, home network) is the update manifest: `version_code`, `version_name`, `sha256`, `signer_sha256`, `apk_path` (`/v1/tv/apk`), `package`, `notes`. `GET /v1/tv/apk` with the same bearer returns the file. The app checks both hashes, then installs with `PackageInstaller` (`REQUEST_INSTALL_PACKAGES`). The person confirms on the TV.

Every status and every TV record has `mode`: `adb` or `non-adb`. The phone shows which one.

`GET /v1/tvapp/setup/{ip}` is the same status, for the phone to show progress. `you_do` is the one line for the person at the TV. `steps` is `done`, `current`, `todo`, `failed` or `skipped`.

`POST /v1/tvapp/setup/{ip}/rollback-home` puts the previous launcher back. The app also reports `health.home_crashes`; two or more rolls the home screen back by itself. Boxes set up here only take automatic updates between 1am and 6am, and never while the app is on screen. After a reboot the hub reconnects; if the TV asks for Allow again, the status goes back to `needs_allow`.

The first time the app opens it enrolls on its own. A box the hub added is approved immediately, with the room from setup.

## Plex row for the TV

`GET /v1/tv/plex` with a TV or phone bearer that can watch media.

```json
{"continue":[{
  "id":"9","title":"Bluey: Keepy Uppy","year":2018,"kind":"episode","rating":"TV-Y",
  "poster_url":"/v1/media/art?path=…","summary":"…","show":"Bluey","season":1,"episode":2,
  "deep_link":"plex://preplay?metadataKey=/library/metadata/9&server=MACHINE","package":"com.plexapp.android"
}],"on_deck":[],"recent":[]}
```

`continue` is in progress, `on_deck` is the next episode not started, `recent` is newly added. Artwork is a hub path fetched with the bearer. `deep_link` opens the Plex app at that item. A kids' TV only gets allowed ratings. There is no Plex token in the payload.

## TV contract

This is the wire format the TV app tests against. Names here are the ones the hub sends. A pending enroll is `code` and `approve_url`, and `approve_url` is exactly `thomascreek://tv/{code}`. The update manifest uses `signer_sha256` and `apk_path`. A Plex row uses `poster_url` and `deep_link`. Clients ignore fields they don't know.

`POST /v1/tv/enroll` `{install_id, name, model?, app_version?, public_key, code?}`. While the TV is waiting:

```json
{"status":"pending","profile":"adult","mode":"non-adb","code":"K7NP3Q","approve_url":"thomascreek://tv/K7NP3Q"}
```

`code` and `approve_url` are present on every pending response. The TV shows `code` and a QR of `approve_url`. The phone opens that URL. Approved, including when `code` was a valid install link:

```json
{"status":"approved","profile":"kids","mode":"adb"}
```

`POST /v1/tv/config` `{install_id, ts, signature}` over `tvconfig:<install_id>:<ts>`. Pending is exactly `{"status":"pending"}`. Approved:

```json
{"status":"approved","version":3,"name":"Living room","profile":"adult","room":"living_room","media_entity":null,
 "playlists":[{"type":"xtream","name":"Thomas Creek","server":"http://iptv.example:8080","username":"household","password":"s3cret"}],
 "epg_url":null,"lineup_version":12,"theme":null}
```

A kids profile also has `"kids_categories":["Kids"]`. `304` has an empty body when `If-None-Match` matches the `ETag`.

`POST /v1/tv/token` `{install_id, ts, signature, person_id?}` over `tvtoken:<install_id>:<ts>` or `tvtoken:<install_id>:<ts>:<person_id>`:

```json
{"access_token":"eyJ…","token_type":"bearer","expires_at":"2026-11-13T21:30:00+00:00","expires_in":900,
 "role":"tablet","profile":"adult","room":"living_room","person":null,
 "capabilities":["climate.own_room","family.view","lights.own_room","media.own_tv"],
 "tv":{"id":"tv_office_0123456789","name":"Living room"}}
```

`POST /v1/tv/state` with the bearer. Response is `{"ok":true}`.

```json
{"playing":{"channel_key":"xc:thomas-creek:10","title":"Newsroom","name":"CNN","number":20},
 "foreground":true,"health":{"app_version":"1.4.1","profile":"adult","epg_age_s":120,"last_error":null,"home_crashes":0},
 "command_id":"tvc_ab12","command_ok":true,"command_error":null}
```

`GET /v1/tv/lineup`

```json
{"version":1710000000,"channels":[
  {"key":"xc:thomas-creek:44102","name":"CNN","number":202,"group":"News","logo":"https://img.example/cnn.png","kids":false,"catchup_days":2}
]}
```

`GET /v1/tv/guide?start=<unix>&end=<unix>&channel=<key>`

```json
{"from":"2026-11-13T19:00:00+00:00","to":"2026-11-14T02:00:00+00:00","channels":[
  {"key":"xc:thomas-creek:10","name":"CNN","number":20,"group":"News","logo":"https://img.example/cnn.png",
   "kids":false,"catchup_days":2,
   "now":{"start":"2026-11-13T20:00:00+00:00","end":"2026-11-13T21:00:00+00:00","title":"Newsroom","subtitle":null,"desc":"The hour.","icon":null,"catchup":true,"catchup_until":"2026-11-15T20:00:00+00:00"},
   "next":{"start":"2026-11-13T21:00:00+00:00","end":"2026-11-13T22:00:00+00:00","title":"Tonight","subtitle":null,"desc":null,"icon":null,"catchup":false,"catchup_until":null},
   "programmes":[]}
]}
```

`GET /v1/tv/series?q=&category=&limit=`

```json
{"categories":["Kids"],"series":[
  {"id":"series:thomas-creek:12","name":"Bluey","cover":"https://img.example/bluey.jpg","year":2018,"genre":"Kids","rating":"TV-Y","category":"Kids","kids":true}
]}
```

`GET /v1/tv/series/series:thomas-creek:12` is that object plus `plot`, `backdrop`, and `seasons`.

```json
{"id":"series:thomas-creek:12","name":"Bluey","cover":"https://img.example/bluey.jpg","year":2018,"genre":"Kids","rating":"TV-Y","category":"Kids","kids":true,
 "plot":"A puppy.","backdrop":null,"seasons":[
  {"number":1,"episodes":[{"id":"ep:thomas-creek:555","season":1,"number":1,"title":"Keepy Uppy","plot":null,"still":null,"duration_s":420}]}
]}
```

`GET /v1/tv/movies` and `GET /v1/tv/movies/vod:thomas-creek:99`:

```json
{"categories":["Movies"],"movies":[
  {"id":"vod:thomas-creek:99","name":"The Matrix","cover":null,"year":1999,"genre":"Action","rating":"R","category":"Movies","kids":false}
]}
```

```json
{"id":"vod:thomas-creek:99","name":"The Matrix","cover":null,"year":1999,"genre":"Action","rating":"R","category":"Movies","kids":false,"plot":"A hacker.","backdrop":null,"duration_s":8160}
```

`GET /v1/tv/library/search?q=news`

```json
{"q":"news","channels":[{"key":"xc:thomas-creek:10","name":"CNN","number":20,"group":"News","logo":null,"kids":false}],
 "programmes":[{"start":"2026-11-13T20:00:00+00:00","end":"2026-11-13T21:00:00+00:00","title":"Newsroom","subtitle":null,"desc":null,"icon":null,"catchup":true,"catchup_until":"2026-11-15T20:00:00+00:00","channel_key":"xc:thomas-creek:10","channel_name":"CNN"}],
 "series":[],"movies":[]}
```

`GET /v1/tv/players`

```json
{"tvs":[
  {"id":"tv_office_0123456789","name":"Living room","room":"living_room","room_name":"Living Room",
   "profile":"adult","media_entity":null,"online":true,
   "playing":{"channel_key":"xc:thomas-creek:10","title":"Newsroom","name":"CNN","number":20},
   "last_command":{"id":"tvc_ab12","ok":true,"error":null}}
]}
```

`POST /v1/actions` request, then the response when the action is live:

```json
{"action":"tv.play","target":"tv_office_0123456789","params":{"kind":"live","channel_key":"xc:thomas-creek:10","title":"Newsroom","resume":0}}
```

```json
{"ok":true,"message":"Told Living room to play Newsroom","dry_run":false,"command_id":"tvc_ab12"}
```

`tv.stop` sends `"action":"tv.stop"` and `"params":{}`. `action` inside the delivered command is `play` or `stop`, not `tv.play`.

`GET /v1/tv/commands?timeout=20` and `POST /v1/tv/commands/ack` `{"ids":["tvc_ab12"]}`:

```json
{"commands":[
  {"id":"tvc_ab12","install_id":"tv_office_0123456789","action":"play","at":"2026-11-13T21:00:00+00:00",
   "kind":"live","channel_key":"xc:thomas-creek:10","name":"CNN","number":20,"title":"Newsroom","resume":0}
]}
```

```json
{"acked":1}
```

`GET /v1/events` is `text/event-stream`. Each event is `event: <type>` and one `data:` line, which is the whole object below. Hello, then a command, a light, an inbox notice, and a snapshot:

```json
{"type":"hello","data":{"role":"tablet","kind":"tv","tv":"tv_office_0123456789"}}
```

```json
{"type":"tv_command","data":{"id":"tvc_ab12","install_id":"tv_office_0123456789","action":"play","at":"2026-11-13T21:00:00+00:00","kind":"catchup","channel_key":"xc:thomas-creek:10","name":"CNN","title":"Newsroom","start":"2026-11-13T20:00:00+00:00","resume":30}}
```

```json
{"type":"tv_command","data":{"id":"tvc_ab13","install_id":"tv_office_0123456789","action":"play","at":"2026-11-13T21:01:00+00:00","kind":"movie","movie_id":"vod:thomas-creek:99","name":"The Matrix","title":"The Matrix","resume":600}}
```

```json
{"type":"tv_command","data":{"id":"tvc_ab14","install_id":"tv_office_0123456789","action":"play","at":"2026-11-13T21:02:00+00:00","kind":"episode","episode_id":"ep:thomas-creek:555","series_id":"series:thomas-creek:12","name":"Bluey","title":"Keepy Uppy","resume":15}}
```

```json
{"type":"tv_command","data":{"id":"tvc_ab15","install_id":"tv_office_0123456789","action":"stop","at":"2026-11-13T21:03:00+00:00"}}
```

```json
{"type":"device","data":{"id":"light.living_room","kind":"light","name":"Living room","room_id":"living_room","state":{"on":true},"updated_at":"2026-11-13T21:00:00+00:00","available":true,"actions":["light.set"]}}
```

```json
{"type":"inbox","data":{"level":"act_now","cat":"doorbell","title":"Someone is at the door","body":"Front door","ref":null}}
```

```json
{"type":"snapshot","data":{}}
```

Phones receive now-playing as `tv`, not `tv_command`:

```json
{"type":"tv","data":{"id":"tv_office_0123456789","name":"Living room","room":"living_room","profile":"adult","media_entity":null,"foreground":true,"playing":{"title":"Newsroom","name":"CNN","channel_key":"xc:thomas-creek:10","number":20},"at":"2026-11-13T21:00:00+00:00","command":{"id":"tvc_ab12","ok":true,"error":null}}}
```

`GET /v1/home` with a TV bearer is the existing home snapshot, limited to that TV's room. Lights and scenes in the room are included. Locks, the garage and cameras are not offered to a TV.

`GET /v1/tv/apps` with a TV bearer. `allowed` is always present. A kids profile, and any child or guest using the TV, gets the kid apps from the household catalog. An adult profile gets every catalog app that has an Android package, including those kid apps.

```json
{"allowed":["com.google.android.youtube.tvkids","com.netflix.ninja","com.plexapp.android","com.disney.disneyplus","com.pbs.kids.pbskidsvideo"]}
```

```json
{"allowed":["com.google.android.youtube.tv","com.google.android.youtube.tvkids","com.netflix.ninja","com.plexapp.android","com.disney.disneyplus","com.pbs.kids.pbskidsvideo","com.amazon.amazonvideo.livingroom","com.wbd.stream","com.hulu.livingroomplus","com.spotify.tv.android","com.apple.atve.androidtv.appletv"]}
```

`GET /v1/tv/update` and `GET /v1/tv/apk` use the TV bearer. The file download is the bytes of the APK. The manifest:

```json
{"version_code":602,"version_name":"1.4.1-tc.2","sha256":"abc","signer_sha256":"deadbeef",
 "apk_path":"/v1/tv/apk","package":"me.dlewis.thomascreek.tv","notes":null,
 "install":"Download the APK with this TV's bearer, check sha256 and signer_sha256, then hand the file to PackageInstaller. The person confirms on the TV."}
```

`GET /v1/tv/plex`

```json
{"continue":[{
  "id":"9","title":"Bluey: Keepy Uppy","year":2018,"kind":"episode","rating":"TV-Y",
  "poster_url":"/v1/media/art?path=%2Flibrary%2Fbluey","summary":"A game.","show":"Bluey","season":1,"episode":2,
  "deep_link":"plex://preplay?metadataKey=/library/metadata/9&server=MACHINE","package":"com.plexapp.android"
}],"on_deck":[],"recent":[]}
```

## Universal title search

`GET /v1/titles/search?q=` (at least two letters). One card per title across Plex, the IPTV lineup and guide, and TMDB (Jellyseerr, which is what asks Sonarr and Radarr). The same title is one card: matched on TMDB id, then IMDb, then TVDB, then the cleaned name and year.

```json
{"query":"matrix","titles":[{
  "id":"tmdb:movie:603","title":"The Matrix","year":1999,"kind":"movie",
  "poster_url":"https://image.tmdb.org/t/p/w342/m.jpg","summary":"…","rating":"R",
  "ids":{"tmdb":"603","imdb":"tt0133093","tvdb":null},
  "offers":[
    {"source":"plex","label":"In Plex (4K)","plex_id":"555","play_on_phone":true,"play_on_tv":true},
    {"source":"iptv","label":"IPTV VOD","params":{"movie_id":"vod:thomas-creek:100","title":"The Matrix"},"play_on_tv":true},
    {"source":"guide","label":"Airs Tue 8pm on ESPN","params":{"channel_key":"xc:thomas-creek:7","title":"The Matrix"},"play_on_tv":true},
    {"source":"guide","label":"Catch-up until Fri","params":{"kind":"catchup","channel_key":"xc:thomas-creek:7","start":"…","title":"The Matrix"},"play_on_tv":true}
  ],
  "request":{"media_id":"movie:603","status":"available","can_request":false,"ask_parent":false,"request_id":null}
}]}
```

`kind` is `movie`, `show` or `channel`. `request.status` is `waiting` (a parent has not answered), `requested`, `downloading`, `available`, or null. `play_on_phone` is only on a phone's card, and only when Plex has the file (`plex_id` is `POST /v1/media/play`'s `item_id`, and the download and stream routes). `params` is a `tv.play` body. An IPTV series offer has `series_id` (`series:thomas-creek:…`) and no play params until an episode is chosen.

A child only sees ratings on the household list (`child_policy.ratings`) and kids' IPTV. A TMDB title with no US rating is left out. A TV bearer can search; it cannot request.

`POST /v1/titles/request {media_id:"movie:603"|"tv:1399"}`. An adult's ask goes to Jellyseerr (and from there to Radarr or Sonarr), the same pipeline as `POST /v1/media/requests`. A child's ask is a `requests` row with `status: "waiting"` and an inbox item for parents; approving it is the existing `POST /v1/requests/{id}/approve`, which is what submits it to Jellyseerr. `503 unavailable` when Jellyseerr is not configured. `503 dry_run` while `media.approve` is not a live action.
