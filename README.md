# ReLttk: manual daily TikTok message

This project uses **unofficial, reverse-engineered TikTok internal APIs**. Whether
messages sent by this bot count towards TikTok streaks **has not been verified**.
The network protocol, QR login and Android deployment remain unverified live in
this safety review. **CLI is the primary runtime on Termux; the Windows TUI is
an optional configuration interface.**

## Default: offline dry-run

```sh
python -B main.py
```

The default command reads only the plan. It does not read session cookies, open a
network connection, load plugins, or open the ledger. The shipped `streak.json`
is intentionally unconfigured; validation messages and exit code 1 are expected.

Copy the template to the Git-ignored personal configuration:

```sh
cp streak.json streak.local.json
python -B main.py --config streak.local.json
```

Fill `session`, the fixed `message`, and selected `targets`. Each target needs
`conv_id`, `conv_short_id` and `conv_type` (1 = direct, 2 = group). Obtain the exact
IDs from the selected account's inbox; do not guess IDs. For example, these are
**dummy values, not usable account or conversation data**:

```json
{
  "session": "my_account",
  "message": "Chao anh!",
  "targets": [
    {"conv_id": "0:1:111:222", "conv_short_id": 8888, "conv_type": 1}
  ]
}
```

## Explicit network operations, for later manual use

These commands contact TikTok; they are not part of dry-run or offline tests:

```sh
python main.py login
python main.py capture-ws-auth --session my_account
python main.py list-conversations --session my_account
python main.py --config streak.local.json --send
```

Login requires an interactive terminal for QR display. No QR URL fallback is
printed. `--send` and `--dry-run` are mutually exclusive; neither can be combined
with a subcommand. On Windows, login attempts optional headless Chromium auth
capture after saving the session. `capture-ws-auth` refreshes auth for an existing
session: it launches a browser, probes a WebSocket handshake without sending any
DM, then saves the matching pair. This setup step is **not browser-free**.

Install core requirements first. Optional Windows components are separate:

```sh
python -m pip install -r requirements.txt
python -m pip install -r requirements-tui.txt
python -m pip install -r requirements-auth-windows.txt
python -m playwright install chromium
python -B main.py tui
```

TUI inbox sync and QR login require explicit button presses. Plan edits do not
send. The manual send button sends the **saved** plan, not unsaved selections.
Its optional scheduler defaults off and only runs while the TUI remains open;
it triggers in the configured minute, without catch-up after sleep/closure.
Saving an enabled schedule explicitly permits future sends, including after
reopening the TUI. Neither CLI nor installation enables a scheduler. No cron,
Tasker or Termux:Boot job is installed by this project.

Auth is stored in `sesion/ws-auth/<session>.json` and bound to the session cookie
fingerprint. A changed login requires recapture. The old global
`ws_auth.local.json` and persistent browser profile are **not used by the one-shot
sender**; old files are left untouched. Recapture on Windows instead of renaming
an unbound file. Transfer the matching session/auth pair privately to Termux as
described in TERMUX.md. Missing/mismatched auth stops before opening WebSocket.

Live connectivity is **not ready to be assumed working**: old embedded sample
session tokens were removed, and device/SDK/signature parameters in the inherited
protocol still need verification with a current session. Do not put real tokens
into tracked source files to work around a failed connection.

## Quota and confirmation

- A persistent SQLite ledger in `state/` reserves each account/conversation/date
  **before transmission**, using `Asia/Ho_Chi_Minh`. A process lock and database
  transaction prevent concurrent duplicate reservations.
- Only a server message echo matching the outgoing UUID, selected conversation,
  actual own sender, positive server message ID and exact text records success.
  Socket send completion, a generic ACK and a local echo are not confirmation.
- A timeout or error is `failed_unknown`, with no retry that day. A crash-left
  `pending` blocks later dates too, until manually reviewed. Cross-midnight
  confirmation/failure also reserves the later date conservatively.
- There is no force-send option or quota bypass. Failed/unknown and pending
  records are not deleted to allow another attempt.
- This limit is local to one ledger. It cannot cover another device, deleted
  state, or messages sent manually in the TikTok app. Keep the phone clock correct.
- Dry-run does not inspect quota because it never authenticates or reads state.
  `python main.py status` inspects local ledger records.

## Security and services

QR login uses `www.tiktok.com` and `web-sg.tiktok.com`; inbox/profile APIs also use
`im-api-sg.tiktok.com`; messaging defaults to `im-ws-sg.tiktok.com` over verified TLS.
Signing runs locally. Legacy media helpers reference TikTok media CDNs. Sample
plugins and stranger auto-accept are disabled by default; the former video upload
plugin to `linkmail.wtf` is inert and has no download/upload implementation.

Sessions are **plaintext** JSON in `sesion/`, with directory mode 0700 and file
mode 0600 on POSIX, including session-bound WS auth. Writes are atomic; permission
failures stop access. Browser capture uses a fresh non-persistent context and
keeps the candidate in memory until a successful probe. Windows
`chmod` does not configure NTFS ACLs. Use a private user directory. Cookies,
sessions, state, local plans and logs are ignored by Git. Authentication errors
omit raw responses; log filtering additionally redacts sensitive values.
A Python `venv` isolates dependencies, **not security privileges**.

## Termux and offline checks

See [TERMUX.md](TERMUX.md) for Samsung S21 Ultra / Android ARM64 setup, dependency
notes, remaining blockers and safe manual commands.

```sh
python -B -m unittest discover -s tests -v
python -m pip check
```

Tests use temporary files, synthetic messages and fake WebSockets. They do not
prove TikTok accepts the current protocol or credits streaks.
The optional TUI smoke test is skipped when Textual is absent. Tested locally on
Windows/Python 3.10 with Textual 8.2.8 and Playwright 1.63.0 installed; browser
capture and live TikTok operations were not run.

## License

MIT
