# What the iOS app expects from the hub

Builds on the hub thread's auth proposal (Bonjour `_thomascreek._tcp`, `/v1/enroll`, challenge/token, `X-TC-Confirm`, `/v1/me`, `/v1/home`, `/v1/events`, `/v1/actions`). The app is built against these shapes; if the hub changes one, update `server/docs/API.md` and tell the app thread.

All JSON is snake_case. Times are ISO 8601 strings unless noted.

## Auth additions
- Enroll body also carries `secure_public_key` (biometry-bound SE key) and `app_version`.
- Bonjour TXT: `api=v1`, `id=<hub id>`.
- `GET /v1/me` -> `{person:{id,name,role,avatar?}, role:"admin|adult|child|guest|pending", capabilities:[string], device:{id,name,status}}`.
  Capability strings the app checks: `lights.all`, `lights.own_room`, `climate.all`, `climate.own_room`, `doors`, `cameras`, `media.all`, `media.own_tv`, `media.request`, `media.approve`, `pipeline`, `holiday.full`, `holiday.looks`, `holiday.pick`, `family.edit`, `family.view`, `shopping.add`, `chores.manage`, `chores.do`, `inventory.edit`, `inventory.search`, `appliances`, `network.manage`, `network.ask`, `systems`, `people`, `activity`.

## VPN
- `POST /v1/vpn/config {wg_public_key}` (app generates the key pair; private key never leaves the phone).
- Reply: `{interface:{addresses:[..], dns:[..]}, peer:{public_key, endpoint, allowed_ips:[..], persistent_keepalive}, home_ssids:[..]}`. App sets on-demand: connect when not on a home SSID.

## Snapshot `GET /v1/home`
```
{ generated_at,
  status: {level:"ok|attention|offline", line},
  people: [{id,name,role,home?,avatar?}],
  modes: [{id,name,symbol,active}],
  floors: [{id,name,room_ids:[..]}],
  rooms: [{id,name,floor_id,symbol,summary,favorite,device_ids:[..]}],
  devices: [{id,kind,name,room_id,state:{..},updated_at,available,actions:[..]}],
  cameras: [{id,name,room_id,snapshot_url,updated_at,event?}],
  now_playing: [{device_id,room_id,title,subtitle,art_url,state}],
  today: {events:[{title,start,end,who}], dinner?:{title,image_url}},
  systems?: {storage:[{name,used_pct,size_tb,scrub_pct?}], apps:{total,crashed,updates}, internet:{up,latency_ms}},
  seasonal: {holiday_lights:bool} }
```
`kind`: light|fan|switch|climate|lock|garage|media|speaker|camera|sensor|vacuum|oven|printer|valve|shower.
Common `state` keys: `on`, `brightness` (0-100), `color` (hex), `current_temp`, `target_temp`, `hvac` (heat|cool|off|auto), `hvac_action`, `locked`, `open`, `position`, `volume`, `source`, `progress` (0-1), `remaining_s`, `value`, `unit`.
SSE `/v1/events`: `{type:"device|status|inbox|snapshot", data:{..}}` (device = full device object).

## Actions `POST /v1/actions {action, target, params}`
-> `{ok, message}`. `428 {error:"confirm_required", nonce}` when the secure header is missing. `403 {error:"forbidden", ask_parent:bool}`.
Action names the app sends: `light.set {on,brightness,color}`, `fan.set {on,speed}`, `switch.set {on}`, `climate.set {target_temp,hvac}`, `lock.set {locked}`, `garage.set {open}`, `media.command {command: power|play|pause|next|prev|up|down|left|right|select|back|home|vol_up|vol_down|mute}`, `media.volume {volume}`, `speaker.group {members}`, `mode.run`, `scene.run`, `valve.run {minutes}`, `vacuum.start {rooms}`, `shower.preheat`, `announce {text, rooms}`.

