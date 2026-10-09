# ReLttk: manual daily TikTok message

This project uses **unofficial, reverse-engineered TikTok internal APIs**. Whether
messages sent by this bot count towards TikTok streaks **has not been verified**.
The network protocol, QR login and Android deployment remain unverified live.

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
python main.py list-conversations --session my_account
python main.py --config streak.local.json --send
```

Login requires an interactive terminal for QR display. No QR URL fallback is
printed. `--send` and `--dry-run` are mutually exclusive; neither can be combined
with a subcommand. There is no scheduler installed or enabled.

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
- This limit is local to one ledger. It cannot cover another device, deleted
  state, or messages sent manually in the TikTok app. Keep the phone clock correct.
- Dry-run does not inspect quota because it never authenticates or reads state.
  `python main.py status` inspects local ledger records.

## Security and services

QR login uses `www.tiktok.com` and `web-sg.tiktok.com`; inbox/profile APIs also use
`im-api-sg.tiktok.com`; messaging uses `im-ws-va.tiktok.com` over verified TLS.
Signing runs locally. Legacy media helpers reference TikTok media CDNs. Sample
plugins and stranger auto-accept are disabled by default; the former video upload
plugin to `linkmail.wtf` is inert and has no download/upload implementation.

Sessions are **plaintext** JSON in `sesion/`, with directory mode 0700 and file
mode 0600 on POSIX. Writes are atomic; permission failures stop access. Windows
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

## License

MIT
