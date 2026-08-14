# Windchaser Gorge

A free wind-conditions dashboard for the Columbia River Gorge, scraping
Victor the Inflictor and The Gorge Is My Gym daily and rendering a
kiteboarding-focused summary.

## Files

- `index.html` — the dashboard itself
- `scraper.py` — scrapes both sources, writes `summary.json`
- `summary.json` — sample data (real data once the daily Action runs)
- `requirements.txt` — Python deps for the scraper
- `.github/workflows/daily-scrape.yml` — runs the scraper once a day, commits the result

## Deploy: step by step

### 1. Initialize git locally

```bash
cd /Users/stevenjohnson/code/windchaser-gorge
git init
git add .
git commit -m "Initial commit"
```

### 2. Create the GitHub repo

Go to https://github.com/new
- Repository name: `windchaser-gorge` (or anything you like)
- Keep it **Public** (required for free GitHub Pages) or **Private** (Pages
  works on private repos too if you're on GitHub Free — either is fine)
- Don't initialize with a README/gitignore — you already have those locally
- Click **Create repository**

### 3. Push

GitHub shows you the exact commands after creating the repo, but they'll look like:

```bash
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/windchaser-gorge.git
git push -u origin main
```

If it asks for credentials and password auth fails (GitHub disabled that
years ago), it means you need a personal access token or to have the
GitHub CLI / Git Credential Manager set up — GitHub's push error message
links directly to the right doc for whichever case applies.

### 4. Enable GitHub Pages

In your repo on github.com:
- **Settings** → **Pages** (left sidebar)
- Under "Build and deployment", **Source**: select **Deploy from a branch**
- **Branch**: `main`, folder `/ (root)`
- Save

GitHub will build and give you a URL like
`https://YOUR_USERNAME.github.io/windchaser-gorge/` within a minute or two.
Add that to your phone's home screen and you're done for hosting.

### 5. Confirm the Action is enabled

Actions are on by default for repos you own, but double check:
- **Settings** → **Actions** → **General** → under "Actions permissions",
  make sure "Allow all actions and reusable workflows" (or similar) is
  selected, not "Disable actions"

### 6. Run it once manually to seed real data

- Go to the **Actions** tab → **Daily Gorge Wind Scrape** (left sidebar)
  → **Run workflow** button → **Run workflow**
- Wait ~30 seconds, refresh — you should see a commit appear from
  `gorge-dashboard-bot` updating `summary.json`
- Reload your Pages URL — it should now show real scraped data instead of
  the sample fallback

From here, it runs automatically every day at the time set in the
workflow file (currently 14:00 UTC — adjust the `cron` line if you want
it earlier/later relative to when both sites post their forecast).

## Does running the Action cost money?

No, not for this. GitHub Actions gives every account **2,000 free
minutes/month** on private repos, and **unlimited minutes on public
repos**. This job runs once a day and takes well under a minute, so
you're using roughly 30 minutes/month at most — nowhere close to any
limit either way.

## Checking your Actions usage

- Personal account-wide usage: **github.com** → click your profile photo
  (top right) → **Settings** → **Billing and plans** → **Plans and usage**
  → look for "Actions" under usage this month
- Per-repo usage: in the repo → **Settings** → **Billing and plans**
  isn't per-repo on personal accounts, so the account-wide page above is
  the one that matters — but you can watch individual run durations in
  the **Actions** tab of the repo itself
