# Troubleshooting

## The board never appears

1. **Is it on the network?**  Open the serial console:
   `mpremote connect COM5 repl` (Windows) or `mpremote connect /dev/ttyACM0 repl`.
   You should see `wifi: connected, ip=…` followed by `connecting to http://…`.
2. **Is the URL right?**  `http://<pi-ip>:8080` — no trailing slash, no `/ws`
   suffix; the firmware adds the path itself.
3. **Can it reach the Pi?**  `ping <pi-ip>` from another machine on the same
   network, and check that port 8080 is open:
   `curl http://<pi-ip>:8080/healthz`.
4. **Is enrolment open?**  The board needs either a token in `/config.json` or
   an open enrolment window.  Press **Add device → Allow new devices** first.
   Logs on the hub: `sudo journalctl -u smarthome -f` — look for
   `node … connected` or `unauthorized`.
5. **Token mismatch?**  `enrolled` is only issued to nodes without a token.  If
   you rotated the token on the hub, put the new one on the board.

## "offline" on the card but the board is powered

* The hub drops a node after `offline_after_s` of silence.  Check the node's
  serial output for repeated `connecting to …` — that means the WebSocket is
  failing, not the Wi-Fi.
* If several boards flap together, the Pi may be the bottleneck (too many
  reconnects, or an SD card stalling).  Check `journalctl -u smarthome` for
  `timed out` or `no ack`.
* Confirm the Pi's clock is sane: `timedatectl`.  TLS is not used by default,
  so a wildly wrong clock does not break the socket itself.

## A command shows "sending" forever

The node did not ack.  Either it is busy in a blocking driver call (a long
`sleep` in `apply()`), or the frame never arrived.

* Watch the hub log: `no ack for <uid> (c12) attempt 3` then
  `command c12 for <uid> failed: no acknowledgement from node`.
* On the board, a traceback in the REPL points at the driver.
* The desired value stays queued; fix the driver and it will apply on the next
  reconnect.

## The wrong device changes

Device ids are global.  Two boards both announcing `strip-1` will fight over
the same device and the hub will keep reassigning it to whichever connected
last.  Give every board unique ids (`desk-strip`, `bench-strip`) or make them
unique per node in `main.py`.

## "not reporting" badge

The node is connected but did not include that device in its `hello`.  Usual
causes: the device threw during `setup()` (look for
`<uid>: unavailable: …` on the serial console), or its `type` in `main.py` is
not in the `registry` dict so it was skipped.

## The UI shows a type error banner

A manifest failed to load.  The banner names the file and the reason.  Fix the
JSON and press **Reload manifests**.  A bad manifest never takes the hub down;
its devices simply do not appear.

## Login problems

* Forgot the password — easiest is **Settings → Users → Reset password** from
  another admin account, or from the shell:
  ```bash
  sudo /opt/smarthome/.venv/bin/python -m smarthome set-password admin
  ```
  This writes straight into the SQLite database the service uses, so it takes
  effect on the next login with **no restart**.  (Older versions kept the hash
  in `config.json` and the running process cached it in memory, which is why a
  reset appeared to do nothing.)
* Locked out because there is no admin: the CLI can still write to the
  database, so the command above creates the `admin` account if it is missing.
* Locked out ("too many attempts"): wait `login_lockout_s` (60 s by default).
* Cookie not sticking over a reverse proxy: serve HTTPS and set
  `auth.cookie_secure: true` in `config.json`, and make sure the proxy passes
  `X-Forwarded-Proto`.

## History is empty for a light

History is sampled: sensors on every reading, everything else at most every
five seconds and only when a value changed.  A light that has not changed has
no rows.  Check with
`curl -H "X-API-Key: $KEY" http://pi:8080/api/devices/<uid>/history?hours=24`.

## Starting over

```bash
sudo systemctl stop smarthome
sudo rm /var/lib/smarthome/smarthome.db*      # devices, nodes, history
sudo systemctl start smarthome                # boards re-register themselves
```

Only the database needs removing: device manifests and code are untouched, and
every node re-announces its devices on reconnect.

## Getting more detail

```bash
sudo systemctl stop smarthome
sudo -u smarthome /opt/smarthome/.venv/bin/python -m smarthome serve --log-level debug --access-log
```

On a bench, `python -m smarthome serve` from the checkout uses `~/.smarthome`
and prints everything to the terminal.
