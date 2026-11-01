# Remote access to the console

The console runs on your laptop and binds to `127.0.0.1`. A Cloudflare Tunnel publishes it
at `https://jobs.parth.party`. `cloudflared` opens an outbound connection to Cloudflare, so
no router port is open. The site is down whenever the laptop is asleep, offline, or not
running `start.py`.

Anyone with the password has full access. They can read your profile and EEO answers, and
they can submit applications. To remove someone's access, change the password.

## Setup

1. Set the password. It needs at least 12 characters.

   ```bash
   .venv/Scripts/python.exe scripts/set_console_password.py
   ```

2. Install `cloudflared`.

   ```bash
   winget install --id Cloudflare.cloudflared
   ```

3. Log in to Cloudflare, and pick `parth.party` in the browser window.

   ```bash
   cloudflared tunnel login
   ```

4. Create the tunnel and its DNS record.

   ```bash
   cloudflared tunnel create job-console
   ```

   ```bash
   cloudflared tunnel route dns job-console jobs.parth.party
   ```

5. Create `%USERPROFILE%\.cloudflared\job-console.yml`. Replace `<tunnel-id>` with the ID
   that step 4 printed.

   ```yaml
   tunnel: job-console
   credentials-file: C:\Users\<you>\.cloudflared\<tunnel-id>.json
   ingress:
     - hostname: jobs.parth.party
       service: http://127.0.0.1:8000
     - service: http_status:404
   ```

6. Restart the services. `start.py` starts the tunnel when the config file and the password
   both exist. Its log is `<logs>/tunnel.log`.

   ```bash
   .venv/Scripts/python.exe start.py
   ```

7. In the Cloudflare dashboard, add a redirect rule for `parth.party`:

   - **Match:** URI Path starts with `/jobs`
   - **Action:** static redirect to `https://jobs.parth.party`, status 302

## Change the password

Run step 1 again. This ends every open session. The running console reads the new password
on its next request, so you do not need a restart.

## Security notes

- A login attempt that fails five times locks that address for 15 minutes. Thirty failures
  across all addresses lock every login for 15 minutes. The counters reset on restart.
- Without `bridge/.console_auth.json`, the console refuses all tunnel traffic with a 503.
- Do not set `httpHostHeader` in the tunnel config. The console's cross-site check compares
  the `Origin` and `Host` headers.
