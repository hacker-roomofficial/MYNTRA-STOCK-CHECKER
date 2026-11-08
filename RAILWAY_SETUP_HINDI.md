# Stock Intelligence — Railway deployment (test build)

## Kya fix hua

- Pichhle project ke `Dockerfile` me galat `python main.py serve` command thi. Ab `python main.py dashboard --host 0.0.0.0` hai. Isse **Login + dashboard** open hoga, legacy Excel upload/API page nahi.
- ZIP me `Dockerfile`, `main.py`, `requirements.txt`, `railway.toml` aur **`stockwatch/` folder sab ZIP ke root par** hain. GitHub me bhi same structure rakho.
- Dashboard ka frontend `stockwatch/ui/` me bundled hai aur Docker build ke dauraan sab files ka check hota hai.
- `stockwatch/` naam sirf internal Python module hai. User-facing app Stock Intelligence hai. Is internal module ko delete na karo.

## Step 1 — GitHub par bilkul sahi structure upload karo

ZIP extract karo. **Andar ke files** GitHub repository ke root par upload karo. Structure:

```text
REPOSITORY ROOT/
  Dockerfile
  railway.toml
  requirements.txt
  main.py
  stockwatch/
    __init__.py
    platform.py
    platform_v3.py
    platform_integrations.py
    ui/
      dashboard.html
      login.html
      app.js
      styles.css
  tests/
```

`stock-intelligence-railway-fixed/` naam ka extra folder GitHub ke root me mat banaao. Agar banate ho to Railway Root Directory `/stock-intelligence-railway-fixed` set karna padega.

## Step 2 — Railway service

1. Railway -> New Project -> Deploy from GitHub -> apna repository select karo.
2. Service -> Settings -> Source -> **Root Directory** `/` rakho, jab files repository root me hain.
3. Build builder Dockerfile / Docker (Dockerfile automatically detected).
4. **Custom Start Command** ko blank / unset rakho. Dockerfile ka `CMD` run hoga. Agar override mandatory ho: `python main.py dashboard --host 0.0.0.0`.
5. **Healthcheck Path** `/health` rakho. Response `{"ok": true}`.
6. Replicas **1** rakho (SQLite + scheduler single-process).

## Step 3 — Variables

Railway -> Service -> Variables -> Add:

```env
PLATFORM_ADMIN_USER=admin
PLATFORM_ADMIN_PASSWORD=<generate-a-new-unique-password-12-or-more-characters>
PLATFORM_SECURE_COOKIE=1
STOCKWATCH_DATA_DIR=/data
STOCKWATCH_HEADLESS=1
STOCKWATCH_BROWSER_TABS=1
STOCKWATCH_RPS=1
STOCKWATCH_CONCURRENCY=1
PLATFORM_SYNC_SECONDS=90
```

- `<...>` ko replace karo, angle brackets literal mat daalo. Password **Railway Variables mein private rakho**; GitHub me kabhi commit na karo.
- `PORT` Railway automatically deta hai. Manual set karne ki zaroorat nahi.
- Existing persistent database par `PLATFORM_ADMIN_PASSWORD` badalne se existing admin password reset **nahi** hota; ye environment variable first-ever bootstrap ke liye hai.

## Step 4 — Persistent data volume

Railway service -> Attach Volume -> mount path **`/data`**. SQLite DB, browser profile aur reports volume me persist honge. Without volume, redeploy ke baad saved accounts/campaigns gayab ho sakte hain.

## Step 5 — Domain, login aur visual check

1. Deploy -> Build Logs me `Dashboard files and imports OK` check karo.
2. Deploy Logs me server listening / running check karo.
3. Settings -> Networking -> Public Networking -> **Generate Domain** karo.
4. `https://YOUR-DOMAIN.up.railway.app/health` kholo: `{"ok": true}` expected.
5. `https://YOUR-DOMAIN.up.railway.app/login` kholo: Stock Intelligence **Sign In** page expected.
6. Admin username/password se sign in karo.
7. Pehli login par company brand name, initials, color, light/dark mode select karo.
8. Sidebar me Overview, Campaigns, Stock History, Access Control, Reports, Settings test karo.

## Step 6 — Google Sheets

Sheet columns: `style_id`, `sizes`, optional `active`.

```csv
style_id,sizes,active
46608964,"XS,S,M,L,XL",1
45141144,"S,M,L,XL,XXL",1
```

- Public sheet: sharing/export accessible karo; URL se create campaign -> Sync.
- Private sheet: Google Cloud OAuth Web Application credentials banao. Railway Variables:
  - `GOOGLE_OAUTH_CLIENT_ID`
  - `GOOGLE_OAUTH_CLIENT_SECRET`
  - `GOOGLE_OAUTH_REDIRECT_URI=https://YOUR-DOMAIN.up.railway.app/api/google/callback`
  - `PLATFORM_ENCRYPTION_KEY` (Fernet key generated privately)
- Google Cloud OAuth client me **same exact redirect URI** add karo.
- Google Sheets API aur Google Drive API enable karo. Google user ko sheet par Viewer (read) access chahiye; Drive upload ke liye suitable folder permission chahiye.
- OAuth configured hone ke baad Settings -> Google Sheets -> Connect Google.
- Google Cloud 'Testing' OAuth app me signed-in Google account ko **Test users** me add karna pad sakta hai.
- Google service account alternative ke liye `GOOGLE_SERVICE_ACCOUNT_JSON` is code me **JSON file path** accept karta hai, raw JSON string nahi. Testing me OAuth connect karo, ya service-account JSON ko secure volume path par provision karo.

## Step 7 — Access Control

- **Admin**: brand, users, all campaigns, settings, exports.
- **Manager**: authorized campaigns, sync/check/reports; no user-management page.
- **Employee**: authorized campaign results. Sync/Run button backend admin/manager-only hai.

Admin -> Access Control -> Add/Create User -> username + strong password + role. Fir Campaigns me campaign select -> Assign User -> user choose karo. **Create User email invitation nahi bhejta**; credentials securely employee ko provide karne honge.

## Step 8 — Agar UI nahi dikh rahi

| Symptom | Check |
|---|---|
| `"/stockwatch": not found` | GitHub root mein `stockwatch/` nahi hai / wrong Railway Root Directory |
| Old Excel/API upload page | Legacy `python main.py serve` start override hatao; correct Dockerfile redeploy |
| 502 / Application failed to respond | Deploy Logs, `PORT`, startup error, `/health` |
| `/health` works, `/login` blank | Static assets `/assets/styles.css` and `/assets/app.js`, browser devtools console |
| Admin setup error | `PLATFORM_ADMIN_USER` and 12+ char `PLATFORM_ADMIN_PASSWORD` |
| Login succeeds but loops | `PLATFORM_SECURE_COOKIE=1`, Railway HTTPS URL, don't use http:// for public site |
| Sheet sync failed | Public permission or Google OAuth setup, exact Sheet URL and column names |
| Browser checks fail | Myntra dynamic loading/captcha/rate limits; verify with 5 IDs first |

**Note:** This is a Railway testing build, not validated enterprise production. Browser automation, OAuth and Drive still need live account-based verification. Single instance only; multi-replica scaling needs a separate database/worker architecture.
