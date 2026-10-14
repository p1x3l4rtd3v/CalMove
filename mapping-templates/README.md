# Mapping workspace

Working folder for the address and room mappings described in
[`docs/ADMIN_GUIDE.md`](../docs/ADMIN_GUIDE.md) section 2.

Everything in this folder except this README and the `*.example.json` templates is
gitignored: real resource e-mails, room names, and domain plans are internal data and
must not enter the repository.

Typical contents (local only):

- `OLD GWS.csv` / `NEW GWS.csv`: resource exports from both Admin consoles
  (Directory → Buildings and resources → Manage resources), matched by room name.
- `rooms.json`: old → new resource e-mails, generated from the two CSVs.
- `domains.json`: old → new attendee domains, for reference; the value is passed to the
  container as `CALMOVE_WEB_DOMAIN_MAP` (or `--map-domain` on the CLI).

Mount the generated files read-only when starting the container:

```sh
docker run -d --name calmove-web -p 8080:8080 \
  -v "$PWD/mapping-templates/rooms.json:/run/config/rooms.json:ro" \
  -e CALMOVE_WEB_ROOM_MAP=/run/config/rooms.json \
  -e CALMOVE_WEB_DOMAIN_MAP="old-company.com=new-company.com" \
  -e CALMOVE_WEB_BASE_URL=https://calmove.example.com \
  calmove-web
```
