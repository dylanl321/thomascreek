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
hub certificate's SubjectPublicKeyInfo>`, `pair=/v1/tv/pair`. Pin that value at enrollment (on home
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

`POST /v1/devices/push {apns_token, env}` -> `{prefs, categories:[{id,title,detail,group,default,locked}]}` (a `prefs` sent here is ignored)
`POST /v1/devices/prefs {prefs}` -> same; changes the caller's own categories
`POST /v1/devices/activity {start_token?, activity_id?, token?}` (send `activity_id` with no `token` when an activity ends)

Categories belong to the person, not the device, so they are the same on every phone and iPad. Each has an
audience `group`: `household` (doors, cameras, house, weather, kitchen, kids, holiday), `personal` (media,
requests, printer, chores) or `technical` (systems, devices, updates, feedback). Defaults come from the role:
technical ones are on for admins only; kids get no doors, cameras, house or kitchen notices. A person can turn
their own categories on or off, except that only an admin can turn a technical one on for someone who isn't an
admin (People on the hub web page, which also sets anyone's categories). `GET /v1/alerts` returns them as
`notify` (and `device_prefs` for older apps); `PUT /v1/alerts {notify:{cat:bool}}` changes them.

Sorting: notices whose words come from elsewhere (`house` from Home Assistant automations, or `auto`) are
sorted into a category before they go out (`hub/notify_sort.py`): an admin's fix for that kind of notice, then
the AI's cached answer, then the AI (the brain's cheap model, at most 6 s), then keyword rules. Urgent notices
(`act_now`, time-sensitive) never wait for the AI: rules decide and the AI learns that kind in the background.
Every other category is fixed and goes out at once. The AI only picks the category; recipients are still the
sender's list narrowed by each person's categories, so it can remove people but never add them. The People page
lists recent sorting with a per-kind fix. APNs custom keys: `cat`, `inbox_id`, `ref`. `aps.category`
is `NEW_DEVICE` or `REQUEST` for notices with buttons. `thread-id` matches `apns-collapse-id`
(the alert's group, so a repeat replaces the one on screen and related alerts sit together);
an alert with no group uses the category as `thread-id` and does not collapse.

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

## House brain (admin, `hub/brain`, `routes/brain.py`)

Background jobs find things in plain Python (states, presence, the activity log) and the cheap model
(`brain.cheap_model`, Claude Haiku 5.5) only words and ranks them; it hands over to `brain.smart_model`
(Sonnet 5.5) when it answers `unsure`. Jobs: `plain_cards`, `left_on`, `health` (maintenance inbox),
`briefing`, `catch_up`. Cards arrive in `GET /v1/smart` with `rule: brain_<job>` and an `ai: {run_id, job}`
field; `POST /v1/smart/dismiss` on them records `used` / `not_now` in the run log. Nothing acts by itself
(`brain.auto_actions` is empty). No spending cap unless `brain.monthly_cap_usd` is set.

| Method | Path | |
|---|---|---|
| GET | `/v1/brain/summary` | `{status: on\|paused\|off\|needs_key\|capped, month, spend_usd, assistant_usd, total_usd, cap_usd\|null, runs, cards, used, not_now, ignored, resolved, dismiss_rate, actions, undo_rate, models, jobs: [{id, title, about, enabled, autonomy, muted, runs, cost_usd, cards, used, last_run}]}` |
| GET | `/v1/brain/runs?job=&limit=&before=` | `{items: [{id, ts, job, job_title, trigger, model, tier, tokens_in, tokens_out, tokens_cached, cost_usd, latency_ms, action, outcome, feedback, headline, input_summary}], before}` |
| GET | `/v1/brain/runs/{id}` | one run with `decision` (what it saw, the model's words, fallback reason, per-call costs) |
| POST | `/v1/brain` | `{paused}` pause or resume every job |
| POST | `/v1/brain/jobs/{id}` | `{enabled?, autonomy?: observe\|suggest}` |
| POST | `/v1/brain/jobs/{id}/run` | run a job once now |

Ask Thomas Creek's turns are logged in the same table (job `assistant`) with their cost.

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

Every approved config includes `kids_categories` and `people` (`[{"id","name","role"}]`: first names of admins, adults and children, no guests or tablets), so a TV can keep a kids profile per child. Pending is exactly `{"status":"pending"}`. `version` counts hub-side edits (profile, room, kids' categories, lineup). The `ETag` is a quoted sha256 of the JSON, so a vault password change still misses the cache when `version` did not move. Send `If-None-Match` to get `304` with an empty body and the same `ETag`. `Cache-Control: private, no-cache`.

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

Plex in the TV library (`hub/tvplex.py`, same callers as the lineup):
- `GET /v1/tv/plex/library`: `{"version","movies":[…],"shows":[…]}`. Each row has `id`, `kind` (`movie`/`series`), `title`, `year`, `tmdb`, `imdb`, `tvdb`, `rating`, `summary`, `resolution` (`2160p`/`1080p`/`720p`/`sd`, films), `duration_s`, `offset_s`, `watched`, `added_at`, `library`, `poster_url` and `backdrop_url` (hub art, `/v1/art/…/vod:plex:<id>` or `series:plex:<id>`). A child gets only titles rated in `child_policy.ratings`. It's cached for 15 minutes.
- `GET /v1/tv/plex/{id}/episodes`: `{"episodes":[{"id","season","episode","title","duration_s","offset_s"}]}`.
- `POST /v1/tv/plex/{id}/play` (a film or an episode): `{"url":"/v1/tv/plex/file/<pass>","offset_s","resolution","container"}`. The pass is good for 8 hours and carries no Plex token.
- `GET /v1/tv/plex/file/<pass>`: the original file (direct play), with `Range` passed through.
- `POST /v1/tv/plex/{id}/progress` `{offset_s, duration_s?, watched?}`: Plex resumes there in every Plex app.

`GET /v1/tv/basic` (same callers). The basic cable lineup a TV shows first ("Your TV"): the local affiliates of the television market nearest the house (Home Assistant's location, within 250 km, at least two stations found), numbered by the station's own number when the name has one, plus one copy of each standard US network on fixed numbers from 20 up (news 20s, sports 30s-40s, entertainment 50s-80s, factual 90s-100s, kids 110s, premium 200s). Among copies the hub prefers one with a guide, then HD/East over SD/West/+1; 4K, PPV, event and foreign copies are left out. `{"version":1,"market":"Raleigh","channels":[{"key","name","number","local"}]}`. ETag / 304. Nothing is hidden: everything else stays in the lineup.

`GET /v1/tv/lineup` (bearer, `media.all` or `media.own_tv`). ETag / 304 like config.

```json
{"version":1710000000,"channels":[
  {"key":"xc:thomas-creek:44102","name":"CNN","number":202,"group":"News","logo":"https://img.example/cnn.png","logo_url":"/v1/art/logo/xc:thomas-creek:44102","kids":false,"catchup_days":2}
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

### One catalog

Several providers are folded into one title. A movie, a series episode, and a live channel each have one canonical id. Every place that copy exists is a source. The TV keeps using the source ref it already has (`vod:`, `series:`, `ep:`, `xc:`). `canonical_id` is added when the hub has folded that row in. Explore treats a title as in the library when any source of its canonical item can be played. `GET /v1/tv/library/search` does the same.

Matching runs in the background when a catalog refresh finds new or changed rows. A request does not match, and it does not check stream health. Providers with tens of thousands of channels are indexed by the cleaned name, not compared one by one.

A pair is folded together only when the hub is sure. Anything less stays two titles.

1. The same TMDB id, the same IMDb id, or the same `epg_channel_id` / `tvg-id`.
2. The same cleaned title and year for a movie, or the same cleaned title and season and episode for a series.
3. For a live channel, the same cleaned name and region, and logos that look like the same file.

Cleaning removes quality tags (`4K`, `FHD`, `HD`, `SD`, `HEVC`), bracketed notes (`[MULTI]`, `ᴿᴬᵂ`), country prefixes (`US|`, `UK:`, `EN -`), punctuation, and accent marks. `ESPN`, `ESPN 2`, `ESPN Deportes`, an East feed, a West feed, and a `+1` timeshift stay separate.

`GET /v1/tv/catalog?q=&kind=movie|series|live`

```json
{"items":[{
  "id":"can_7f3a9c2b1d4e6f80",
  "kind":"movie",
  "title":"The Matrix",
  "year":1999,
  "sources":[
    {"provider":"alpha","stream_id":"100","ref":"vod:alpha:100","quality":"2160p","language":null,"name":"US| The Matrix (1999) [4K] HEVC"},
    {"provider":"beta","stream_id":"55","ref":"vod:beta:55","quality":"1080p","language":"en","name":"EN - The Matrix 1999 FHD"}
  ]
}]}
```

`kind` is `movie`, `series`, or `live`. A series row also has `season` and `episode` when those were in the name. A live row has `region` (`US`, `UK`, or null) and `variant` (`2`, `east`, `deportes`, or null) and `timeshift` (`0` or `1`). `sources` never includes a password or a stream URL. `quality` is `2160p`, `1080p`, `720p`, `sd`, or null. An empty catalog is `{"items":[]}`.

A provider TMDB id (`tmdb` or `tmdb_id` on the Xtream row) decides a pair outright: the same id folds, different ids stay apart. Movie and show posters don't count against a match (copies of one film often carry different art); a channel logo still does. Pairs the rules will not decide are left separate. After each catalog refresh, when `anthropic.api_key` is saved, the hub asks the model in `iptv_match.model` (default `claude-haiku-5-5`) about the waiting pairs, 25 at a time. The model sees the cleaned titles and years plus each copy's provider name, category, genre, rating, TMDB id, plot and running time when known; never logos or links. It answers same, different, or can't tell: same folds the pair (kept like an admin merge), different keeps it apart for good, and can't tell leaves it for an admin and isn't asked again for a week. `iptv_match.daily_tokens` (default `100000`) is the day's cap. With no key, or once the cap is reached, the pair stays two titles and nothing is shown to the household. Nothing waits on an admin: a pair the model can't tell stays two titles. The admin web page shows only one status line (how many copies the AI merged and kept apart).

`GET /v1/tv/catalog/unsure` (`systems`) is the short list an admin can settle. At most 20; `total` counts them all. A pair already folded, kept apart, or no longer in the catalog is left out.

```json
{"unsure":[{"id":"9c1e…","left":"can_aaa","right":"can_bbb","title":"CNN","other":"Cable News Network","score":0.74}],"total":1}
```

`POST /v1/tv/catalog/merge` `{ids:["can_aaa","can_bbb"]}` and `POST /v1/tv/catalog/split` `{id:"can_7f3a9c2b1d4e6f80","provider":"beta","stream_id":"55"}` are `systems`. Merge answers `{"ok":true,"id":"can_aaa"}`. Split answers `{"ok":true,"id":"can_new"}`. A remembered choice wins over the rules and over the model. A second merge of the same pair answers the same id and does not ask the model. Unknown ids answer `404` "That title isn't in the catalog."

### Logins and playback

A provider can have more than one login. Passwords stay in the vault. The old `iptv.xtream_server`, `iptv.xtream_username` and `iptv.xtream_password` keys still count as one provider, `thomas-creek`, with one login, `main`. More providers live in the hub config, without passwords:

```yaml
iptv:
  providers:
    - id: alpha
      name: Alpha
      server: http://iptv.example:8080
      logins:
        - id: house
          username: house
          password_secret: iptv.alpha.house.password
```

`password_secret` is the vault name. The hub web page has an add-login form. It writes the password to the vault and does not show it again.

The hub reads `max_connections`, `active_cons` and `exp_date` from each login's `player_api.php` user info about every ten minutes.

`GET /v1/tv/accounts` (`systems`)

```json
{"logins":[
  {"provider":"alpha","login":"house","max_connections":2,"active":1,"slots_left":1,"expires":"2026-12-01","status":"ok"}
 ],
 "sessions":[
  {"tv":"tv_office_0123456789","name":"Living room","provider":"alpha","login":"house","canonical_id":"can_7f3a9c2b1d4e6f80","since":"2026-11-14T18:00:00+00:00"}
 ]}
```

`status` is `ok`, `full`, `expired`, or `disabled`. `expires` is `YYYY-MM-DD` or null when the login does not expire. Passwords are not in this body. A login that expires within seven days, or that is disabled, tells admins once. It does not tell the household.

The TV asks with a canonical id.

`POST /v1/tv/tune` `{canonical_id, device?}`. `device` is the install id when a phone is sending the picture to a TV. A TV bearer omits it and plays on itself.

```json
{"ok":true,"session":"ses_ab12cd34ef567890abcd","canonical_id":"can_7f3a9c2b1d4e6f80",
 "stream":{"url":"/v1/tv/stream/ses_ab12cd34ef567890abcd","ref":"vod:alpha:100","quality":"2160p"}}
```

The hub picks a source and a login that still has a free slot, prefers a higher quality, and spreads the rooms across logins. A full, expired, or disabled login is skipped. Nothing free:

```json
{"ok":false,"message":"Every login is busy. Try again in a moment."}
```

An unknown canonical id:

```json
{"ok":false,"message":"That title isn't in the catalog."}
```

Both are `200`. The same object is delivered to that TV on `GET /v1/events`:

```json
{"type":"tv_tune","data":{"install_id":"tv_office_0123456789","session":"ses_ab12cd34ef567890abcd","canonical_id":"can_7f3a9c2b1d4e6f80","stream":{"url":"/v1/tv/stream/ses_ab12cd34ef567890abcd","ref":"vod:alpha:100","quality":"2160p"}}}
```

`GET /v1/tv/stream/{session}` with that TV's bearer, on the home network, redirects to the provider. The redirect is how Xtream plays. It is not written to the log. Another TV is `404` "That stream has ended." A request from outside the house is refused like the other TV routes.

If the picture fails to start or drops, the TV posts `POST /v1/tv/tune/fail` `{session}`. The hub frees the slot and sends another `tv_tune` for the next source or login. When nothing else is free, the event is not sent and the answer is `{"ok":false,"message":"Every login is busy. Try again in a moment."}`. `tv.stop` for that TV frees its slot. A TV that has not checked in for 90 seconds frees it too.

`GET /v1/tv/copies` (TV bearer, or a phone with media access) lists the movies and series the catalog folded from more than one copy: `{"version": "<12 hex>", "groups": [{"id": "can_…", "refs": ["vod:alpha:100", "vod:beta:55"]}]}`. Refs are best quality first. Titles with one copy are left out. The TV shows one row per group and plays it with `POST /v1/tv/tune`.

The TV change: play and tune send `canonical_id` to `POST /v1/tv/tune`, open `stream.url` with the TV bearer, and on a startup failure or a drop post `POST /v1/tv/tune/fail`. A new `tv_tune` event replaces the stream. Existing `tv.play` commands are unchanged.

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

`command` is present only on the heartbeat that carried the result. A child sees a `tv` event only for their own room or their own TV. A TV subscription does not receive other TVs' `tv` events, `camera_event`, or camera snapshots. It does receive doorbell and act-now house, kitchen, weather and door notices as `inbox` (display only: no `actions`, no image). An adult TV also receives `decision` / `requests`. Doorbell text is `cat: "doorbell"`. The same stream also carries `home_alert`, `bedtime`, and `dvr_update`, with the shapes in the sections below.

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

### TV fleet (`hub/tvinstall.py`)

`GET /v1/tvapp/fleet?fresh=` (`systems`) lists every Android TV in the house, keyed by IP. It merges the remote setup's network scan, `adb mdns services` (Wireless debugging), the hub's ADB boxes and the registered TVs. It never connects to a TV, so listing never pops an Allow prompt. Each row has `{ip, name, model, room, kind, profile, tv_id, online, app_version, managed, wireless, pairing_port, warning, guide[], state, next, you_do, approve_code}`. `state` is one of `needs_allow`, `pairing`, `installing`, `failed`, `approve`, `update`, `not_installed`, `not_set_up`, `ready` or `offline`. `next` is the one line to show. `guide` is the steps to press on that kind of TV (Shield, Google TV, Fire TV). `others` lists Apple TVs and other non-Android TVs, with a note.

`POST /v1/tvapp/install` `{ips[], name?, room?, profile?, home?, pairing_code?, pairing_port?}` starts `Setup.run` in the background for each IP and returns `{started[status], problems[]}` right away. The hub retries each TV every 5 seconds for 10 minutes while it is `offline` (debugging off) or `needs_allow`. It sends a push when a TV first needs Allow and when it finishes. A pairing code is for one TV; without `pairing_port` the hub reads it from mDNS. Poll `GET /v1/tvapp/setup/{ip}` or the fleet list.

`POST /v1/tvapp/update-all` sends the newest build to every row in `update`: registered TVs through `POST /v1/tvs/{id}/actions` `update`, and unregistered hub boxes through a background install. It returns `{results:[{ip, name, ok, message}]}`.

Wireless debugging listens on a random port. The hub stores `connect_port` from mDNS, connects to `ip:connect_port`, and looks the port up again when a box goes offline.

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
  {"key":"xc:thomas-creek:44102","name":"CNN","number":202,"group":"News","logo":"https://img.example/cnn.png","logo_url":"/v1/art/logo/xc:thomas-creek:44102","kids":false,"catchup_days":2}
]}
```

`logo` is the provider image when it has no credentials in it. `logo_url` is the hub's cached logo. `GET /v1/art/logo/{key}` serves that file.

`GET /v1/tv/guide?start=<unix>&end=<unix>&channel=<key>`

```json
{"from":"2026-11-13T19:00:00+00:00","to":"2026-11-14T02:00:00+00:00","channels":[
  {"key":"xc:thomas-creek:10","name":"CNN","number":20,"group":"News","logo":"https://img.example/cnn.png",
   "kids":false,"catchup_days":2,
   "logo_url":"/v1/art/logo/xc:thomas-creek:10",
   "now":{"start":"2026-11-13T20:00:00+00:00","end":"2026-11-13T21:00:00+00:00","title":"Newsroom","subtitle":null,"desc":"The hour.","icon":null,"catchup":true,"catchup_until":"2026-11-15T20:00:00+00:00","poster_url":"/v1/art/poster/prog:0b49a5b26ac1e2d3739f","backdrop_url":"/v1/art/backdrop/prog:0b49a5b26ac1e2d3739f"},
   "next":{"start":"2026-11-13T21:00:00+00:00","end":"2026-11-13T22:00:00+00:00","title":"Tonight","subtitle":null,"desc":null,"icon":null,"catchup":false,"catchup_until":null,"poster_url":"/v1/art/poster/prog:21f95957753554805ce6","backdrop_url":"/v1/art/backdrop/prog:21f95957753554805ce6"},
   "programmes":[]}
]}
```

`GET /v1/tv/series?q=&category=&limit=`

```json
{"categories":["Kids"],"series":[
  {"id":"series:thomas-creek:12","name":"Bluey","cover":"https://img.example/bluey.jpg","year":2018,"genre":"Kids","rating":"TV-Y","category":"Kids","kids":true,"poster_url":"/v1/art/poster/series:thomas-creek:12","backdrop_url":"/v1/art/backdrop/series:thomas-creek:12"}
]}
```

`GET /v1/tv/series/series:thomas-creek:12` is that object plus `plot`, `backdrop`, and `seasons`.

```json
{"id":"series:thomas-creek:12","name":"Bluey","cover":"https://img.example/bluey.jpg","year":2018,"genre":"Kids","rating":"TV-Y","category":"Kids","kids":true,
 "poster_url":"/v1/art/poster/series:thomas-creek:12","backdrop_url":"/v1/art/backdrop/series:thomas-creek:12",
 "plot":"A puppy.","backdrop":null,"seasons":[
  {"number":1,"episodes":[{"id":"ep:thomas-creek:555","season":1,"number":1,"title":"Keepy Uppy","plot":null,"still":null,"still_url":"/v1/art/still/ep:thomas-creek:555","duration_s":420}]}
]}
```

`GET /v1/tv/movies` and `GET /v1/tv/movies/vod:thomas-creek:99`:

```json
{"categories":["Movies"],"movies":[
  {"id":"vod:thomas-creek:99","name":"The Matrix","cover":null,"year":1999,"genre":"Action","rating":"R","category":"Movies","kids":false,"poster_url":"/v1/art/poster/vod:thomas-creek:99","backdrop_url":"/v1/art/backdrop/vod:thomas-creek:99"}
]}
```

```json
{"id":"vod:thomas-creek:99","name":"The Matrix","cover":null,"year":1999,"genre":"Action","rating":"R","category":"Movies","kids":false,"poster_url":"/v1/art/poster/vod:thomas-creek:99","backdrop_url":"/v1/art/backdrop/vod:thomas-creek:99","plot":"A hacker.","backdrop":null,"duration_s":8160}
```

`GET /v1/tv/library/search?q=news`

```json
{"q":"news","channels":[{"key":"xc:thomas-creek:10","name":"CNN","number":20,"group":"News","logo":null,"logo_url":"/v1/art/logo/xc:thomas-creek:10","kids":false}],
 "programmes":[{"start":"2026-11-13T20:00:00+00:00","end":"2026-11-13T21:00:00+00:00","title":"Newsroom","subtitle":null,"desc":null,"icon":null,"catchup":true,"catchup_until":"2026-11-15T20:00:00+00:00","poster_url":"/v1/art/poster/prog:0b49a5b26ac1e2d3739f","backdrop_url":"/v1/art/backdrop/prog:0b49a5b26ac1e2d3739f","channel_key":"xc:thomas-creek:10","channel_name":"CNN"}],
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

`GET /v1/art/{kind}/{key}?size=m` with a TV or phone bearer that can watch. `kind` is `logo`, `poster`, `backdrop` or `still`. The body is the image. `ETag` is the quoted sha256 of those bytes. `Cache-Control` is `private, max-age=604800`. `If-None-Match` returns `304`. `size` is `s`, `m` (w342), `l` (w780), `original`, or `w92` / `w154` / `w185` / `w342` / `w500` / `w780`. A missing TMDB or fanart.tv key leaves posters and backdrops empty (`404`); channel logos still resolve. Episode stills use TVmaze and need no key.

Channel logos are chosen in this order: the iptv-org logo for the iptv-org id or tvg-id, then a file in tv-logo/tv-logos, then the provider logo. The index refreshes weekly. A background pass fills logos for the lineup and posters for programmes in the next 24 hours.

### DVR

For a channel with no catch-up. The hub records the live stream with ffmpeg (`-c copy`) into one MPEG-TS file on the NAS. Playback is that growing file with HTTP Range, not HLS. `start` and `end` on a recording are the programme times you sent. `pad_before_s` (default 60) and `pad_after_s` (default 180) only change when the file starts and stops; they are not fields on the recording. `error` is `null` when there is nothing to report. `duration_s` is `0` until capture has started. `stream_url` is always the hub path; the stream answers `409 not_ready` until the file has bytes. `poster_url` is `/v1/art/poster/prog:{20 hex}` from the title, or from `programme_key` when that is a `prog:` id. A provider password is never in this payload.

`POST /v1/dvr/recordings` → `201`

```json
{"channel_key":"xc:thomas-creek:10","start":"2026-11-14T01:00:00+00:00","end":"2026-11-14T02:00:00+00:00","title":"Newsroom","programme_key":"prog:0b49a5b26ac1e2d3739f","pad_before_s":60,"pad_after_s":180}
```

```json
{"id":"dvr_ab12","channel_key":"xc:thomas-creek:10","title":"Newsroom","start":"2026-11-14T01:00:00+00:00","end":"2026-11-14T02:00:00+00:00","status":"scheduled","size_bytes":0,"duration_s":0,"poster_url":"/v1/art/poster/prog:0b49a5b26ac1e2d3739f","error":null,"stream_url":"/v1/dvr/recordings/dvr_ab12/stream"}
```

`POST /v1/dvr/recordings/now` `{"channel_key":"xc:thomas-creek:10","duration_s":3600,"title":"News"}` → `201` with `"status":"recording"`. `duration_s` must be 60 to 28800.

`POST /v1/dvr/rules` `{"channel_key":"xc:thomas-creek:10","title_match":"Newsroom","new_only":true}` → `201` `{"id":"dvrule_ab12","channel_key":"xc:thomas-creek:10","title_match":"Newsroom","new_only":true}`. The scheduler adds a recording when a matching programme is in the guide. `new_only` skips a start time that is already scheduled.

`GET /v1/dvr/recordings?status=scheduled` → `{"recordings":[ <recording> ]}`. `GET /v1/dvr/recordings/{id}` is one recording. `DELETE` → `{"ok":true}`. `POST /v1/dvr/recordings/{id}/stop` → the recording with `"status":"cancelled"`.

`status` is `scheduled`, `recording`, `done`, `failed` or `cancelled`. `409 conflict` when the recording would open more streams than `dvr.max_connections` at one time. The oldest finished recording is deleted when the folder passes `dvr.quota_bytes`. A restart keeps a future recording; one whose window passed while the hub was down is `failed`.

`GET /v1/dvr/recordings/{id}/stream` is `video/mp2t` with `Accept-Ranges: bytes`. `Range: bytes=10-19` on a 1000-byte file is `206` and `Content-Range: bytes 10-19/1000`. The same call works while the file is still growing.

SSE, on every status change, to phones and TVs:

```json
{"type":"dvr_update","data":{"id":"dvr_ab12","channel_key":"xc:thomas-creek:10","title":"Newsroom","start":"2026-11-14T01:00:00+00:00","end":"2026-11-14T02:00:00+00:00","status":"recording","size_bytes":0,"duration_s":0,"poster_url":"/v1/art/poster/prog:0b49a5b26ac1e2d3739f","error":null,"stream_url":"/v1/dvr/recordings/dvr_ab12/stream"}}
```

A child only sees a recording of a kids' channel, and only a kids' channel can be recorded from a kids' profile.

`GET /v1/tv/apps` with a TV bearer. `allowed` is always present. A kids profile, and any child or guest using the TV, gets the kid apps from the household catalog. An adult profile gets every catalog app that has an Android package, including those kid apps.

```json
{"allowed":["com.google.android.youtube.tvkids","com.netflix.ninja","com.plexapp.android","com.disney.disneyplus","com.pbs.kids.pbskidsvideo"]}
```

```json
{"allowed":["com.google.android.youtube.tv","com.google.android.youtube.tvkids","com.netflix.ninja","com.plexapp.android","com.disney.disneyplus","com.pbs.kids.pbskidsvideo","com.amazon.amazonvideo.livingroom","com.wbd.stream","com.hulu.livingroomplus","com.spotify.tv.android","com.apple.atve.androidtv.appletv"]}
```

### Kids launcher

A TV with the kids launcher on gets the launcher's list from `GET /v1/tv/apps` instead of the profile's list, in tile order.

The TV config (`POST /v1/tv/config` and the pair answer) always carries `launcher`. Off is `{"on":false}`. On:

```json
{"launcher":{"on":true,"person_id":"per_theo","apps":["com.google.android.youtube.tvkids","com.disney.disneyplus"],"guard":true}}
```

`person_id` is who the TV signs in as with no "Who's watching?" (null: the TV asks as usual). A child there also makes the TV's profile `kids`. `guard` means the TV goes back home when an app that isn't on the list comes to the front.

`POST /v1/tv/apps/installed` with a TV bearer: `{"apps":[{"package":"com.google.android.youtube.tv","label":"YouTube"}]}` (up to 200), the apps this TV can open. The answer is `{"ok":true,"count":1}`.

Admins (`systems`): `GET /v1/tvapp/tvs/{id}/launcher` answers the setting plus `status` (what the hub last did on the box: `home`, `guard`, `previous`, `note`), `installed` (the list above) and `box` (the address the hub reaches it on, or null). `PUT /v1/tvapp/tvs/{id}/launcher` takes `{on, person_id?, apps, guard?}` and answers the setting with `status`. When the TV's address is a box on the hub's list, the hub enables the HOME alias, makes it Home (keeping the previous one), allows Usage access and turns on the accessibility guard `me.dlewis.thomascreek.tv/com.thomascreek.tv.device.GuardService`. Turning it off reverses all of that. The admin page has the same card under Devices.

`GET /v1/tv/update` and `GET /v1/tv/apk` use the TV bearer. The file download is the bytes of the APK. The manifest:

```json
{"version_code":602,"version_name":"1.4.1-tc.2","sha256":"abc","signer_sha256":"deadbeef",
 "apk_path":"/v1/tv/apk","package":"me.dlewis.thomascreek.tv","notes":null,
 "install":"Download the APK with this TV's bearer, check sha256 and signer_sha256, then hand the file to PackageInstaller. The person confirms on the TV.",
 "idle_only":true,"silent":true,"window":{"from":"01:00","to":"06:00","timezone":"America/New_York"}}
```

The `install` sentence is unchanged. A TV that already knows it still confirms on screen. A current TV reads `idle_only`, `silent`, and `window` and does not ask. It downloads only while idle, inside that window, in the hub's timezone. `from` is included and `to` is not: 01:00 starts it, 06:00 stops it. Outside that window, or while something is playing, it does nothing and shows nothing. `GET /v1/tv/apk` still answers at any hour, so an older TV can update when a person asks.

The hub already keeps one build: `version_code`, `sha256`, and the signing certificate. The first build pins `signer_sha256`. A later build with a different certificate is not kept and is not offered. The TV checks `sha256` and `signer_sha256` before it installs.

`GET /v1/tv/update?current=602` with the version the TV is running. When that is the build the hub has, or newer, the body is only:

```json
{"version_code":602,"up_to_date":true}
```

No `apk_path`, so there is nothing to download. A missing or lower `current` is the full manifest above. A `current` that is not a number is treated as missing.

After a silent install the TV reports once, with its bearer, on the home network:

`POST /v1/tv/update/health`

```json
{"version_code":602,"boot_ok":true,"hub_ok":true,"playback_ok":true,"rollback":false}
```

The answer is `{"ok":true}`. `rollback` true means the TV put the previous build back. A report that is missing a field, or is not true or false, is `400` "Say how the update went." The hub stores the latest report for that TV and does not send a push or an event.

It shows only on `GET /v1/tvapp` (`systems`). A TV that has not reported is left off the list. A problem is first.

```json
{"updates":[{"id":"tv_office_0123456789","name":"Living room","version_code":602,"boot_ok":true,"hub_ok":true,"playback_ok":false,"rollback":false,"ok":false,"at":"2026-11-14T06:10:00+00:00"}]}
```

`ok` is true only when boot, the hub, and playback are ok and the TV did not roll back.

### Downloads

The hub web page has a Downloads page at `/admin/downloads`, linked from the nav. It is the same sign-in as `/admin`. It lists the Thomas Creek TV builds the hub has on disk. The latest one is the build behind `GET /v1/tv/update`. Older files sit in a collapsed list. Each row has the version name, version code, date, size, sha256, and a download link. When nothing is stored the page says "No builds yet" and how to publish one.

`GET /tv` on the home network, with no bearer, redirects to the latest build. `GET /tv/{version_code}` is that file, `application/vnd.android.package-archive`, named `ThomasCreekTV-{version_code}.apk`. Away from home, or when that build is not here, the response is a short page: "This link only works at home." or "No builds yet." It is not a stack trace.

That `/tv` address is what gets registered at [https://go.aftvnews.com/](https://go.aftvnews.com/). The hub does not invent a numeric code. Once Dylan has registered the address, he puts the number in the hub config:

```yaml
tv_app:
  downloader_code: "48291"
```

Digits only, 4 to 12 characters. Anything else is ignored. The Downloads page shows the number in large type after it is set, and saving it there stores the same setting. The address always points at the latest build, so the number does not change when a new APK arrives.

Where a build comes from. A `tc-*` tag in `dylanl321/thomascreek-tv` publishes two release files, `ThomasCreekTV.apk` and `update.json`:

```json
{"versionCode":602,"versionName":"1.4.1-tc.2","apk":"ThomasCreekTV.apk","sha256":"<64 hex>","notes":"tc-1.4.1-tc.2"}
```

The hub downloads that release when `github.tv_app_token` is saved (the same check as `POST /v1/tvapp/check`). The same two files can be placed in the hub data folder `tvapp/inbox`, or uploaded on the Downloads page. The checksum must match, and the signing certificate must match the pin from the first build kept. A file that fails either check is not kept, and the page says so in one sentence.

`GET /v1/tv/plex`

```json
{"continue":[{
  "id":"9","title":"Bluey: Keepy Uppy","year":2018,"kind":"episode","rating":"TV-Y",
  "poster_url":"/v1/media/art?path=%2Flibrary%2Fbluey","summary":"A game.","show":"Bluey","season":1,"episode":2,
  "deep_link":"plex://preplay?metadataKey=/library/metadata/9&server=MACHINE","package":"com.plexapp.android"
}],"on_deck":[],"recent":[]}
```

### Send to another room

`position_s` on `playing` is whole seconds from the start, 0 to 86400. The TV may omit it. These fields are also accepted on `playing` and ignored when absent: `kind` (`live`, `catchup`, `movie`, `episode`), `movie_id`, `series_id`, `episode_id`, `start`. A channel handoff needs `channel_key`. A film needs `movie_id`. An episode needs `episode_id`. If the TV only sends the title, the hub fills those in from the last `tv.play` it sent that TV.

`POST /v1/tv/state`

```json
{"playing":{"channel_key":"xc:thomas-creek:10","title":"Newsroom","name":"CNN","number":20,"position_s":125},
 "foreground":true,"health":{"app_version":"1.4.1","profile":"adult","epg_age_s":120,"last_error":null,"home_crashes":0}}
```

`GET /v1/tv/devices` uses the same bearer as `GET /v1/tv/players`. A child only sees a TV in their own room or their own set. The room they last moved something to is first, and that row has `"usual":true`. `media_entity` is present when the TV is linked to a Home Assistant player, so a phone can match the now-playing card. `room` is the area id.

```json
{"devices":[
  {"id":"tv_bed_0123456789abcd","name":"Bedroom","room":"bedroom","online":true,"usual":true,
   "playing":null},
  {"id":"tv_office_0123456789","name":"Living room","room":"living_room","online":true,
   "media_entity":"media_player.living",
   "playing":{"channel_key":"xc:thomas-creek:10","title":"Newsroom","name":"CNN","number":20,"position_s":125,"poster_url":"/v1/art/poster/prog:0b49a5b26ac1e2d3739f"}}
]}
```

`poster_url` is added on `playing` when the hub can name artwork. A film uses its poster, a series uses the series poster, a titled programme uses the programme poster, and a channel with no title uses the channel logo. The phone fetches that path with the bearer. It is omitted when nothing is playing, or when there is no title and no channel. The art route answers `404` when the file is not cached yet. The card still shows the title.

`POST /v1/tv/handoff` `{from_device, to_device}`. Either value is an install id or the linked `media_entity`. The hub reads what's playing and `position_s`, sends `tv.play` to `to_device` with `resume` set to that position, then `tv.stop` to `from_device`. Nothing is playing, or it's the same TV, answers 200 and does not send a command:

```json
{"ok":false,"moved":false,"message":"Nothing is playing on Living room","from":"tv_office_0123456789","to":"tv_bed_0123456789abcd"}
```

Moved:

```json
{"ok":true,"moved":true,"message":"Moved Newsroom to Bedroom","from":"tv_office_0123456789","to":"tv_bed_0123456789abcd",
 "dry_run":false,"command_id":"tvc_ab12","stop_command_id":"tvc_ab13"}
```

The play command is the same `tv_command` as any other play, with `resume` set. A TV that hasn't checked in for two minutes still gets the command: `message` says it'll come on when that TV wakes up. A kids' TV asked to play something else answers `403` with `message` "Theo's TV only plays kids' shows". A room this person can't use answers `403` "That room isn't one you can use". Unknown TV is `404`.

### iPhone remote

The phone is the remote for a room. Moving a show to another room stays `POST /v1/tv/handoff`. Starting a title stays `POST /v1/actions` with `tv.play`. This endpoint is the buttons on the remote.

`POST /v1/tv/remote` uses the same bearer as `GET /v1/tv/devices`.

```json
{"device":"tv_office_0123456789","command":"pause"}
```

`device` is an install id or the linked `media_entity`. Leave it out and the hub picks a TV: the one that is playing, then the only one that is on, then the room this person last moved a show to, then the only TV they can use. If more than one fits, it does not guess:

```json
{"ok":false,"message":"Pick a room."}
```

`command` is `play`, `pause`, `seek`, `channel_up`, `channel_down`, `tune`, `text`, `back`, or `home`.

`play` and `pause` are the transport buttons. `play` resumes what is already on. It does not start a new title. `seek` includes `position_s`, whole seconds from 0 to 86400. `tune` includes `channel_key`. `text` includes `text`, up to 80 characters, for the search box. `channel_up`, `channel_down`, `back`, and `home` need nothing else.

The hub answers immediately. It does not wait for the TV.

```json
{"ok":true,"id":"tvr_ab12cd34ef567890abcd"}
```

The TV receives it on the existing `GET /v1/events` stream, and only that TV. It is not added to `GET /v1/tv/commands`, and the TV does not acknowledge it. Phones do not receive this event.

```json
{"type":"tv_remote","data":{"id":"tvr_ab12cd34ef567890abcd","install_id":"tv_office_0123456789","command":"seek","position_s":125,"at":"2026-11-14T01:30:00+00:00"}}
```

`channel_key` is included only for `tune`. `text` is included only for `text`. A TV that is not on the event stream misses the command. `online` on the room card already says whether that TV has checked in. The tap still answers `ok`.

A kids' profile, or a child, tuning a channel that is not for kids is `403`:

```json
{"error":"forbidden","message":"That channel isn't for this TV"}
```

A room this person can't use is `403` "That room isn't one you can use". An unknown TV is `404` "That TV isn't registered". A command the remote does not have is `400` "That button isn't on this remote." A seek without a place is `400` "Say where to skip to." A tune without a channel is `400` "Say which channel." Empty text, or text that looks like an address, is `400` "Say what to type."

More than twelve commands to the same TV in five seconds answers `200` and sends nothing:

```json
{"ok":false,"message":"That's as fast as it goes."}
```

No push is sent for a remote button.

### Doorbell and the door cameras

The hub finds the doorbell and the cameras by a door (the name or the room mentions a door, the porch, the driveway, or the entry). One camera in the house is used when none of them says so. Nothing has to be set. Quiet hours are 22:00 to 07:00 in the hub's timezone: a doorbell still shows, and a person or motion waits until morning. `00:00` to `00:00` turns quiet hours off. Motion is off until someone asks for it. The same camera does not alert again for a short while.

`home_alert` on `GET /v1/events`, for every TV and for phones that aren't a guest:

```json
{"type":"home_alert","data":{"id":"ha_ab12cd34","kind":"doorbell","camera":"driveway","title":"Someone's at the door","snapshot_url":"/v1/tv/alerts/ha_ab12cd34/snapshot.jpg","stream_url":"/v1/tv/alerts/ha_ab12cd34/stream","ttl_s":90,"stream":"mjpeg"}}
```

`kind` is `doorbell`, `person` or `motion`. `ttl_s` is how long the card stays up (90, 45 or 20). `snapshot_url` and `stream_url` are omitted when no camera could be found; the title still shows. Both URLs are on the hub and need the same bearer. They contain no Home Assistant address and no password. `stream` is `mjpeg` or, when go2rtc is configured, `hls`.

`GET /v1/tv/alerts/{id}/snapshot.jpg` is `image/jpeg`. A TV bearer works at home. A phone at home works. Away from home it needs `cameras`, the same as the other camera pictures. An alert older than ten minutes, or a camera that doesn't answer, is `404` `{"error":"gone","message":"That alert has passed"}`.

`GET /v1/tv/alerts/{id}/stream` is `multipart/x-mixed-replace` (MJPEG) or, when `stream` is `hls`, `application/vnd.apple.mpegurl`. The playlist's segment URLs are `/v1/tv/alerts/{id}/media?src=` and need the same bearer. A segment path with `://` or `..` is ignored.

`GET /v1/tv/alerts/settings` is what is in effect, including the cameras that were found:

```json
{"cameras":["driveway"],"rooms":null,"quiet":{"from":"22:00","to":"07:00"},"motion":false,"auto":true,
 "found":[{"id":"driveway","name":"Driveway","room":"front","door":true}]}
```

`PUT /v1/tv/alerts/settings` is admin. `cameras: null` or `rooms: null` goes back to finding them. `cameras` lists ids such as `driveway`. The same block can live under `home_assistant.extra` as `door_cameras`, `door_rooms`, `alert_quiet` and `alert_motion`; a saved PUT wins.

### Up Next

`GET /v1/tv/upnext?profile=adult|kids`. The same bearer as the guide. A child, a guest or a kids' TV always gets the kids shelf, even if `profile=adult`. Anyone else asking `profile=kids` gets the kids shelf. With no profile, a child gets kids and everyone else gets adult.

The shelf is ranked from plays this profile has made. A profile that has not watched anything gets `{"rows":[]}`.

```json
{"rows":[
  {"id":"continue","title":"Continue watching","items":[
    {"kind":"movie","movie_id":"vod:thomas-creek:101","title":"Kids Movie","subtitle":"10 min in","reason":"continue watching","poster_url":"/v1/art/poster/vod:thomas-creek:101","backdrop_url":"/v1/art/backdrop/vod:thomas-creek:101","progress":0.12}
  ]},
  {"id":"next","title":"New episodes","items":[
    {"kind":"episode","episode_id":"ep:thomas-creek:901","series_id":"series:thomas-creek:50","title":"Next","subtitle":"Cartoon Show","reason":"new episode of a series you watch or record","poster_url":"/v1/art/poster/series:thomas-creek:50"}
  ]},
  {"id":"recordings","title":"Ready to watch","items":[
    {"kind":"recording","recording_id":"dvr_abc123","channel_key":"xc:thomas-creek:10","title":"Newsroom","subtitle":"Recorded","reason":"recording ready","poster_url":"/v1/art/poster/prog:abc"}
  ]},
  {"id":"onnow","title":"On now","items":[
    {"kind":"live","channel_key":"xc:thomas-creek:10","title":"Newsroom","subtitle":"CNN","reason":"starting now on a channel you watch","poster_url":"/v1/art/poster/prog:0b49a5b26ac1e2d3739f"}
  ]},
  {"id":"trending","title":"For you","items":[
    {"kind":"live","channel_key":"xc:thomas-creek:12","title":"Sports","subtitle":"You watch this","reason":"trending in your history"}
  ]}
]}
```

An item carries only the ids it has. `progress` is 0 to 1 and only present when the hub knows how long the title is. `reason` is one of `continue watching`, `new episode of a series you watch or record`, `recording ready`, `ready to watch`, `starting now on a channel you watch`, `trending in your history`. On now is a programme that started within the last 20 minutes on a channel this profile watched in the last 30 days. A finished recording is one the DVR marked `done`. A title asked for from Explore, once it can be played, is added to Ready to watch with `reason` `ready to watch` and `movie_id` or `series_id`. Rows with nothing in them are left out. The order is continue, new episodes, recordings, on now, for you.

### Watching on the phone (`hub/routes/tvphone.py`)

`POST /v1/tv/phone/play` with one of `{"movie_id"}`, `{"episode_id"}` or `{"channel_key"}`. Same bearer and kids rule as the guide (a child or guest only gets kids' titles; anything else is 404). The answer is `{"url","kind","title","expires_at"}`, where `url` is a hub path with a 6-hour pass in it. The provider login never leaves the hub, and the pass only names the title.

- Films and episodes: `GET /v1/tv/phone/file/{pass}`, a byte-range proxy of the provider file. Only mp4, m4v and mov play on the phone; anything else answers 409 `tv_only` ("This one only plays on a TV"). Episodes need the series opened once (`GET /v1/tv/series/{id}`), as on the TV.
- Channels: `GET /v1/tv/phone/live/{pass}/index.m3u8`. The hub reads the provider's HLS and points every line at `/v1/tv/phone/live/{pass}/p/{n}.m3u8` or `/s/{n}`. The provider's own addresses stay in the hub's memory. Reloads use the redirected playlist, not the login address.
- Plex titles keep using `GET /v1/media/{id}/stream`.

The iPhone opens these through the published address when one is set, because its player can't use the pinned home certificate.

### Explore

`GET /v1/tv/explore?profile=adult|kids`. The same bearer as the guide. A child, a guest or a kids' TV always gets the kids feed, even if `profile=adult`. Anyone else asking `profile=kids` gets the kids feed. The answer is the last built feed. It does not call TMDB or the provider while the TV is waiting. The hub rebuilds it about every six hours.

A title is on the feed only when the household can play it, or when it can be asked for and the request stack can actually take it. Playable means it is in the IPTV movie or series library, in Plex's recently added list, or a finished DVR recording. The live channel lineup is not scanned. Askable means Jellyseerr is set up and the title has a TMDB id. Anything else is left off.

```json
{"hero":{
  "kind":"movie","title":"The Matrix","year":1999,"overview":"A hacker.","tagline":"Welcome to the Real World",
  "poster_url":"/v1/tv/explore/art/poster/movie/603","backdrop_url":"/v1/tv/explore/art/backdrop/movie/603",
  "logo_url":"/v1/tv/explore/art/logo/movie/603","title_card_url":"/v1/tv/explore/art/title/movie/603","rating":"R","genres":["Action","Science Fiction"],
  "reason":"New this week","badges":["New"],"availability":"play","movie_id":"vod:thomas-creek:100"
 },
 "rows":[
  {"id":"new","title":"New releases","items":[
    {"kind":"movie","title":"The Matrix","year":1999,"overview":"A hacker.","tagline":"Welcome to the Real World",
     "poster_url":"/v1/tv/explore/art/poster/movie/603","backdrop_url":"/v1/tv/explore/art/backdrop/movie/603",
     "logo_url":"/v1/tv/explore/art/logo/movie/603","title_card_url":"/v1/tv/explore/art/title/movie/603","rating":"R","genres":["Action","Science Fiction"],
     "reason":"New this week","badges":["New"],"availability":"play","movie_id":"vod:thomas-creek:100"}
  ]},
  {"id":"popular","title":"Popular","items":[
    {"kind":"series","title":"Bluey","year":2018,"overview":"A puppy.","tagline":null,
     "poster_url":"/v1/tv/explore/art/poster/tv/82728","backdrop_url":"/v1/tv/explore/art/backdrop/tv/82728",
     "logo_url":"/v1/tv/explore/art/logo/tv/82728","title_card_url":"/v1/tv/explore/art/title/tv/82728","rating":"TV-Y","genres":["Kids"],
     "reason":"Popular this week","badges":["Popular","Top 10"],"availability":"request",
     "tmdb_id":82728,"media_id":"tv:82728"}
  ]},
  {"id":"acclaimed","title":"Must watch","items":[
    {"kind":"movie","title":"Spirited Away","year":2001,"overview":"A girl.","tagline":null,
     "poster_url":"/v1/tv/explore/art/poster/movie/129","backdrop_url":"/v1/tv/explore/art/backdrop/movie/129",
     "logo_url":"/v1/tv/explore/art/logo/movie/129","title_card_url":"/v1/tv/explore/art/title/movie/129","rating":"PG","genres":["Animation"],
     "reason":"Acclaimed","badges":["Top 10"],"availability":"play","movie_id":"vod:thomas-creek:129"}
  ]}
 ]}
```

`kind` is `movie` or `series`. `availability` is `play` or `request`. A playable movie has `movie_id`. A playable series has `series_id`. A Plex title also has `plex_id`. A finished recording has `recording_id`. A request has `tmdb_id` and `media_id` (`movie:603` or `tv:82728`). `request_id` is present once someone has asked. `tagline` is null when the hub does not have one. When a title has a TMDB id, `logo_url` is always `/v1/tv/explore/art/logo/{movie|tv}/{tmdb_id}` and `title_card_url` is always `/v1/tv/explore/art/title/{movie|tv}/{tmdb_id}`, even if the list payload had no logo. The logo file is the English logo from TMDB `/{movie|tv}/{id}/images`, or a logo with no language when there is no English one. Other languages are not used. The title card is an English backdrop from that same call, which is the picture with the title painted on. A library title with no TMDB id has both fields null. `poster_url`, `backdrop_url`, `logo_url` and `title_card_url` are hub paths. Fetch them with the bearer. A missing file is `404` "No artwork". The card still shows the title.

`badges` is any of `New`, `Popular`, `Top 10`, `Requested`. `reason` is `New this week`, `Popular this week`, `Acclaimed`, or `In your library`. Rows with nothing in them are left out. The order is New releases, Popular, Must watch. The hero is the first new title that has a backdrop, or the first playable title. With nothing to show, the feed is `{"hero":null,"rows":[]}`.

Kids titles are limited to the ratings in `child_policy.ratings`. A TMDB title with no US rating is left out of the kids feed unless it is already in a kids library category. An adult feed is not limited that way.

With no `tmdb.api_key` in the vault, the hub does not call TMDB. The feed is one row from the library. `poster_url` and `backdrop_url` are still `/v1/art/poster/{id}` and `/v1/art/backdrop/{id}`. Those files are the provider picture already on the title (`stream_icon` or `cover`, and `backdrop_path` from the list or from `get_vod_info` / `get_series_info`), a Plex thumb or art fetched by the hub's Plex client, or a recording thumbnail. The TV only ever requests the hub path. A title with none of those is a 404, and the card is a plain tile.

```json
{"hero":{"kind":"movie","title":"Kids Movie","year":2018,"overview":null,"tagline":null,
  "poster_url":"/v1/art/poster/vod:thomas-creek:101","backdrop_url":"/v1/art/backdrop/vod:thomas-creek:101",
  "logo_url":null,"title_card_url":null,"rating":"G","genres":["Kids"],"reason":"In your library","badges":["New"],
  "availability":"play","movie_id":"vod:thomas-creek:101"},
 "rows":[{"id":"library","title":"Recently added","items":[
   {"kind":"movie","title":"Kids Movie","year":2018,"overview":null,"tagline":null,
    "poster_url":"/v1/art/poster/vod:thomas-creek:101","backdrop_url":"/v1/art/backdrop/vod:thomas-creek:101",
    "logo_url":null,"title_card_url":null,"rating":"G","genres":["Kids"],"reason":"In your library","badges":["New"],
    "availability":"play","movie_id":"vod:thomas-creek:101"}
 ]}]}
```

`POST /v1/tv/explore/request` `{tmdb_id, kind}`. `kind` is `movie` or `series`. One tap. The answer is always a sentence, not an error code, except a body that does not name a title (`400` "Say which title.").

Already in the library:

```json
{"ok":true,"status":"play","movie_id":"vod:thomas-creek:100","message":"It's already in the library."}
```

An adult, with Jellyseerr set up:

```json
{"ok":true,"status":"requested","request_id":"88","tmdb_id":603,"kind":"movie","media_id":"movie:603","message":"Asked for The Matrix."}
```

A child:

```json
{"ok":true,"status":"waiting","request_id":"req_ab12","tmdb_id":603,"kind":"movie","media_id":"movie:603","message":"Asked a parent."}
```

Jellyseerr is not set up, or the title is not one it can take:

```json
{"ok":false,"status":"unavailable","message":"That isn't something we can ask for yet."}
```

A kids' profile asking for a grown-up rating:

```json
{"ok":false,"status":"unavailable","message":"That one needs a grown-up."}
```

Progress is an SSE event on `GET /v1/events`, for phones and TVs. No push. `status` is `requested`, `downloading`, or `ready`.

```json
{"type":"explore","data":{"tmdb_id":603,"kind":"movie","title":"The Matrix","status":"ready","profile":"adult","movie_id":"vod:thomas-creek:100"}}
```

`series_id` is used instead of `movie_id` for a series. `profile` is `adult` or `kids`. A kids' TV and a child only receive the kids profile. The same event is what moves the card, and a `ready` title is the Up Next item above.

### Channel health

The provider's lineup is far too large to check channel by channel. The score comes from plays. `POST /v1/tv/playback_events` with a TV or phone bearer:

```json
{"events":[{"channel_key":"xc:thomas-creek:10","source":"thomas-creek","ok":true,"startup_ms":400,"ts":1710000000}]}
```

`source` is a short name the TV already uses for that copy of the channel. It is not a URL. An event whose source or error contains `://` is ignored. At most 40 events are kept from one call. The answer is `{"ok":true,"accepted":1}`.

Each source keeps the last 12 results. `score` is how often those worked, from 0 to 1, pulled down a little when startup is slow. `last_ok` is when it last worked, or null. Five failures in a row means the source is left off the default. It stays in the list. Nothing is deleted.

On `GET /v1/tv/lineup` and `GET /v1/tv/guide`, a channel that has been played gains:

```json
{"health":{"score":0.9,"last_ok":"2026-11-14T01:00:00+00:00"},
 "sources":[
   {"source":"thomas-creek","score":0.9,"last_ok":"2026-11-14T01:00:00+00:00","default":true},
   {"source":"slow","score":0,"last_ok":null,"default":false}
 ]}
```

`sources` is best first. The TV's source switcher uses the first `default: true`. A channel with no plays is unchanged.

`GET /v1/tv/health` is the same sources, worst first, for the phone:

```json
{"sources":[{"channel_key":"xc:thomas-creek:10","name":"CNN","source":"slow","score":0,"last_ok":null,"default":false}]}
```

Overnight, between 1am and 6am, the hub opens a stream for favorites, channels watched in the last 30 days, and at most the 200 most played. It checks one or two at a time, never more than the provider allows, and never the rest of the lineup. A check that fails is another play result. The address is not stored and not returned.

### Kids' bedtime

A kids profile starts with 90 minutes a day and the household bedtime window (`child_policy.bedtime`, default 20:30, until `smart.wake_time`, default 07:00). A warning comes 10 minutes before either limit. An adult profile has no limit until someone sets one. Nothing has to be configured.

`GET /v1/tv/bedtime?profile=kids`

```json
{"profile":"kids","daily_minutes":90,"from":"20:30","to":"07:00","warn_minutes":10,"scene":"scene.good_night","state":"warning","minutes_left":10}
```

`state` is `unlocked`, `warning`, or `locked`. `minutes_left` is present on a warning, and `0` when the daily minutes are used up. `scene` is a Home Assistant scene the hub found (a name like bedtime, good night, or dim), or null. `PUT /v1/tv/bedtime` is admin and accepts `profile`, `daily_minutes`, `from`, `to`, `warn_minutes`, and `scene`. `scene: ""` means don't touch the lights.

`bedtime` on `GET /v1/events`:

```json
{"type":"bedtime","data":{"state":"warning","minutes_left":10,"profile":"kids"}}
```

A kids' TV receives the kids profile. An adult TV receives the adult profile. Phones receive both. The older person-bedtime event, which has no `data.state`, is unchanged.

When the state becomes `locked`, the hub runs that scene if `scene.run` is a live action. If the scene is missing or the house doesn't answer, the TV still locks.

`POST /v1/tv/bedtime/extend` `{profile,minutes}` is a parent (admin or adult). `minutes` is 5 to 120. It adds that much time and lifts the night window for that long. A child gets `403` "Ask a parent". A warning also tells parents once, in the app.

### Ambient

`GET /v1/ambient?device=` for a TV or a phone that can watch. The date picks the scene. Nothing has to be configured.

```json
{"season":"autumn","holiday":null,"ambience":{"id":"leaves","title":"Autumn leaves","channel_key":"xc:thomas-creek:7","poster_url":"/v1/art/logo/xc:thomas-creek:7"},
 "widgets":{"weather":{"temp":62,"condition":"Cloudy"},"calendar":[{"title":"Practice","start":"2026-11-14T16:00:00-05:00"}],
  "cameras":[{"name":"Driveway","snapshot_url":"/v1/ambient/cameras/driveway.jpg"}],
  "art":["/v1/art/poster/prog:0b49a5b26ac1e2d3739f"]}}
```

`season` is `winter`, `spring`, `summer`, or `autumn`. `holiday` is `halloween` through October, `christmas` from 1 to 26 December, or null. Winter uses Fireplace on an even day of the month and Snowfall on an odd day, except during Christmas. `ambience` has `channel_key` when a lineup channel name matches the scene, or `stream_url` when one was saved. A stream URL with a username or password is not kept. `poster_url` is omitted when there is no artwork. `widgets.weather` is null until a forecast is cached. `calendar`, `cameras`, and `art` are empty lists when there is nothing to show. Camera stills are `GET /v1/ambient/cameras/{name}.jpg` with the same bearer. A camera that doesn't answer is `404` "That camera didn't answer".

The eight scenes are Fireplace, Snowfall, Christmas lights, Autumn leaves, Halloween, Spring rain, Summer beach, and Aquarium.

`GET /v1/ambient/library` lists them. `PUT /v1/ambient` is admin: `{pin, device?, items:[{id, channel_key?, stream_url?, poster_url?}]}`. `pin` is one of the ids, or null to follow the date. A `device` pin applies only to that TV. The same assignments can live in the hub config under `ambient.pin` and `ambient.streams`. A saved PUT wins.

### Siri

Three phrases on the iPhone. Siri includes the app name, which is how the shortcuts are registered. Each one stays in Siri. A missing show, room, or play is a sentence.

Play `<thing>` in `<room>`. `GET /v1/search?q=` first. If that hit is already a `tv.play` action, it is sent as-is. Otherwise `GET /v1/tv/library/search?q=` supplies a channel, the programme on now, or a movie, and that becomes `POST /v1/actions` `tv.play` on the TV in that room. One TV in the house is used when the room is left out. The room is the TV's name or its room.

Move the TV to `<room>`. `GET /v1/tv/devices`, then `POST /v1/tv/handoff` from the TV that is playing to the TV in that room. Nothing playing answers with that sentence and does not send a command.

Turn off the TV in `<room>`. `POST /v1/actions` `tv.stop` on that TV.

### Zero-setup pairing

The hub advertises `_thomascreek._tcp` on the home network. A new TV posts here with no bearer. Home network only. Eight requests from one address in ten minutes; the next answer is `429` `{"error":"rate_limited","message":"Wait a moment and try again."}`. A pending request expires after 15 minutes.

`POST /v1/tv/pair`

```json
{"install_id":"tv_office_0123456789","name":"Living room TV","model":"onn 4K","app_version":"1.5.0","suggested_room":"living_room","public_key":"<base64 P-256>"}
```

`install_id` is 16 to 64 letters, digits, `-` or `_`, and it is the stable identity. `public_key` is the same encoding as `POST /v1/tv/enroll`. `suggested_room` is an area id such as `living_room`, or a room name. A brand-new TV:

```json
{"status":"pending","pair_id":"pair_ab12cd34ef567890abcd","expires_at":"2026-11-14T01:15:00+00:00","poll_after_s":2}
```

`GET /v1/tv/pair/{pair_id}` (no bearer, home network) is how the TV waits. Still waiting:

```json
{"status":"pending","pair_id":"pair_ab12cd34ef567890abcd","expires_at":"2026-11-14T01:15:00+00:00","poll_after_s":2}
```

Approved:

```json
{"status":"approved","access_token":"eyJ…","token_type":"bearer","expires_at":"2026-11-14T01:30:00+00:00","expires_in":900,
 "role":"tablet","profile":"adult","room":"living_room","capabilities":["climate.own_room","family.view","lights.own_room","media.own_tv"],
 "tv":{"id":"tv_office_0123456789","name":"Living room TV"},
 "config":{"status":"approved","version":3,"name":"Living room TV","profile":"adult","room":"living_room","media_entity":null,
  "playlists":[{"type":"xtream","name":"Thomas Creek","server":"http://iptv.example:8080","username":"household","password":"s3cret"}],
  "epg_url":null,"lineup_version":12,"theme":null,"favorites":["xc:thomas-creek:10"]}}
```

`config` is the same approved `POST /v1/tv/config` body, plus `favorites` (channel keys starred in the house). It also has `kids_categories` and `people`. Denied, or a request older than 15 minutes:

```json
{"status":"denied"}
```

```json
{"status":"expired"}
```

Both are `200`. The TV starts the request again. It does not show an error code.

The same `install_id` that was already approved, posting again from home (a wiped TV with a new key), is approved on this call. The new `public_key` replaces the old one. Room, profile, name, and favorites stay. The body is the approved object above, with no `pair_id`.

Phones hear one SSE event, and one push when push is configured. Repeating the post does not send another.

```json
{"type":"tv_pair","data":{"pair_id":"pair_ab12cd34ef567890abcd","install_id":"tv_office_0123456789","name":"Living room TV","model":"onn 4K","suggested_room":"living_room","status":"pending"}}
```

`GET /v1/tv/pair` with an admin bearer:

```json
{"pending":[{"pair_id":"pair_ab12cd34ef567890abcd","install_id":"tv_office_0123456789","name":"Living room TV","model":"onn 4K","suggested_room":"living_room","suggested_room_name":"Living Room","expires_at":"2026-11-14T01:15:00+00:00"}]}
```

`POST /v1/tv/pair/{pair_id}/approve` `{room?, profile?, name?}` and `POST /v1/tv/pair/{pair_id}/deny` are admin. Approve answers `{"ok":true,"status":"approved"}`. Deny answers `{"ok":true,"status":"denied"}`. A second tap on an expired request answers `{"ok":false,"status":"expired"}` and does not raise. `profile` is `adult` or `kids`. `room` is an area id. The signed `POST /v1/tv/enroll` path is unchanged.

### Device management

The hub keeps its own picture of every Thomas Creek TV. The TV reports what it can do and how it is feeling. An admin asks it to do something over the same event stream the TV already holds open. Play and stop stay on `tv_command`. These commands are `tv_manage`. A phone does not receive them.

The TV reports one capability, the strongest it actually has:

| capability | What it means |
|---|---|
| `basic` | The app is installed. The person confirms installs. No device-owner and no root. |
| `owner` | The app is Android device owner (`DevicePolicyManager`). |
| `root` | The app can run a root shell. This does not imply device owner. |

`platform` is `google_tv`, `android_tv`, or `unknown`.

`POST /v1/tv/fleet/report` with the TV bearer, on the home network. The TV posts every 15 minutes, and right away when the capability, the foreground app, the network kind, the package hash, the temperature crossing 80°C, or the remote battery crossing 15% changes. A field the box cannot read is `null` or left out. Both mean the same.

```json
{"capability":"owner","model":"onn. 4K Streaming Box","android":"12","platform":"google_tv","app_version":"1.5.0",
 "cpu_pct":12,"ram":{"used_mb":800,"total_mb":2048},"storage":{"used_mb":3200,"total_mb":8000},"temperature_c":48,
 "network":{"kind":"wifi","ssid":"House","rssi_dbm":-55},"ping_ms":4,
 "foreground":"me.dlewis.thomascreek.tv","uptime_s":86400,"remote_battery_pct":80,
 "packages":{"hash":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","changed":false}}
```

`network.kind` is `wifi`, `ethernet`, or `none`. `ssid` and `rssi_dbm` are null on ethernet. `packages.hash` is the lowercase hex SHA-256 of the sorted package names, one per line, with no extra blank line. `changed: true` includes `"list":["com.example.app"]`. The hub stores at most one sample a minute, and always stores a sample that changes the package hash or crosses a temperature or battery line. A faster repeat still gets the answer below.

```json
{"ok":true,"need_packages":false,
 "kill_list":["com.adups.fota","com.adups.fota.sysoper","com.fw.upgrade.sysoper","com.swe.dgbluancher"],
 "policy":{"blocked":[],"restrict_settings":false,"hide_status_bar":false,"disable_keyguard":false},
 "commands":[]}
```

`need_packages` is true when the hash is new and `list` was left out. The TV posts the list next, with `changed: true`. `kill_list` and `policy` are what the TV applies on this check-in, even if it missed an event. `commands` are the same objects as `tv_manage`, still waiting for an ack.

`capability` must be `basic`, `owner`, or `root`. Anything else is `400` "The TV didn't say what it can do." A phone calling this route is `403` `not_a_tv`.

History stays 30 days (`tv_fleet.retain_days`). Samples from the last 48 hours are kept as stored. Older ones fold to the last value in each hour. Older than the retention window is dropped.

Commands travel on `GET /v1/events` as `tv_manage`, and a TV that is not holding the stream open can long-poll `GET /v1/tv/manage?timeout=20` (max 25, `0` returns what is waiting). A phone gets `403` `not_a_tv`. The TV acks with `POST /v1/tv/manage/ack`. Pending commands wait until the ack or two minutes, same as play commands.

```json
{"type":"tv_manage","data":{"id":"tvm_ab12cd34ef567890","install_id":"tv_office_0123456789","action":"reboot","params":{},"at":"2026-11-14T18:00:00+00:00"}}
```

```json
{"id":"tvm_ab12cd34ef567890","ok":true,"detail":null}
```

A command the box cannot do is still `200`, with `"ok":false` and a `detail` sentence. The TV does not show a stack trace or an error code. An ack for a command that already finished:

```json
{"ok":false,"message":"That command has already finished."}
```

| action | Who runs it | params | When this box can't |
|---|---|---|---|
| `reboot` | owner, root | `{}` | "This TV can't restart itself." |
| `restart_app` | all | `{}` | Restarts `me.dlewis.thomascreek.tv`. |
| `install_apk` | all | `{url, sha256, version_code, version_name}` | Owner and root install with no prompt. Basic hands the file to PackageInstaller and the person confirms. `url` is a hub path (`/v1/tv/apk` or `/v1/tv/{version_code}`). The TV downloads it with its bearer and checks `sha256`. A mismatch is not installed: "That build didn't check out." |
| `uninstall` | owner, root | `{package}` | Owner uses `DevicePolicyManager` uninstall. Root runs `pm uninstall --user 0`. Basic: "This TV can't remove apps." |
| `disable` | owner, root | `{package}` | Owner hides it (`setApplicationHidden`). Root runs `pm disable-user --user 0`. Basic uses the same sentence as uninstall. |
| `set_kiosk` | owner | `{on:true}` or `{on:false}` | Lock task on the launcher activity the hub already has (`tv_app.home_activity`). Off leaves lock task. Basic, and a root box that cannot lock task: "This TV can't lock the screen to the launcher." |
| `apply_policy` | owner, root | the policy object | Basic: "This TV can't apply that policy." Root applies what the shell can, and says so in `detail` when a switch was skipped. |
| `kill_list` | owner, root | `{packages:[…]}` | Every check-in. Root force-stops each one (`am force-stop`). Owner hides each one. Basic acks `ok: true` and does nothing. Packages that are installed come back as `hits`. |
| `screenshot` | all | `{}` | Root takes a silent `screencap`. Owner and basic ask on screen, once: "Share this screen with the house?" If the person declines: "The screen stayed private." The TV does not ask again for that command. |
| `show_message` | all | `{style, text}` | `style` is `fullscreen` or `toast`. `text` is plain, at most 240 characters. Fullscreen has a way to dismiss it. Toast goes away on its own. |
| `tv_power` | best effort | `{on:true}` or `{on:false}` | HDMI-CEC or a key when the box has one. Otherwise: "This TV can't change the power." |
| `tv_volume` | best effort | `{level:0}` to `{level:100}`, or `{delta:-1}` or `{delta:1}` | Otherwise: "This TV can't change the volume." Neither field: "Say how loud." |

The hub will not send `uninstall` or `disable` for `me.dlewis.thomascreek.tv`. A kill-list ack that found something:

```json
{"id":"tvm_ab12cd34ef567890","ok":true,"detail":null,"hits":["com.adups.fota"]}
```

After a root screenshot, or after the person accepts one, the TV posts the PNG to `POST /v1/tv/fleet/screenshot?command=tvm_ab12cd34ef567890` with its bearer (`image/png`, at most 4 MB). `GET /v1/tv/fleet/{id}/screenshot` (`systems`) returns that image. A missing one is `404` "That picture isn't here."

`POST /v1/tv/fleet/{id}/command` (`systems`) `{action, params}` answers `{"ok":true,"id":"tvm_ab12cd34ef567890"}` as soon as the command is waiting. The page does not wait for the ack. An unknown action is `400` "That isn't a command this TV knows." An unknown TV is `404` "That TV isn't in the house."

Remote shell is root only, admin only, and home network only. It is not free-form unless the flag is on. The flag defaults off:

```yaml
tv_fleet:
  shell: false
  retain_days: 30
  offline_after_min: 30
  hot_c: 80
  low_battery_pct: 15
```

`POST /v1/tv/fleet/{id}/shell` `{template}` queues a `shell` command. Away from home the answer is the usual `403` "The TV app talks to the hub only on the home network." A TV that is not root acks "This TV has no shell." Templates, and the only string the TV runs for each:

| template | command |
|---|---|
| `logcat` | `logcat -d -t 200 -s ThomasCreekTV:V AndroidRuntime:E` |
| `packages` | `pm list packages` |
| `storage` | `df -h /data` |
| `uptime` | `uptime` |
| `network` | `ip -4 addr` |
| `battery` | `dumpsys battery` |

The ack carries `output` (at most 16 KiB) and `truncated` when the TV cut it. An unknown template is `400` "That isn't one of the shell commands." Free-form is `{command:"…"}`, one line, at most 200 characters. With `tv_fleet.shell` false the hub answers `{"ok":false,"message":"Shell commands are off."}` and sends nothing. Every shell request is stored: who, when, which TV, the template or the line, and whether it worked. `GET /v1/tv/fleet/audit?limit=50`:

```json
{"items":[{"at":"2026-11-14T18:00:00+00:00","who":"Dylan","id":"tv_office_0123456789","action":"shell","template":"logcat","command":null,"ok":true}]}
```

The output is not in that list and is not pushed. `GET /v1/tv/fleet/{id}/commands/tvm_ab12cd34ef567890` (`systems`) is the command plus `output` when it has one.

`GET /v1/tv/fleet` (`systems`):

```json
{"devices":[{
  "id":"tv_office_0123456789","name":"Living room","room":"living_room","online":true,
  "capability":"owner","model":"onn. 4K Streaming Box","android":"12","platform":"google_tv","app_version":"1.5.0",
  "health":"ok","last_seen":"2026-11-14T18:00:00+00:00",
  "cpu_pct":12,"ram_pct":39,"storage_pct":40,"temperature_c":48,
  "network":{"kind":"wifi","ssid":"House","rssi_dbm":-55},"ping_ms":4,
  "foreground":"me.dlewis.thomascreek.tv","uptime_s":86400,"remote_battery_pct":80
}]}
```

`online` is the same two-minute rule as `GET /v1/tv/players` (a heartbeat, a token, an enroll, or a fleet report). `health` is `offline` when nothing has been heard for `offline_after_min` (30), else `hot` at `hot_c` (80) or above, else `battery` at `low_battery_pct` (15) or below, else `storage` at 90% or above, else `ok`. A null temperature or battery does not raise `hot` or `battery`. An empty house is `{"devices":[]}`.

`GET /v1/tv/fleet/{id}` is that object plus `packages_hash`, `packages` (the last full list), `policy`, and `last_command` (`{id, action, ok, detail, at}` or null). `GET /v1/tv/fleet/{id}/history?metric=cpu&hours=24` is the chart. `metric` is `cpu`, `ram`, `storage`, `temperature`, `ping`, or `rssi`. `ram` and `storage` points are percents. `hours` is 1 to 720, default 24.

```json
{"metric":"cpu","points":[{"t":"2026-11-14T18:00:00+00:00","v":12}]}
```

An unknown metric is `400` "That chart isn't one the hub keeps."

`GET /v1/tv/fleet/kill-list` and `PUT /v1/tv/fleet/kill-list` `{packages:[…]}`. The first list, before anyone saves, is the starter set in [fleet-kill-list.md](fleet-kill-list.md). A save replaces it, including a save of `[]`. The starter set is not put back. A name that is not a package is `400` "That doesn't look like an app." `me.dlewis.thomascreek.tv` cannot be added. Saving sends `kill_list` to every owner and root TV. Basic TVs are skipped.

`GET /v1/tv/fleet/policy` and `PUT /v1/tv/fleet/policy` with the policy object. Defaults are nothing blocked, settings open, status bar shown, keyguard left as the box has it (`hide_status_bar` false, `disable_keyguard` false). Saving sends `apply_policy` to every owner and root TV.

`POST /v1/tv/fleet/rollout` uses a build the hub already stored (`version_code` from the Downloads page). `stage` is the install ids to offer first. `then` is `all` or omitted. `now` defaults false.

```json
{"version_code":602,"stage":["tv_office_0123456789"],"then":"all","now":false}
```

While `state` is `staging`, `GET /v1/tv/update` offers that version only to the staged TVs. Everyone else keeps the previous manifest, including `up_to_date: true` when they are already on it. Each staged TV reports on the existing `POST /v1/tv/update/health`. When every staged TV is `ok`, `state` becomes `rolling` and the rest are offered the build, still inside the silent window (`idle_only`, 01:00 to 06:00). `now: true` also queues `install_apk` immediately for whoever is being offered it (silent on owner and root, a prompt on basic). A staged report with `rollback: true` or `playback_ok: false` sets `state` to `stopped`. The rest are not offered the build. The file stays on disk. `done` means everyone who was included has reported ok. With no rollout, or once it is `done`, the newest build is offered to every TV as it is today.

```json
{"rollout":{"version_code":602,"state":"staging","now":false,
 "stage":[{"id":"tv_office_0123456789","name":"Living room","status":"waiting"}],"rest":"held"}}
```

`status` is `waiting`, `ok`, or `stopped`. No rollout is `{"rollout":null}`. An unknown build is `404` "That build isn't on the hub."

Alerts are one notice to admins (`systems`, level `fyi`), and not to the household. One per TV per kind, until that kind clears, then it can be said again. Kinds: offline ("Living room has been quiet for a while."), hot ("Living room is running hot."), battery ("Living room's remote is low."), kill ("Living room still has an app from the block list."). The name is the TV's name.

The hub web page has Devices at `/admin/devices`, linked from the nav, with the same sign-in as `/admin`. The list shows online, health, capability, and app version. A TV opens charts for the last day (CPU, memory, storage, temperature, ping). The buttons are the commands above. Rollout picks a stored APK, the first rooms, then the rest. The kill list and the policy are edited there. A root TV shows the shell templates. The free-form line is on the page only when `tv_fleet.shell` is true. No TVs yet: "No TVs have checked in yet."

`/admin/devices/provision` walks a factory-reset box into device owner. The app is installed first from the same `/tv` link as Downloads. Then one command:

```
adb shell dpm set-device-owner me.dlewis.thomascreek.tv/.DeviceAdminReceiver
```

That component is `me.dlewis.thomascreek.tv.DeviceAdminReceiver`. The page also shows a short script that installs the APK and runs that command. Nothing in it comes from a third party. The next fleet report should say `capability: owner`.

Where the setup wizard supports QR provisioning, the page shows a QR of this JSON. The download location is the `/tv` link on the origin the browser is using. The checksum is the URL-safe base64 of the raw SHA-256 of that APK, which is what Android expects. Wi-Fi fields are typed on the page for the QR only and are not saved.

```json
{"android.app.extra.PROVISIONING_DEVICE_ADMIN_COMPONENT_NAME":"me.dlewis.thomascreek.tv/me.dlewis.thomascreek.tv.DeviceAdminReceiver",
 "android.app.extra.PROVISIONING_DEVICE_ADMIN_PACKAGE_DOWNLOAD_LOCATION":"http://hub.example/tv",
 "android.app.extra.PROVISIONING_DEVICE_ADMIN_PACKAGE_CHECKSUM":"<url-safe base64 of the raw sha256>",
 "android.app.extra.PROVISIONING_SKIP_ENCRYPTION":true,
 "android.app.extra.PROVISIONING_LEAVE_ALL_SYSTEM_APPS_ENABLED":true}
```

Google TV and Android TV do not behave the same. Device owner can be set on a factory-reset box before any account is added and before setup marks the device provisioned. Certified Google TV (Chromecast with Google TV, and many Sony and TCL sets) often has no QR screen and rejects `dpm set-device-owner` once the wizard has finished. The wizard usually wants a Google account and a network first. If debugging cannot be turned on, the page says the box will stay a normal app (`basic`). Android TV is more likely to offer the QR. A box that already has a device owner needs a factory reset before this one can be set. Root is a separate report. Kiosk lock-task is device owner only.

These routes are the ones an iPhone admin screen can call later. The page and the JSON are the same.

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

## TVs on the phone (`hub/tvs.py`, `routes/tvs.py`)

Admins and adults (`remotes.setup`). One shape for the iPhone app and the assistant, built from the TV registry, the fleet check-ins and now-playing.

`GET /v1/tvs` → `{tvs:[TV], latest_version}`. Approved TVs first, then pending ones, by name. Revoked TVs are left out.

```json
{"id":"tv_office_0123456789","name":"Living room","room":"living_room","profile":"adult","status":"approved",
 "online":true,"last_seen":"2026-11-15T04:30:00+00:00","health":"ok","app_version":"1.4.1-tc.6","update_available":false,
 "foreground":true,"now_playing":{"kind":"live","title":"Newsroom","name":"CNN","channel_key":"xc:thomas-creek:10"},
 "last_error":null,"model":"SHIELD Android TV","platform":"android_tv","capability":"basic","ip":"10.0.1.21",
 "screenshot":{"url":"/v1/tvs/tv_office_0123456789/screenshot?t=1794716000","at":"2026-11-15T04:33:20+00:00"},
 "settings_rev":3}
```

`online` means heard from in the last two minutes (fleet report, state, token or config pull). An offline TV has `now_playing: null` and `health: "offline"`; otherwise `health` is the fleet value (`ok`, `hot`, `battery`, `storage`). `screenshot` is null until the TV has sent one.

`GET /v1/tvs/{id}` is the same plus `device` (`cpu_pct`, `ram_pct`, `storage_pct`, `temperature_c`, `network`, `ping_ms`, `uptime_s`, `remote_battery_pct`, `android`; null before the first fleet report), `last_command`, `settings` and `actions`.

`PATCH /v1/tvs/{id}/settings` `{name?, room?, profile?, settings?}`. `settings` holds only the keys to change; `null` removes one so the TV uses its own default. The answer is `{ok, settings, settings_rev}`. Keys:

| Key | Value |
|---|---|
| `home_rows` | row ids in order, at most 30 (`continue`, `live_now`, …) |
| `favorites` | channel keys, at most 500, duplicates dropped |
| `hidden_categories` | category names, at most 500 |
| `parental` | `{locked: bool, max_rating: "PG" \| "TV-Y7" \| … \| null}`. A PIN stays on the TV. |
| `language` | `en`, `en-US`, … |
| `player` | `{autoplay_next: bool, resume: bool, subtitles: "off" \| "auto" \| lang, audio_language: lang \| null}` |

`picture`, `audio`, `display` and `sound` are refused with `400` "Picture and audio stay on each TV." An unknown key is `400` "`<key>` isn't a TV setting the hub keeps."

Every change bumps the config version once and sends `config_changed` to that TV. The TV then makes its signed `POST /v1/tv/config`, whose body now has `settings` and `settings_rev`. A change made on the TV comes back with `POST /v1/tv/settings` `{settings, base_rev?}` (TV bearer); the last write wins, from either side, and the answer is what the hub holds.

`config_changed` (TV event stream only, never to phones): `{"install_id":"tv_…" | null, "version": 42, "settings_rev": 3}`. `install_id` null means a house-wide change (kids categories, lineup, sources); every TV pulls, and an unchanged config answers `304`.

`POST /v1/tvs/{id}/actions` `{action, …}` → `{ok, via, message, id?}`. `via` is `app` (sent to Thomas Creek TV over its event stream; `id` is the fleet command), `adb` (done by the hub over network debugging) or `none`.

| Action | What happens |
|---|---|
| `restart_app` | Online: the app restarts itself. Offline: the hub force-stops and opens it over adb. |
| `open_app` | Opens the app over adb. Without adb, an online app restarts into the front. |
| `update` | The newest kept build. Over adb for a box on the TV app list, else sent to the app (`install_apk` with `/v1/tv/apk`); the TV may show an Install prompt. Already current: `via: "none"`. |
| `screenshot` | The hub takes it over network debugging (`adb exec-out screencap -p`) and answers `via: "adb"` with the picture already saved; `needs_allow` until the hub's key is allowed. A rooted box without adb sends its own (`via: "app"`; poll for it). `can_screenshot` on the TV says whether either way exists; the phone hides the button otherwise. The rest of this row: Poll `GET /v1/tvs/{id}` until `screenshot.at` changes, then load `screenshot.url`. |
| `play` | `{channel_key}`, `{movie_id}` or `{episode_id, series_id?}`, plus `title?`. Same delivery as `tv.play`. |

A TV that needs the hub's one-time adb Allow answers `409` `needs_allow` "Living room needs one Allow for the hub: press Allow on the TV, then try again." Offline TVs answer `409` `offline` for actions that need the app.

`GET /v1/tvs/{id}/screenshot` returns the last PNG (`404` before the first one).
