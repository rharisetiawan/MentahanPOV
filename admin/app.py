"""Flask app for the MentahanPOV admin dashboard (edit .env/credentials,
control the bot, trigger a run) — see `admin/__init__.py` for why this
runs as a separate host process instead of living inside the read-only
`dashboard.py` container.

Auth reuses the exact same HTTP Basic Auth pattern as `dashboard.py`
(DASHBOARD_USER/DASHBOARD_PASSWORD from config.py) rather than inventing
a second credential to manage.

Routes:
    /config        — edit every .env value via a schema-driven form
    /credentials   — upload/replace/delete the token & secret files
    /status        — read-only view of state/posts.json
    /bot           — start/stop the mentahanpov-bot container, live log
    /run           — trigger main.py on a video inside a throwaway
                      container (`docker compose run --rm ...`), live log
"""

from __future__ import annotations

import json
import logging
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.utils import secure_filename

from config import config as pipeline_config

from . import creds, env_schema, env_store, jobs

log = logging.getLogger("mentahanpov.admin")

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"
ENV_FILE = REPO_ROOT / ".env"
INCOMING_DIR = REPO_ROOT / "incoming"

# Path-kind fields whose value points at a token/secret file rather than a
# plain directory — these get an upload widget on the Credentials tab.
CREDENTIAL_SUFFIXES = ("_FILE", "_SECRETS", "_STATE")


def _sections() -> list[env_schema.Section]:
    return env_schema.parse(ENV_EXAMPLE)


def _values() -> dict[str, str]:
    return env_store.read_current(ENV_FILE)


def _credential_fields(sections: list[env_schema.Section]) -> list[tuple[str, str]]:
    return [
        (f.key, f.comment or f.key)
        for f in env_schema.all_fields(sections)
        if f.kind == "path" and f.key.endswith(CREDENTIAL_SUFFIXES)
    ]


