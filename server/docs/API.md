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
`inbox` (one inbox item), `snapshot` (re-read `/v1/home`). Keepalive comment every 20 s.

`POST /v1/actions {action, target, params}` -> `{ok, message, dry_run}`

`target` is the device id (an HA entity id) or, for `mode.run`, the mode id; for
`network.pause`/`network.grant` it is the child's person id. Supported:
`light.set {on,brightness,color}`, `fan.set {on,speed}`, `switch.set {on}`,
`climate.set {target_temp,hvac}` (children are clamped to the parent's range),
`lock.set {locked}`, `garage.set {open}`, `media.command {command}`, `media.volume {volume}`,
`speaker.group {members}`, `scene.run`, `mode.run`, `valve.run {minutes}`, `vacuum.start`,
`shower.preheat`, `announce {text, rooms}`, `network.pause {minutes?}`, `network.grant {minutes?}`.

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
Admin: `GET /v1/network/devices` (everything Firewalla sees, with `linked_to`),
`POST /v1/network/kids/{person_id}/devices {macs:[..]}` (admin tier; the hub creates one paused
"block internet" rule per device), `DELETE /v1/network/kids/{person_id}/devices/{mac}`.

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

## App updates

`GET /v1/app/update?build=16&version=2.0` (no sign-in needed; with a token the hub records the phone's build for the web page)

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
