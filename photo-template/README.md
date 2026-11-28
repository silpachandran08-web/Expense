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
