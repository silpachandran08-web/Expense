// Vercel Routing Middleware: every request needs the password (browser login prompt, any username).
// Password comes from the SITE_PASSWORD env var so it is not in the repo; unset = nobody gets in.
export const config = { matcher: '/:path*' };

export default function middleware(request) {
  const pass = process.env.SITE_PASSWORD;
  const auth = request.headers.get('authorization') || '';
  const [scheme, encoded] = auth.split(' ');
  if (pass && scheme === 'Basic' && encoded) {
    const decoded = atob(encoded);
    const colon = decoded.indexOf(':');
    if (colon >= 0 && decoded.slice(colon + 1) === pass) return; // continue to the site
  }
  return new Response('Password required', {
    status: 401,
    headers: { 'WWW-Authenticate': 'Basic realm="Shakebot", charset="UTF-8"' },
  });
}