## Holiday lights
Role-checked passthrough `GET|POST /v1/lights/{path}` to the show player, hub adds APP_TOKEN: `status`, `start`, `stop`, `next`, `play?scene=`, `queue?scene=`, `unqueue?i=`, `look?name=`, `brightness?bri=`, `schedule?enabled=&skip=`, `api/live`, `api/notifications`.
Web pages `/v1/lights/house` and `/v1/lights/wled`: accept `?access_token=` once and set a cookie for the page's own requests.

## Push and inbox
- `POST /v1/devices/push {apns_token, env:"sandbox|production", prefs?:{cat:bool}}` -> `{prefs, categories:[{id,title,detail,default}]}`
- `POST /v1/devices/prefs {prefs}`
- `POST /v1/devices/activity {start_token?, activity_id?, token?}` (lights Live Activity; ContentState unchanged from show_player `live_state()`)
- `GET /v1/inbox` -> `{items:[{id,t,level:"act_now|decision|maintenance|fyi",cat,title,body,actions?:[{id,title,destructive?}],ref?}]}`
- `POST /v1/inbox/{id}/ack`, `POST /v1/inbox/{id}/act {action}`
- APNs payload custom keys: `cat`, `inbox_id`, `ref` (deep link like `thomascreek://room/kitchen`).

## Everything else
- `POST /v1/cameras/{id}/pass` -> `{webrtc_url, hls_url?, expires_at}`; `GET /v1/cameras/{id}/events` -> `{items:[{id,label,start,thumb_url,clip_url}]}`
- `GET /v1/media/continue`, `GET /v1/media/search?q=` -> `{items:[{id,title,year,kind,poster_url,rating,summary}]}`; `POST /v1/media/play {item_id, target_device_id}`; kids' built-in player: `GET /v1/media/{id}/stream` -> `{hls_url, expires_at}`
- `GET /v1/requests` -> `{items:[{id,title,poster_url,who,status:"waiting|approved|denied|downloading|ready",progress?,created_at}]}`; `POST /v1/requests {media_id|kind,text}`; `POST /v1/requests/{id}/approve|deny`
- `GET /v1/pipeline` (admin) -> `{downloads:[{title,progress,eta_s,state}], failures:[..]}`
- `GET /v1/chores?person=` -> `{person, points, streak, goal, items:[{id,title,icon,done,points}], rewards:[{id,title,icon,cost}]}`; `POST /v1/chores/{id}/done`; `POST /v1/rewards/{id}/redeem`
- `GET /v1/calendar?days=7`, `GET /v1/meals/today`, `GET /v1/meals/week`, `GET /v1/shopping` -> `{lists:[{id,name,items:[{id,text,done}]}]}`, `POST /v1/shopping/{list}/items {text}`
- `GET /v1/inventory/search?q=` (Homebox)
- `GET /v1/systems` (admin), `GET /v1/activity?before=`, `GET /v1/people`, `PATCH /v1/people/{id}`, `POST /v1/devices/{id}/assign {person_id}`, `POST /v1/devices/{id}/revoke`
- `GET /v1/assets/house-model` (the 3D house JSON, not in the public repo) and `GET /v1/assets/header` (house photo, same reason)

## Additions (app thread, after first pass)
- `person.ui_style`: `"standard" | "kid" | "little"`. Children get `kid` (9-year-old, words + pictures) or `little` (5-year-old, picture-first). Guests are role `guest` with `person.access_until` and `person.area` (e.g. `apartment`).
- `GET /v1/me` also returns `remote_url?` (the hub published through the reverse proxy, if enabled) and `guest?: {wifi_ssid, wifi_password, guidance:[{icon,title,detail}], host_name}` for guests.
- Child extras in `/v1/me`: `child?: {room_id, tv_device_id?, internet:{paused, until?, reason?}}`.
- Camera pass: `POST /v1/cameras/{id}/pass` -> `{player_url, expires_at}` where `player_url` is a go2rtc-style WebRTC page the app loads in a web view (bearer-free, short-lived). `hls_url` optional.
- House model: `GET /v1/assets/house-model` returns exactly the mockups' `house-model-web.json` format (`{mats:{name:"#hex"}, groups:[{k,m,p:[xyz..],i:[..]}], lines:[[x1,y1,z1,x2,y2,z2]..]}`); the app renders it natively with SceneKit. Please send `ETag` so the app can cache it.
- `POST /v1/requests` body for kids asking for things: `{kind:"media|internet|reward|other", media_id?, text, minutes?}`.

