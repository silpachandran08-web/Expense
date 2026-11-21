# Spice Route Exports — website

A single static page: `index.html` plus `vendor/three.min.js` (the 3D library, bundled so the site
does not depend on an outside CDN). No build step and no server code.

## Before going live
Search `index.html` for these and replace them:
- `Spice Route` / `Spice Route Exports` — business name
- `+00 00000 00000`, `sales@example.com` (appears twice), `Your address here` — contact details
- Product table and stage facts — confirm grades, packing and figures match what you sell

## Put it online (pick one)
- **Netlify Drop** — go to app.netlify.com/drop and drag the `spices-website` folder in. Free, about a minute.
- **Vercel** — `npx vercel deploy spices-website --prod`
- **Any web host / cPanel** — upload `index.html` and the `vendor` folder to the site's public folder.

Then point your domain at the host from its dashboard.

## Preview locally
`python3 -m http.server -d spices-website 8000` and open http://localhost:8000
