# Myntra Size-wise Stock Monitor → n8n → WhatsApp

Excel se style IDs → Myntra check (size-wise) → out-of-stock ho to **style ID + size** WhatsApp pe alert.

```
 styles.xlsx ──► stockwatch (Python, asyncio) ──► SQLite (resume + dedup) ──► report_*.xlsx
                    │  fast engine : aiohttp, adaptive speed, session cookies from real Chrome
                    │  search engine: Chrome (background) → search bar → style ID → product page
                    ▼
              n8n webhook ──► format message ──► WhatsApp
```

## 1. Setup (Windows / Mac / Linux)

```bash
python -m venv .venv && .venv\Scripts\activate        # Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium                 # sirf agar Google Chrome installed nahi hai
copy .env.example .env                                # Mac/Linux: cp .env.example .env   (phir edit karo)
```

## 2. Input Excel

| style_id | sizes |
|---|---|
| 12345678 | S, M, L |
| 23456789 | *(blank = saare sizes watch karo)* |

* Header na ho to bhi chalega (column A me sirf IDs).
* Excel ke float IDs (`12345678.0`), duplicate rows, kharab rows — sab handle hote hain.
* Ek hi style 2 baar ho (blank + "M") to **saare sizes** watch hote hain (blank = all).

Sample banane ke liye: `python main.py sample --count 100`

## 3. Run

```bash
python main.py login                                   # EK baar: Chrome dikhega, captcha/login kar lo, Enter dabao
python main.py run -i styles.xlsx                      # default mode = auto
python main.py run -i styles.xlsx --mode browser --tabs 4     # sirf search-bar se (slow, sabse "human")
python main.py run -i styles.xlsx --run-id oct25 --resume     # beech me ruka? wahin se continue
```

| Mode | Kya karta hai | Speed |
|---|---|---|
| `auto` (default) | Chrome se session leke fast HTTP; jo fail ho unhe search bar se dobara check | lakhs |
| `fast` | sirf fast HTTP | lakhs |
| `browser` | Chrome background me, search bar me style ID type karke | ~1–3 style/sec per tab |

Output: `out/report_<run_id>.xlsx` → sheets `Out_of_Stock`, `Errors_NotFound`, `All`.

## 4. n8n + WhatsApp

1. n8n me **Import from file** → `n8n/myntra_whatsapp_alert.json`
2. **Format WhatsApp Messages** node me `SECRET = 'CHANGE_ME'` ko apni key se badlo (wahi `.env` ke `N8N_API_KEY` me).
3. **Send WhatsApp** node me credentials + `phoneNumberId` + `recipientPhoneNumber` (`91XXXXXXXXXX`) bharo. Import ke baad node ke fields ek baar dekh lena (n8n versions me thoda badalte hain).
4. Workflow **Active** karo, Production webhook URL `.env` me `N8N_WEBHOOK_URL` me daalo.

Alert aisa dikhega:
```
🚨 Myntra Stock Alert (1/3)
🟠 Size OOS 12345678 Roadster Shirt
   ❌ M, XL
   https://www.myntra.com/12345678
🔴 ALL OUT 23456789 ...
```

**WhatsApp ke options:** official *WhatsApp Business Cloud API* (n8n ka WhatsApp node) me business-initiated message ke liye approved **template** chahiye (24h window ke bahar). Alternative: Twilio, ya WAHA/Evolution API (unofficial — number ban hone ka risk). Node badalna ho to sirf last node replace karo; `{{$json.text}}` ready hai.

## 5. Alerts ka behaviour

* Wahi OOS state dobara alert **nahi** hoti (`realert_hours`, default 24h). Size badle ya naya style OOS ho to turant.
* Wapas stock me aane pe state reset; phir OOS hua to fir alert.
* Ek run me max `max_alert_styles=300` WhatsApp pe jaate hain (flood se bachne ko); baaki Excel report me. Agle run me agle 300.
* n8n down ho to alerts "pending" rehte hain, agle run me dobara jaate hain.
* Roz/har ghante chalane ke liye Windows Task Scheduler / cron me `python main.py run -i styles.xlsx` laga do.

## 6. Speed — sach kya hai

