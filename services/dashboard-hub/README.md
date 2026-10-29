# Dashboard hub

Feeds the BOARDS tab in Pentacle Mobile. Listens on port 7781 (127.0.0.1 and
the Tailscale IP) and pushes four boards over `ws://<host>:7781/live?token=…`:

| Board id | Source | Refresh |
|---|---|---|
| `sai.trading` | iv-rank-scanner paper bot: pidfiles, trader log, router on :7410, IB Gateway :4002, ThetaData :25503 | 15 s |
| `sai.projects` | Pentacle sessions and status cards (`agent-orch list`, sessions.db read-only) | 10 s |
| `sai.finance` | Lews Budget API on 127.0.0.1:8765 | 5 min |
| `sai.travel` | Travel Engine API (balances only) | 5 min |

The phone authenticates with its Pentacle device token (the same
`pentacle-auth-v2:` envelope it uses for the daemon), checked against
`~/.config/pentacle-stream/operator-credentials.json`. Revoking the phone in
Pentacle locks it out here too. Only `pentacle-mobile` credentials are accepted.

Interactive endpoints (Bearer token):

- `GET /api/finance/filters`, `GET /api/finance/transactions?q=&account_id=&category_id=&start=&end=`,
  `GET /api/finance/month?month=YYYY-MM`, `POST /api/finance/sync`
- `POST /api/travel/searches {origin,dest,date}`, `GET /api/travel/searches/{id}`
- `POST /api/boards/{id}/refresh`

Collectors only read. Trading has no controls, and the travel vault and
booking endpoints are deliberately not proxied.

## Run

```sh
python3 -m venv .venv && .venv/bin/pip install aiohttp pytest pytest-aiohttp
.venv/bin/python -m pytest
.venv/bin/python -m dashboard_hub --host 127.0.0.1 --port 7781
```

Install as a LaunchAgent:

```sh
cp deploy/com.sai.pentacle.hub.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sai.pentacle.hub.plist
```

After code changes: `launchctl kickstart -k gui/$(id -u)/com.sai.pentacle.hub`.
Log: `~/Library/Logs/Pentacle/hub.log`.
