# Postiz setup — $0 local, no tunnel, no domain

**Plan changed for the better.** The named Cloudflare Tunnel is no longer needed.

Why: Meta only needs the OAuth redirect to be reachable by **the browser doing the
connecting** — which is this PC. Publishing afterwards is outbound (Postiz → Meta).
Meta permits `localhost` redirect URIs while an app is in **Development mode**, and
your app stays in Development mode anyway (that's why you skip App Review).

So: no tunnel, no domain, **and no DNS change to riseforit.com** — which was the
real risk, since your domain is on GoDaddy (`ns65/ns66.domaincontrol.com`) and
moving a live production domain's nameservers for a side utility is a bad trade.

Fallback if Meta ever refuses plain `http://localhost`: see the bottom of this file.

---

## PART A — your clicks (admin + one reboot)

WSL is **not installed** on this machine, and Docker Desktop needs it.

### A1. Install WSL2 — admin PowerShell
Right-click Start → **Terminal (Admin)** → run:
```
wsl --install --no-distribution
```
`--no-distribution` because Docker Desktop brings its own; no Ubuntu needed.

### A2. Reboot
Required. WSL2 isn't active until you do.

### A3. Install Docker Desktop
After the reboot, in a normal (non-admin) terminal:
```
winget install --id Docker.DockerDesktop --accept-source-agreements --accept-package-agreements
```
Then **launch Docker Desktop from the Start menu**:
- Accept the licence
- If it asks WSL2 vs Hyper-V → **WSL2**
- Wait until the whale icon stops animating (that means the engine is ready)

Optional but recommended: Docker Desktop → **Settings → General → Start Docker
Desktop when you sign in**. Otherwise Postiz won't come back after a reboot.

### A4. Tell me it's done
That's all the admin I need. Everything after this is staged and non-admin.

---

## PART B — already staged (no action from you)

- `D:\ContentMachine\postiz\docker-compose.yml` — app + Postgres 17 + Redis 7,
  bound to `127.0.0.1:5000` only
- `D:\ContentMachine\postiz\.env` — JWT secret and Postgres password already
  generated; Meta fields left blank for Part C
- `D:\ContentMachine\postiz\data\` — volumes on **D:** (C: has ~7 GB free)
- `D:\ContentMachine\postiz\start-postiz.ps1` — brings it up, waits for it to
  answer, prints the URL. `-Down`, `-Logs`, `-Pull` also supported.

Once Docker is running:
```
powershell -ExecutionPolicy Bypass -File D:\ContentMachine\postiz\start-postiz.ps1
```
First run pulls ~1–2 GB. Then open **http://localhost:5000** and create your local
account (it's your own machine — that account is just the login).

---

## PART C — Meta side, your thumbs

Do these in order; 1–2 must precede 3.

### C1. Instagram → Professional
1. Instagram app → profile → **☰** → **Settings and activity**
2. **Account type and tools** → **Switch to professional account**
3. Pick a category → choose **Business**
4. When offered, **connect your Facebook Page** — say yes

### C2. Facebook Page
No Page yet? facebook.com → **☰ Menu** → **Pages** → **Create new Page** → name +
category → **Create**.

### C3. Create the Meta app
1. **developers.facebook.com** → **My Apps** → **Create App**
2. App type **Other** → Next
3. Business type **Business** → Next
4. Name it (e.g. "Content Machine Poster") → **Create app**
5. **App settings → Basic** → copy **App ID** and **App Secret** (click Show)
6. Add any **Privacy Policy URL** and save

### C4. Add Instagram + permissions
1. **Add products** → add **Instagram**
2. **App roles → Roles** → confirm you're **Administrator**
3. **App review → Permissions and features** → request Advanced Access for:
   `instagram_basic`, `instagram_content_publish`, `instagram_manage_comments`,
   `instagram_manage_insights`, `pages_show_list`, `pages_read_engagement`,
   `business_management`

**Leave the app in Development mode.** Roles on the app (you) can use these
permissions without App Review. Full review is only for posting on behalf of
other people's accounts — i.e. the trainer-facing version, already gated behind
your first closed client.

### C5. Redirect URI — exact string
Meta app → **Facebook Login → Settings** → **Valid OAuth Redirect URIs**, add
**exactly**:
```
http://localhost:5000/integrations/social/instagram
```
Character-for-character. No trailing slash.

### C6. Paste the credentials
Edit `D:\ContentMachine\postiz\.env`:
```
FACEBOOK_APP_ID="<App ID from C3>"
FACEBOOK_APP_SECRET="<App Secret from C3>"
```
Then restart so they load:
```
powershell -ExecutionPolicy Bypass -File D:\ContentMachine\postiz\start-postiz.ps1
```

### C7. Connect the channel
Postiz → **Settings → Add channel → Instagram** → OAuth prompt → pick the IG
account + Page. **Do this in a browser on this PC** (the redirect is localhost).

---

## Fallback: if Meta rejects `http://localhost`

Some Meta products insist on HTTPS. If C5 won't save, in order of preference:

1. **`https://localhost:5000`** with a self-signed cert — Meta sometimes accepts
   the URI string even when the cert isn't trusted, since only your browser
   follows the redirect.
2. **Cloudflare named tunnel on a cheap separate domain** (~$10/yr). A *named*
   tunnel, not a Quick Tunnel: Quick Tunnels hand out a random
   `*.trycloudflare.com` hostname that changes on restart, which breaks the
   redirect URI on every reboot.
3. **Do not** move riseforit.com's nameservers to Cloudflare for this. The blast
   radius (live app + email) is far larger than the problem.

---

## What Postiz never receives

- Raw source video — never leaves `D:\ContentMachine\data\uploads`
- Anything over 100 MB — the Schedule wiring refuses it
- Only finished, approved clips + caption + hashtags