* Engine ki apni speed (local mock pe, 2-CPU box): **~7,700 styles/sec, 1 lakh = ~13 sec**. Isme aiohttp use hua hai (httpx 17x slow nikla — maine measure kiya).
* **Myntra pe asli speed** is baat se tay hogi ki Myntra aur aapka IP kitni requests/sec tolerate karte hain, engine se nahi. `STOCKWATCH_RPS` kam se shuru karo (60), tool 429/403 pe khud slow hota hai aur theek chalne pe khud tez. Lakhs ko *minutes* me karne ke liye 300–1000 req/s chahiye — iske liye rotating proxies (`STOCKWATCH_PROXIES_FILE`) lagne ki sambhavna hai.
* Agar aap Myntra **seller** ho: Seller/Partner portal ki inventory report / API se apna stock lena sabse tez aur sabse reliable hai (exact quantity, koi blocking nahi). Yeh tool tab kaam aata hai jab aapko customer ko *dikhne wala* stock check karna ho.
* Myntra ki Terms of Use automated access pe rok laga sakti hain — apne risk pe, respectful rate pe chalao.

## 7. ⚠️ First-run checklist (live Myntra pe, ek baar)

Maine ye poora pipeline local mock site/API + real Chromium pe test kiya hai (`pytest`). **Live Myntra se connect nahi ho paya** (mere sandbox me network allow-list hai), isliye 3 cheezein aap ek baar verify karo — sab `stockwatch/config.py` me ek-ek line hain:

1. `api_path` (`/gateway/v2/product/{style_id}`) — browser DevTools → Network me ek product page kholke dekho kaun si JSON call sizes deti hai. Alag ho to line badlo. Na chale to `--mode browser` ya `auto` chalega (page ke `window.__myx.pdpData` se padhta hai).
2. `search_selector` — search box ka CSS selector.
3. Size ka `available` field — `python main.py run -i one.xlsx --mode browser --headed --limit 1` chalake 1 style ka result Myntra page se match karo (jo size page pe cut/OOS dikhe wahi `oos_sizes` me aaye).

Agar Myntra captcha/challenge de: `python main.py login` se ek baar solve karo (profile `.chrome-profile/` me save rehta hai) aur `--rps` kam karo.

## 8. Tests

```bash
pip install -r requirements-dev.txt && python -m pytest   # parser, Excel, 20k-style pipeline, resume, alert dedup, real-Chromium search bar, auto-fallback
python -m tests.bench 100000
```

## 9. Files

```
main.py                     CLI entry
stockwatch/config.py        sab settings (endpoint, selectors, speed, alerts)
stockwatch/parser.py        Myntra payload → size-wise OK / PARTIAL_OOS / ALL_OOS
stockwatch/fetcher_http.py  fast async engine + adaptive throttle + session refresh + proxies
stockwatch/fetcher_browser.py  Chrome search-bar engine + session warm-up
stockwatch/store.py         SQLite: resume + alert de-dup
stockwatch/notifier.py      n8n webhook (chunked, retried)
stockwatch/runner.py        pipeline
n8n/myntra_whatsapp_alert.json   importable workflow
```

---

# 10. Railway par production deploy (web service)

`python main.py serve` ek web service chalata hai: browser me **upload page**, `POST /api/runs` API,
live progress, Excel report download, WhatsApp alerts (n8n ke through), optional schedule.
Railway project me 2 cheezein hongi: **n8n (+Postgres)** aur **stockwatch** (ye project).

```
Browser/curl --upload--> stockwatch (Railway, Docker+Chromium, Volume /data) --webhook--> n8n (Railway) --> WhatsApp
```

## Steps

1. **GitHub repo**: is folder ko private repo me push karo. Repo ke **root** me `main.py`, `stockwatch/`, `Dockerfile`,
   `requirements.txt` hone chahiye. (Build ke time Dockerfile khud check karta hai ki `stockwatch` import ho raha hai;
   agar folder chhut gaya to *build* fail hoga, container crash-loop nahi karega.)
2. Railway → **New → GitHub Repo** → ye repo. `railway.toml` se Dockerfile builder, `/health` healthcheck aur restart policy apne-aap lag jaati hai.
3. Service me **Volume** add karo, **mount path `/data`** (jobs, uploads, reports, Chrome profile yahin rehte hain; redeploy par bachte hain).
4. **Variables** (neeche table), phir **Settings → Networking → Generate Domain**.
5. Domain kholo → password prompt: user kuch bhi, password = `STOCKWATCH_API_KEY`. Excel upload karo.

