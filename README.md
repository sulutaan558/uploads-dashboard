# Uploads dashboard

One page showing every TikTok -> YouTube channel: how many videos went out, whether any
slot was missed, what is queued for retry, and when the next upload fires -- in your own
timezone.

The page is public; the data is not. `docs/data.enc` is AES-GCM encrypted and the key
lives only in the URL fragment of the bookmark, which browsers never send to a server.

A new channel appears on its own: the sync job discovers any `tiktok-yt-automation-*`
repo by name.

Secrets this repo needs:

| secret | why |
|---|---|
| `CHANNELS_READ_TOKEN` | classic PAT with `repo` scope — the job reads other, private repos |
| `DASHBOARD_KEY` | base64 32-byte AES key; must stay stable or every bookmark breaks |
| `YOUTUBE_API_KEY` | optional; without it subscriber/view counts are simply hidden |
