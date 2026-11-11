"""Admin dashboard — edit .env/credentials and control the bot/pipeline
for the MentahanPOV production deployment.

Runs as its own process on the host (NOT inside the `mentahanpov-app`
Docker image), separate from `dashboard.py` (the read-only status page,
which runs containerized with read-only mounts on purpose — see
DEPLOYMENT.md). This one needs to write `.env`/`credentials/` and issue
`docker compose` commands, neither of which a container can do without
either bind-mounting the whole repo read-write and the Docker socket
into it, or just running on the host directly. Host process is simpler
and doesn't touch the existing, already-hardened read-only container.

Entry point is the repo-root `admin.py`. See DEPLOYMENT.md -> "Admin
dashboard" for how this is actually run in production (systemd unit,
not docker-compose).
"""
