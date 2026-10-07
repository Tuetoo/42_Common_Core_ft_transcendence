# Gateway TLS certificates (not committed)

`server.crt` and `server.key` in this directory are generated locally by
`infra/gateway/gen-certs.sh` (run by `make certs`, and automatically by
`make up` when they are missing). They are a self-signed development
pair for `localhost` / `127.0.0.1`, valid 825 days, and are mounted
read-only into the gateway container at `/etc/nginx/certs`.

Regenerate with `sh infra/gateway/gen-certs.sh --force`. Everything in
this directory except this README is ignored by git.