## Exact details the app now relies on (app thread, end of first build)
Auth
- Login: the device key signs the UTF-8 bytes of the `nonce` string (ES256, DER, base64).
- Secure confirm: header `X-TC-Confirm: <nonce>:<base64 DER sig>`, where the Face ID key signs the UTF-8 string `"<nonce>.<sha256 hex of the raw request body>"` (lowercase hex; empty body hashes the empty string). The nonce comes from the 428 reply's `nonce`, or from `POST /v1/auth/challenge {device_id, purpose:"confirm"}` (sent with the bearer).
- `403 {error:"revoked"}` makes the app forget its keys and enroll again on home Wi-Fi.
- Enroll body: `device_name, model ("iPhone16,2"), platform ("ios"|"ipados"), os_version, app_version, public_key, secure_public_key?, apns_token?, apns_env?`. Reply `{device_id, status:"pending"|"active"}`.

Snapshot extras
- `status.items?: [{id, level:"ok|attention|critical", title, detail?, device_id?, action?, action_title?, ref?}]` (e.g. garage open 40 min with action `garage.set` and title "Close"). `ref` is a deep link like `thomascreek://doors`.
- `weather?: {temp, condition, symbol}`; `now_playing[].progress` (0-1) and `.volume` (0-100); `person.color` ("#hex"); `systems.apps.crashed_names`, `systems.internet.down_mbps`, `systems.backups {ok, last}`.
- Device `state` extras the UI reads: `speed` (fan), `source`, `title`, `artist`, `apps` (media: list of app names), `state` ("playing"/"idle"/"printing"...), `remaining_s`, `hvac_action` ("heating"/"cooling"/"idle").
- SSE types: `device`, `status`, `now_playing` (full list), `inbox`, `me`, `snapshot`.

Holiday lights: commands are sent as POST to `/v1/lights/<cmd>?<query>`, `status` and `api/live` as GET.

