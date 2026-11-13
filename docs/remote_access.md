# Publish the console on your own domain (optional)

By default, the console is only on your laptop at http://127.0.0.1:8000. If you own a
domain on Cloudflare, you can publish the console at an address such as
`https://jobs.example.com` and open it from your phone or another computer. The tool works
the same without this.

The console keeps running on your laptop and binds to `127.0.0.1`. A Cloudflare Tunnel
publishes it. `cloudflared` opens an outbound connection to Cloudflare, so you do not open
any router port. The site is down whenever the laptop is asleep, offline, or not running
`start.py`.

A password page guards the published site. Anyone with the password has full access. They
can read your profile and EEO answers, and they can submit applications. To remove
someone's access, change the password.

## Before you start

- A domain whose DNS is on Cloudflare. The free plan is enough.
- The console already set up and running with `python start.py`.

In the steps below, replace `example.com` with your domain and `jobs.example.com` with the
address you want.

## Setup

1. Set the password. It needs at least 12 characters.

   ```bash
   .venv/Scripts/python.exe scripts/set_console_password.py
   ```

2. Install `cloudflared`.

   ```bash
   winget install --id Cloudflare.cloudflared
   ```

3. Log in to Cloudflare. In the browser window that opens, pick your domain.

   ```bash
   cloudflared tunnel login
   ```

4. Create the tunnel and its DNS record. Keep the tunnel name `job-console`, because
   `start.py` looks for that name.

   ```bash
   cloudflared tunnel create job-console
   ```

   ```bash
   cloudflared tunnel route dns job-console jobs.example.com
   ```

5. Create `%USERPROFILE%\.cloudflared\job-console.yml`. Replace `<you>` with your Windows
   user name and `<tunnel-id>` with the ID that step 4 printed.

   ```yaml
   tunnel: job-console
   credentials-file: C:\Users\<you>\.cloudflared\<tunnel-id>.json
   ingress:
     - hostname: jobs.example.com
       service: http://127.0.0.1:8000
     - service: http_status:404
   ```

6. Restart the services. If the config file and the password both exist, `start.py`
   starts the tunnel. Its log is `<logs>/tunnel.log`.

   ```bash
   .venv/Scripts/python.exe start.py
   ```

7. Open `https://jobs.example.com` from another device and log in.

8. Optional: send a path on your main site to the console. In the Cloudflare dashboard,
   add a redirect rule for `example.com`:

   - **Match:** URI Path starts with `/jobs`
   - **Action:** static redirect to `https://jobs.example.com`, status 302

## Change the password

Run step 1 again. This ends every open session. The running console reads the new password
on its next request, so you do not need a restart.

## Turn it off

Stop `start.py`, then delete or rename `job-console.yml`. The next `start.py` skips the
tunnel.

## Security notes

- A login attempt that fails five times locks that address for 15 minutes. Thirty failures
  across all addresses lock every login for 15 minutes. The counters reset on restart.
- Without `bridge/.console_auth.json`, the console refuses all tunnel traffic with a 503.
- Do not set `httpHostHeader` in the tunnel config. The console's cross-site check compares
  the `Origin` and `Host` headers.
