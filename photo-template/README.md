# Photo Template

Upload a photo; it is placed into the rectangle of a template and downloaded as PNG.
Runs entirely in the browser (static site, no server, photos never leave the device).

## Add a template
1. Put the image in `templates/` (e.g. `templates/wedding.jpg`).
2. Open the site, expand **Add a new template**, choose the same image and drag the photo rectangle.
3. Copy the line it shows into `templates/templates.json`, then commit.

Format: `{ "name": "Wedding", "file": "wedding.jpg", "rect": [x, y, width, height] }` (pixels of the template image).

## Deploy on Vercel
New Project → import this repo → set **Root Directory** to `photo-template` → Framework: *Other* → Deploy.

## Run locally
`python3 -m http.server` in this folder, open http://localhost:8000.

## Shakebot templates
Built by `make_templates.py` (edit text there, re-run). Each template: background `file`, optional
`overlay` PNG drawn on top, optional `filter`: `orange` (duotone), `bw`, `warm`.

## Password
`middleware.js` asks for a password on every page and image (browser login box, any username).
Set it in Vercel → Project → Settings → Environment Variables: `SITE_PASSWORD` = your password, then redeploy.
Without that variable nobody can get in.