More routes
- `GET /v1/calendar?days=7` -> `{events:[{id?,title,start,end?,who?,all_day?}]}`
- `GET /v1/meals/week` -> `{days:[{date:"YYYY-MM-DD",title?,emoji?,image_url?,recipe_id?}]}`; `GET /v1/meals/recipes/{id}` -> `{id,title,image_url?,total_time?,servings?,ingredients:[..],steps:[..]}`; `today.dinner` may carry `emoji` and `recipe_id`.
- `PATCH /v1/shopping/{list}/items/{id} {done}`
- `GET /v1/chores` (the caller's board), `GET /v1/chores?person=all` (household totals as one board), `GET /v1/chores/all` -> `{boards:[ChoreBoard]}`
- `GET /v1/media/library?rated=1` (kids' rated library) -> `{items}`; `GET /v1/requests?mine=1`
- `GET /v1/network/kids` -> `{kids:[{id,name,paused,until?,devices?}]}`; actions `network.pause` and `network.grant {minutes?}` (Secure tier)
- `POST /v1/push/test`
- `GET /v1/activity?category=people|automation|doors&limit=&before=` -> `{items:[{id,t,who?,device?,what,why?}], before?}`
- `GET /v1/systems` -> `{pools:[{name,used_pct,size_tb?,scrub_pct?}], apps:[{name,state,update?}]}`
- `GET /v1/people` -> `{people:[Person], devices:[{id,name,model?,person_id?,status,last_seen?}]}`; assign and revoke are confirmed with Face ID.
- Push categories (`/v1/devices/push` reply): `[{id,title,detail,default,group?}]`. APNs `category` values the app handles: SHOW, HEALTH, CONTROL, SCENE, RENDERS, MANAGER, WEATHER, SUMMARY (lights), APPROVAL (Approve / Not now buttons, sends `/v1/inbox/{inbox_id}/act`), SECURE and DEVICE (open the app).

## Matched to the hub's server/docs/API.md (first PR)
- `X-TC-Confirm: <nonce>.<base64 sig>` (dot, not colon). TLS on 8443 with a self-signed P-256 cert; the app pins the SPKI SHA-256 from Bonjour TXT `pin`.
- Lights commands go as GET (show_player.py's commands are GET only); `dry_run` replies show "Practice mode".
- `POST /v1/media/play {item_id, target_device_id}`; images and clips are hub paths fetched with the bearer; `player_url` is hub-relative without bearer.
- Inbox `assign` opens People; push categories `NEW_DEVICE` and `REQUEST` are registered.
- Activity items `{id,t,who,action,target,params,outcome,detail,source}`, paging `next_before`; people `{people:[{...,devices}], unassigned}`; meals `{items:[{date,meal,title,slug,image_url}]}`.

## Matched to the hub's second round
- Chores `{person, date, points, streak, goal, done, items:[{...,waiting,locked,time,word}], rewards:[{...,available}]}`; adults use `/v1/chores/all`.
- `/v1/systems` pools and apps rows (TrueNAS states are upper case); `used_pct` may be null.
- `/v1/network/kids` devices are objects and `paused` may be null. Dylan links devices from `/v1/network/devices` with `POST /v1/network/kids/{id}/devices {macs}` (Face ID) and unlinks with `DELETE`.
- Calendar events carry `location`.

## Cameras (Frigate only) and TV remote, app build 14

- `GET /v1/cameras` -> `{cameras:[{id,name,width,height,online,snapshot_url,last_event?:{label,start}}]}`. Width/height are the detect frame size; the app shows cameras wider than 2.1:1 on their own full-width row.
- `GET /v1/cameras/events?limit=&before=&camera=&label=` -> `{items:[{id,camera,camera_name,label,sub_label,score,zones,start,end,thumb_url,snapshot_url,clip_url}], next_before}`, newest first. `end` null means still happening.
- `GET /v1/cameras/events/{id}` -> one event in the same shape (opened from a push or a link).
- SSE `camera_event` with one event in the same shape; the app prepends it to Home and the Cameras feed.
- Home shows recent events, not live tiles. Live view only on the camera screen via the pass.
- TV remote: media device state may carry `commands` (only these buttons are shown), `sources` and `source`, `volume` 0-100 and `muted`. New `media.command` params: `{command:"source", source}` and `{command:"volume", volume}`.
- Activity entries decode leniently: `id` string or number, `t` epoch seconds or ISO (`at` as fallback).
- `GET /v1/pipeline` -> `{requests, downloads, recent, upcoming, failures:[{source,error}], services}`; the app shows Problems, Requests, Downloading, Coming up, Recently added. Download `progress` may be 0-1 or 0-100.

## Trips (TREK)

- `GET /v1/trips` (`trips.view`) gives `{trips, next, plan_url}`; `GET /v1/trips/{id}` gives one trip with `days`, `accommodations`, `travellers`, `unplanned_places`, `unscheduled_reservations` and `today` while it's on. Full shapes in server/docs/API.md.
- Screens: Family → Trips tile → `TripsView` → `TripView` (Plan, Bookings, Stay, Ideas) → `TripDayView` (map and stops). Home shows `NextTripCard` once a trip is 3 weeks out (countdown only on the tablet). Kids' Home shows `KidTripCard`; the 5-year-old gets sleeps to count.
- Children get no booking or stay notes from the hub; `plan_url` ("Plan in TREK") is only sent to people with `trips.plan`.
- Deep links: `thomascreek://trips`, `thomascreek://trip/<id>`.
- Trip days also come back from `/v1/calendar` as all-day events, so the Calendar screen needs no change.

## Kids' YouTube (yt-dlp on the hub)

App only, not a Plex library (Dylan, 2026-11-12). No approval step; parents get an FYI inbox note.

- `GET /v1/youtube` → `{sources:[Source], videos:[Video], storage?, blocked?}` (storage and blocked for admin/adult only).
  Source: `{id, kind: video|playlist|channel, title, channel, channel_id, url, follow, added_by, added_by_id, picture_url?, count, waiting, bytes, last_error?}`.
  Video: a MediaItem (`id: "yt-<videoId>"`, `kind: "youtube"`, `poster_url: /v1/youtube/picture/<videoId>`, `summary`/`show` = channel, `duration_s`) plus `{video_id, channel, channel_id, status: queued|fetching|ready|failed, progress?, bytes, height, error?, watched, source_ids}`.
- `GET /v1/youtube/preview?url=` → `{kind, title, channel, picture (YouTube https URL), followers, count, newest, followable}`; 400 `bad_link` with a plain message.
- `POST /v1/youtube {url, follow}` → Source. admin, adult, child. Same link again returns the same source.
- `POST /v1/youtube/sources/{id} {follow}`; `DELETE /v1/youtube/sources/{id}` (parents, or the child who added it).
- Parents only: `DELETE /v1/youtube/videos/{videoId}`, `POST /v1/youtube/block {channel_id, name}`, `DELETE /v1/youtube/block/{channel_id}`, `POST /v1/youtube/refresh`.
- `POST /v1/youtube/videos/{videoId}/retry` for a failed one.
- `GET /v1/youtube/picture/{videoId | src-<sourceId>}` → JPEG.
- Downloads: `GET/POST /v1/media/yt-<videoId>/download` work as for films. Options: `original` (instant, the hub's own MP4) and `small` (480p, converted by the hub). `POST /v1/media/yt-<id>/progress` marks it watched on the hub (used for trimming).

## App updates (2026-11-12)

- `GET /v1/app/update?build=<CFBundleVersion>&version=<short version>` (no sign-in needed; with a token the hub records the phone's build). Reply `{latest_build, latest_version, min_build, notes, testflight_url, update_available, update_required}`.
- `AppUpdate` (App/Core/AppUpdate.swift) checks on every open (at most every 5 min) and on the `app_update` event. Newer build: `UpdateBanner` above the tab bar (✕ hides it until the next build) and an Update row in Profile. Below `min_build`: `UpdateRequiredView` covers everything until updated. The last answer is cached, so a required update holds while away.
- Update opens `testflight_url` (`itms-beta://beta.itunes.apple.com/v1/app/<Apple ID>`), then `itms-beta://`, then TestFlight's App Store page.

## Kitchen: ovens, recipes, dinner plan (2026-11-12)
- Ovens are `kind: oven` (GE water_heater entities). Device state: on, mode, modes, current_temp, target_temp, remote_ready.
- `GET /v1/kitchen` (appliances): `{ovens:[{id,name,on,available,remote_ready,mode,modes,current_temp,target_temp,min_temp,max_temp,session?}], presets:[{id,title,detail,mode,temp}], dinner:{title,slug,image_url,oven}}`.
- `oven.set` params `{mode, temp, timer_min, timer_from: now|preheat|manual, auto_off, recipe}` is Secure (Face ID); `{on:false}` is Everyday. `oven.timer {minutes, auto_off?}` is Everyday. Errors 409 `remote_enable`, 409 `oven_offline`: show the message.
- Notices use cat `kitchen`, ref `thomascreek://kitchen/<entity>`, buttons `timer` and `off` through `/v1/inbox/{id}/act`.
- Recipe `oven`: `{mode, temp|null, minutes|null, quote, title, saved?}` on `GET /v1/meals/recipes/{slug}`; also `favorite`, `tags`, `source_url`, `description`.
- `GET /v1/meals/recipes?search=&tag=` → `{items:[{id,slug,title,image_url,total_time,tags,favorite,oven,last_made}]}`; `GET /v1/meals/tags`.
- `POST /v1/meals/recipes` `{url}` or `{title, ingredients[], steps[], servings, prep_time, cook_time, tags[], oven{mode,temp,minutes} | no_oven}` → `{slug, recipe}`. `PUT /v1/meals/recipes/{slug}` same fields (only what's sent changes). `DELETE` same path. `PUT .../image` raw JPEG/PNG body. `POST .../favorite {on}`, `POST .../shopping {list_id?}`, `POST .../made`.
- `POST /v1/meals/recipes/parse {text}` → draft `{title, ingredients, steps, oven}` (scanned or pasted text).
- `GET /v1/meals/plan?start=&days=7` → `{days:[{date, dinner:{id,slug,recipe_id,title,image_url,total_time,note}|null, other:[]}]}`. `PUT /v1/meals/plan/{yyyy-mm-dd} {slug}` or `{title}` ("Eating out"); `{}` clears. `POST /v1/meals/plan/fill {start?, days?}` fills empty nights. `GET /v1/meals/suggest?n=`.
- Editing needs `family.edit` (adults); viewing `family.view`.

## Search (2026-11-12)

`GET /v1/search?q=` (see server/docs/API.md "Search"). The app shows rooms, devices, cameras and people from the
last snapshot at once (LocalSearch), then the hub's groups; `pending` types are asked for once more. Rows run
`action` through `HomeStore.perform` (Face ID for secure), `batch` step by step, `media` opens Play on (kids: the
kids' player), `request` posts `/v1/requests`, `chore` posts `/v1/chores/{id}/done`; otherwise `deep_link`.
New deep links: search?q=, remote/<entity>, climate, music, calendar, shopping, inventory, pipeline, hubweb,
vpnpool, downloads. Spotlight indexes rooms, devices, cameras and screens (identifier = deep link). Siri:
"Search Thomas Creek". The "Ask Thomas Creek" row calls `Router.ask`, which opens the assistant with the words.
## Smart "Right now" cards

`GET /v1/home` now carries `weather` ({temp, condition, symbol, high, low, sunrise, sunset}) and `smart`, a list of
cards the hub picked for this person right now, highest priority first. `GET /v1/smart` returns just
`{cards, weather}`; the event stream sends `{"type":"smart"}` when they may have changed (and device events can
change them too), so the app refetches `/v1/smart`.

Card: `{id, rule, priority, tint (accent|warn|bad|kid|night|sun), symbol (SF Symbol), eyebrow, title, detail?,
items?: [{title, value?, url?}], image? (hub path), emoji?, ref? (deep link), why, style? ("picture" for the
picture-first layout), buttons: [...]}`.

Button kinds:
- `action`: `{action, target?, params, secure}` → `POST /v1/actions` (Face ID when `secure`). `steps.run` takes
  `{steps: [{action, target, params}], label}` and runs several taps in one (lights, switches, fans, locks,
  garage, media); each step passes the normal policy and the strongest tier any step needs applies.
- `open`: `{url}` deep link.
- `call`: `{method, path}` e.g. `POST /v1/cameras/events/{id}/wrong` ("Not a package").
- `done`: just closes the card.
`done` on any button is the toast text after it worked.

`POST /v1/smart/dismiss {id, rule, handled}`: handled=true after a button worked; false is "Not now", which hides
the card until 4 am (30 min for safety cards). Three "Not now" in a row on one rule mutes it for that person
(reply has `muted: true`, `rule_title`). `GET /v1/smart/settings` → `{muted, rules}`; `POST /v1/smart/settings
{rule, muted}`.

Hub config `smart:` dinner_time, dinner_lead_min, lockup_time, trash_night, bedtimes {name: "HH:MM"}, wake_time,
latitude/longitude (default: Home Assistant's), goodnight_mode, off_rules. Weather is Open-Meteo, falling back
to the first `weather.*` entity.
