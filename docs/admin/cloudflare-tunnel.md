# Sharing a test instance through Cloudflare Tunnel

Tessera needs PostgreSQL, Redis and long-running Python processes, so it does not run on Cloudflare Pages
or Workers alone. To let other people try it, run the Docker stack on a machine you control (your computer
or a small Linux server) and publish it through a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/):
`cloudflared` makes an outbound connection to Cloudflare, so no port is opened and no public IP is needed.
Cloudflare serves the address over HTTPS.

This is for testing and demos. A production deployment uses the `prod` web image behind a proper ingress
(see the architecture document).

## Before you share a link

- **Change the demo passwords.** Anyone with the link can reach the sign-in page, and the demo users'
  passwords are public. Sign in as `admin`, then change the passwords of `admin`, `demo` and `marie` under
  **Settings → Users**, or start from a database without demo data.
- Only the web client (port 5173) goes through the tunnel; it forwards `/api` and the realtime socket to the
  API itself.

## Option 1: a quick tunnel (no Cloudflare account, temporary address)

1. Start the stack and load the demo data as in the README (`make up`, `make demo`).
2. Install `cloudflared`: <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/>
   (Windows: `winget install --id Cloudflare.cloudflared`; macOS: `brew install cloudflared`).
3. Run:

   ```bash
   cloudflared tunnel --url http://localhost:5173
   ```

4. `cloudflared` prints an address such as `https://random-words.trycloudflare.com`. Open it, or send it to
   your testers. It works as long as the command runs; each run gets a new address.

The web dev server accepts `*.trycloudflare.com` addresses out of the box (`frontend/vite.config.ts`).

## Option 2: a named tunnel on your own domain (stable address)

Needs a free Cloudflare account and a domain whose DNS is on Cloudflare.

1. In the Cloudflare dashboard, open **Zero Trust → Networks → Tunnels**, create a tunnel, and follow the
   steps to install and run `cloudflared` with its token on the machine running Tessera.
2. Add a public hostname, for example `crm.example.com`, with service `http://localhost:5173`.
3. Tell the web dev server and the e-mail links about the address. In `.env`:

   ```bash
   VITE_ALLOWED_HOSTS=crm.example.com
   TESSERA_PUBLIC_URL=https://crm.example.com
   ```

   then restart the stack (`make down`, `make up`).
4. Optional: put the address behind **Cloudflare Access** (Zero Trust → Access → Applications) so only the
   e-mail addresses you list can open it.

## If the page says "Blocked request. This host is not allowed"

The web dev server only answers host names it knows. Add yours to `VITE_ALLOWED_HOSTS` in `.env`
(comma-separated) and restart the stack.
