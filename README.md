# MSE Public Dashboard

Public GitHub Pages dashboard that refreshes automatically from `https://new.mse.mn/trade-daily-report`.

- Public URL: `https://<username>.github.io/<repo>/`
- Refresh: weekdays at 18:00 Ulaanbaatar time
- Manual refresh: Actions → “Update MSE dashboard and deploy Pages” → Run workflow
- No Claude login required for viewers

## GitHub Pages setup
Repository → Settings → Pages → Source: **GitHub Actions**.

The workflow renders the MSE page in headless Chromium with Playwright, extracts public table data, generates `data/latest.json`, and deploys the static dashboard.