| Variable | Zaroori? | Matlab |
|---|---|---|
| `STOCKWATCH_API_KEY` | **haan** | Bina iske service start hi nahi hoti (public URL unprotected nahi chhodte). Lamba random rakho. |
| `N8N_WEBHOOK_URL` | haan | n8n ka **production** webhook: `https://<n8n-domain>/webhook/myntra-stock` |
| `N8N_API_KEY` | haan | n8n Code node ka `SECRET` (wahi value) |
| `STOCKWATCH_RPS` / `STOCKWATCH_MAX_RPS` | nahi | start / ceiling requests per second (default 60 / 400) |
| `STOCKWATCH_BROWSER_TABS` | nahi | search-bar fallback ke parallel tabs (default 4; chhote plan par 2) |
| `STOCKWATCH_SCHEDULE_HOURS` | nahi | `6` = har 6 ghante pichli uploaded file dobara check. `0` = band |
| `STOCKWATCH_PROXIES` | nahi | comma-separated proxy URLs (`http://user:pass@host:port,...`) |
| `STOCKWATCH_ALWAYS_NOTIFY` | nahi | `1` = sab in-stock ho tab bhi summary WhatsApp |

`STOCKWATCH_DATA_DIR=/data` aur `PORT` Dockerfile/Railway khud set karte hain.

## API (scripts / cron ke liye)

```bash
curl -H "X-Api-Key: $KEY" -F file=@styles.xlsx -F mode=auto https://<domain>/api/runs     # -> {"run_id": "..."}
curl -H "X-Api-Key: $KEY" https://<domain>/api/runs/<run_id>                               # status + progress
curl -H "X-Api-Key: $KEY" -o report.xlsx https://<domain>/api/runs/<run_id>/report
```

## Reliability

* Ek waqt me ek run; baaki queue me. Run ke beech redeploy/crash hua to restart par **wahi run resume** hota hai
  (jo styles ho chuke, dobara fetch nahi hote). Test me verify hua hai.
* Chrome warm-up fail ho (Myntra block/timeout) to run fail nahi hota, bina session cookies ke HTTP se chalta hai.
* n8n down ho to alerts pending rehte hain, agle run me jaate hain.
* Volume redeploy par kuch second ka downtime hota hai (Railway ka behaviour).

## Dhyan do

* **Docker image maine yahan build nahi kiya** (mere sandbox me Docker daemon nahi chalta). Code ko Dockerfile jaisi clean copy me start karke test kiya hai. Pehla build Railway pe hoga; build logs dekhna.
* **Myntra datacenter IPs ko block kar sakta hai** (Railway ka IP ghar ke IP se zyada risky hai). 403/429 zyada aaye to `STOCKWATCH_PROXIES` me residential/rotating proxies lagao, `STOCKWATCH_RPS` kam karo.
* Plan ke hisaab se volume size limit hai (trial 0.5GB, Hobby 5GB) - reports chhoti hoti hain, par purani files kabhi-kabhi saaf kar lena.
* `railway.toml` (config-as-code) ke baare me Railway ke docs me deprecation notice hai (purani files 2026-12-01 tak chalengi). Wahi settings dashboard me bhi set ho sakti hain: Builder = Dockerfile, Healthcheck path = `/health`, Restart = On failure.


## UI and browser-check update (2026-10-29)

- Dashboard has a responsive monitoring layout with run progress, summary cards, report download, and per-style size-status chips.
- Browser mode opens each supplied style ID's product page directly instead of depending on the fragile search-bar CSS selector.
- Missing stock payloads, access challenges, and timeouts remain `ERROR` (unverified), never automatically reported as out of stock.
- The dashboard's size chips are based on the stock status saved by the checker. If Myntra blocks requests or withholds its product payload, the UI will clearly say stock is unverified; it cannot invent sizes or quantities.


### Dashboard refresh and Myntra maintenance pages
The dashboard now polls every 15 seconds and avoids re-rendering unchanged run data, reducing flicker. If Myntra returns a Site Maintenance/Oops page, the run is marked ERROR/unverified and is never interpreted as out of stock. This is a source-side response; the application cannot guarantee live stock results until Myntra returns valid product data.
