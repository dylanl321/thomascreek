# Cloud media worker

Lighter streams for phones on a slow connection, made on the house's cloud server. The hub decides who may
watch what and leases the provider login; this worker only converts. See `server/hub/cloudmedia.py`.

## Run it (Coolify)

Deploy this folder as a Dockerfile application, with a persistent volume at `/data` and these settings:

| Setting | What |
|---|---|
| `HUB_URL` | The hub on the house's private network (`https://<hub address>:8443`) |
| `PUBLIC_URL` | This worker's own `https://` address, the one phones open |
| `LADDER` | Sizes to make, biggest first. Default `720,360` |
| `MAX_SESSIONS` | Streams at once. Default 6 (measured: an 8-core server makes about 12 at 720p) |
| `THREADS_PER_SESSION` | ffmpeg threads per stream. Default 2 |

No keys are set by hand. On first start the worker makes its own secret, asks the hub to join and prints
`JOIN CODE XXXXXX` in its log. Approve that code on the hub web page (Watching, Cloud media). The secret stays
in `/data`; the hub keeps only its hash. The hub's certificate is pinned on first contact.

## What it never does

It never logs a playback pass, a provider address or a login (ffmpeg's messages are scrubbed of addresses, and
the access log is off). It never opens a stream the hub didn't hand it for a phone that holds the lease.
