# Portlance Exports — website

A single static page: `index.html` plus `vendor/three.min.js` (the 3D library, bundled so the site
does not depend on an outside CDN). No build step and no server code.

## Before going live
Add a phone / WhatsApp number and address to the contact section in `index.html` if you want them shown.

## Put it online (pick one)
- **Netlify Drop** — go to app.netlify.com/drop and drag the `spices-website` folder in. Free, about a minute.
- **Vercel** — `npx vercel deploy spices-website --prod`
- **Any web host / cPanel** — upload `index.html` and the `vendor` folder to the site's public folder.

Then point your domain at the host from its dashboard.

## Enquiry emails (Netlify Forms)
The quote form is a Netlify form named `quote`. After deploying to Netlify:
1. Site → **Forms** → click **Enable form detection** (if shown), then deploy again (drag the folder in once more).
2. Forms → **Form notifications** → **Add notification** → **Email notification** →
   choose form `quote`, enter the address that should receive enquiries, save.
Each enquiry is then emailed there, and every submission is also listed under Forms.
The free plan includes 100 submissions a month. A hidden honeypot field filters basic spam bots.
On hosts without Netlify Forms (e.g. GoDaddy cPanel) the form falls back to opening the visitor's email app.

## Preview locally
`python3 -m http.server -d spices-website 8000` and open http://localhost:8000