def create_app() -> Flask:
    app = Flask(__name__)

    def require_auth(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            auth = request.authorization
            if not pipeline_config.dashboard_password:
                log.warning("[admin] DASHBOARD_PASSWORD is empty — refusing to serve")
                return Response("Admin dashboard misconfigured: DASHBOARD_PASSWORD is empty.", 500)
            if (
                not auth
                or auth.username != pipeline_config.dashboard_user
                or auth.password != pipeline_config.dashboard_password
            ):
                return Response(
                    "Login required.",
                    401,
                    {"WWW-Authenticate": 'Basic realm="MentahanPOV Admin"'},
                )
            return view(*args, **kwargs)

        return wrapped

    # ---- config -----------------------------------------------------------

    @app.route("/")
    @require_auth
    def index():
        return redirect(url_for("config_page"))

    @app.route("/config", methods=["GET", "POST"])
    @require_auth
    def config_page():
        sections = _sections()
        if request.method == "POST":
            values = _values()
            for f in env_schema.all_fields(sections):
                if f.kind == "bool":
                    values[f.key] = "true" if request.form.get(f.key) else "false"
                elif f.key in request.form:
                    values[f.key] = request.form.get(f.key, "").strip()
            env_store.write(ENV_FILE, sections, values)
            flash(
                "Tersimpan ke .env. Klik 'Terapkan' di tab Bot biar container-nya "
                "kebaca .env yang baru (env_file dibaca cuma pas container dibuat).",
                "success",
            )
            return redirect(url_for("config_page"))

        values = _values()
        return render_template("config.html", sections=sections, values=values)

    # ---- credentials --------------------------------------------------

    @app.route("/credentials")
    @require_auth
    def credentials_page():
        sections = _sections()
        fields = _credential_fields(sections)
        values = _values()
        merged = {}
        for key, _label in fields:
            f = env_schema.find_field(sections, key)
            merged[key] = values.get(key) or (f.default if f else "")
        files = creds.list_credential_files(fields, merged, REPO_ROOT)
        return render_template("credentials.html", files=files)

    @app.route("/credentials/upload/<key>", methods=["POST"])
    @require_auth
    def credentials_upload(key: str):
        sections = _sections()
        field = env_schema.find_field(sections, key)
        if field is None or field.kind != "path":
            flash("Field tidak dikenal.", "error")
            return redirect(url_for("credentials_page"))

        values = _values()
        dest = creds.resolve(values.get(key) or field.default, REPO_ROOT)
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Pilih file dulu.", "error")
            return redirect(url_for("credentials_page"))

        creds.save_upload(dest, file)
        flash(f"{dest.name} diupload ke {dest.parent}/.", "success")
        return redirect(url_for("credentials_page"))

    @app.route("/credentials/delete/<key>", methods=["POST"])
    @require_auth
    def credentials_delete(key: str):
        sections = _sections()
        field = env_schema.find_field(sections, key)
        if field is None:
            return redirect(url_for("credentials_page"))
        values = _values()
        dest = creds.resolve(values.get(key) or field.default, REPO_ROOT)
        creds.delete_file(dest)
        flash(f"{dest.name} dihapus.", "success")
        return redirect(url_for("credentials_page"))

    # ---- status / history -----------------------------------------------

    @app.route("/status")
    @require_auth
    def status_page():
        state_path = pipeline_config.state_file
        if not state_path.is_absolute():
            state_path = REPO_ROOT / state_path
        entries = []
        if state_path.exists():
            try:
                data = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            entries = [
                {"key": k, **v}
                for k, v in sorted(
                    data.items(),
                    key=lambda kv: kv[1].get("updated_at", ""),
                    reverse=True,
                )
            ]
        return render_template("status.html", entries=entries, state_path=state_path)

    # ---- bot control (docker compose) --------------------------------------

    @app.route("/bot")
    @require_auth
    def bot_page():
        ps = jobs.run_compose("ps", "--status=running", "--services", timeout=20)
        running = "mentahanpov-bot" in ps.stdout.split()
        logs = jobs.run_compose("logs", "--no-color", "--tail", "300", "mentahanpov-bot", timeout=20)
        return render_template("bot.html", status={"running": running, "log": logs.stdout + logs.stderr})

    @app.route("/bot/start", methods=["POST"])
    @require_auth
    def bot_start():
        result = jobs.run_compose("up", "-d", "mentahanpov-bot", timeout=120)
        if result.returncode != 0:
            flash(f"Gagal start: {result.stderr[-500:]}", "error")
        else:
            flash("Bot dimulai (docker compose up -d mentahanpov-bot).", "success")
        return redirect(url_for("bot_page"))

    @app.route("/bot/stop", methods=["POST"])
    @require_auth
    def bot_stop():
        result = jobs.run_compose("stop", "mentahanpov-bot", timeout=60)
        if result.returncode != 0:
            flash(f"Gagal stop: {result.stderr[-500:]}", "error")
        else:
            flash("Bot dihentikan.", "success")
        return redirect(url_for("bot_page"))

    @app.route("/bot/apply", methods=["POST"])
    @require_auth
    def bot_apply():
        # `docker compose up -d` (no --build) recreates any service whose
        # env/config changed, picking up the latest .env — env_file is
        # only read at container *creation*, per DEPLOYMENT.md, so saving
        # .env alone never reaches an already-running container.
        result = jobs.run_compose("up", "-d", timeout=180)
        if result.returncode != 0:
            flash(f"Gagal apply: {result.stderr[-800:]}", "error")
        else:
            flash("Diterapkan — semua service di-recreate dengan .env terbaru.", "success")
        return redirect(url_for("bot_page"))

    @app.route("/bot/log.json")
    @require_auth
    def bot_log_json():
        ps = jobs.run_compose("ps", "--status=running", "--services", timeout=20)
        running = "mentahanpov-bot" in ps.stdout.split()
        logs = jobs.run_compose("logs", "--no-color", "--tail", "300", "mentahanpov-bot", timeout=20)
        return jsonify({"running": running, "log": logs.stdout + logs.stderr})

    # ---- run pipeline (docker compose run --rm) ----------------------------

    @app.route("/run")
    @require_auth
    def run_page():
        from main import PLATFORM_REGISTRY

        job = jobs.get("pipeline")
        status = job.status() if job else {"running": False, "returncode": None, "log": ""}
        return render_template(
            "run.html",
            platforms=sorted(PLATFORM_REGISTRY),
            default_platforms=set(pipeline_config.platforms),
            status=status,
        )

    @app.route("/run/start", methods=["POST"])
    @require_auth
    def run_start():
        file = request.files.get("video")
        video_path_input = request.form.get("video_path", "").strip()

        if file and file.filename:
            INCOMING_DIR.mkdir(parents=True, exist_ok=True)
            filename = secure_filename(file.filename)
            file.save(str(INCOMING_DIR / filename))
        elif video_path_input:
            filename = Path(video_path_input).name
            if not (INCOMING_DIR / filename).exists():
                flash(f"File gak ketemu di incoming/: {filename}", "error")
                return redirect(url_for("run_page"))
        else:
            flash("Upload video atau isi nama file yang sudah ada di incoming/.", "error")
            return redirect(url_for("run_page"))

        # Host path -> the path this same file has *inside* the container,
        # via docker-compose.yml's `./incoming:/app/incoming` mount.
        container_path = f"/app/incoming/{filename}"

        args = jobs.compose_args(
            "run", "--rm", "--no-deps", "mentahanpov-bot", "python", "main.py", container_path
        )
        selected = request.form.getlist("platforms")
        if selected:
            args += ["--platforms", ",".join(selected)]
        for flag in ("dry_run", "skip_gdrive", "skip_gemini", "skip_watermark", "force"):
            if request.form.get(flag):
                args.append("--" + flag.replace("_", "-"))
        args.append("-v")

        try:
            jobs.start("pipeline", args)
            flash(f"Pipeline dimulai untuk {filename}.", "success")
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("run_page"))

    @app.route("/run/stop", methods=["POST"])
    @require_auth
    def run_stop():
        jobs.stop("pipeline")
        return redirect(url_for("run_page"))

    @app.route("/run/log.json")
    @require_auth
    def run_log_json():
        job = jobs.get("pipeline")
        return jsonify(job.status() if job else {"running": False, "returncode": None, "log": ""})

    return app
