# irclogs

Small, dependency-free (Python stdlib) web UI for searching a personal IRC history index
(SQLite FTS5) and sharing selected lines as public, bash.org-style quote pages.

- `/` search, `/ctx?id=` context view (select lines → share), `/channels`, `/quotes`: private, behind Cloudflare Access.
- `/q/<id>` and `/q/<id>.txt`: public quote pages. A quote is a frozen copy of only the selected lines; optional nick masking.

The code is public; the data is not in this repo. See `docker-compose.yml` for the host paths it expects.
