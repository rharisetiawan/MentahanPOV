"""Flask app for the MentahanPOV dashboard.

Routes are grouped by tab:
    /config        — edit every .env value via a schema-driven form
    /credentials   — upload/replace/delete the token & secret files
    /status        — read-only view of state/posts.json
    /bot           — start/stop telegram_bot.py, live log
    /run           — trigger main.py on a video, live log

Run via the repo-root `dashboard.py`, not this module directly.
"""

from __future__ import annotations

import json
import logging
import secrets
import sys
from pathlib import Path

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from config import config as pipeline_config

from . import creds, env_schema, env_store, jobs

log = logging.getLogger("mentahanpov.dashboard")

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"
ENV_FILE = REPO_ROOT / ".env"
INCOMING_DIR = REPO_ROOT / "incoming"
RUN_LOG_DIR = REPO_ROOT / "state" / "dashboard_logs"
BOT_LOG = RUN_LOG_DIR / "telegram_bot.log"
PIPELINE_LOG = RUN_LOG_DIR / "pipeline.log"

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
    # A fresh random key each launch is fine here: the dashboard is a local
    # tool you restart rarely, and losing the session just means logging
    # back in — nothing else depends on it surviving a restart.
    app.secret_key = secrets.token_hex(32)

    def login_required(view):
        from functools import wraps

        @wraps(view)
        def wrapped(*args, **kwargs):
            if pipeline_config.dashboard_password and not session.get("authed"):
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    @app.context_processor
    def inject_globals():
        return {
            "authed": bool(session.get("authed")) or not pipeline_config.dashboard_password,
            "has_password": bool(pipeline_config.dashboard_password),
        }

    # ---- auth -----------------------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not pipeline_config.dashboard_password:
            return redirect(url_for("config_page"))
        if request.method == "POST":
            if request.form.get("password") == pipeline_config.dashboard_password:
                session["authed"] = True
                return redirect(request.args.get("next") or url_for("config_page"))
            flash("Password salah.", "error")
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---- config -----------------------------------------------------------

    @app.route("/")
    @login_required
    def index():
        return redirect(url_for("config_page"))

    @app.route("/config", methods=["GET", "POST"])
    @login_required
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
                "Tersimpan ke .env. Restart bot / jalankan ulang pipeline biar "
                "perubahan kepakai.",
                "success",
            )
            return redirect(url_for("config_page"))

        values = _values()
        return render_template("config.html", sections=sections, values=values)

    # ---- credentials --------------------------------------------------

    @app.route("/credentials")
    @login_required
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
    @login_required
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
    @login_required
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
    @login_required
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

    # ---- bot control ------------------------------------------------------

    @app.route("/bot")
    @login_required
    def bot_page():
        job = jobs.get("bot")
        status = job.status() if job else {"running": False, "returncode": None, "log": ""}
        return render_template("bot.html", status=status)

    @app.route("/bot/start", methods=["POST"])
    @login_required
    def bot_start():
        if not pipeline_config.telegram_bot_token:
            flash("TELEGRAM_BOT_TOKEN kosong — isi dulu di tab Config.", "error")
            return redirect(url_for("bot_page"))
        try:
            jobs.start("bot", jobs.python_args("telegram_bot.py"), BOT_LOG)
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("bot_page"))

    @app.route("/bot/stop", methods=["POST"])
    @login_required
    def bot_stop():
        jobs.stop("bot")
        return redirect(url_for("bot_page"))

    @app.route("/bot/log.json")
    @login_required
    def bot_log_json():
        job = jobs.get("bot")
        return jsonify(job.status() if job else {"running": False, "returncode": None, "log": ""})

    # ---- run pipeline -----------------------------------------------------

    @app.route("/run")
    @login_required
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
    @login_required
    def run_start():
        file = request.files.get("video")
        video_path_input = request.form.get("video_path", "").strip()

        if file and file.filename:
            INCOMING_DIR.mkdir(parents=True, exist_ok=True)
            dest = INCOMING_DIR / secure_filename(file.filename)
            file.save(str(dest))
            video_path = dest
        elif video_path_input:
            video_path = Path(video_path_input)
        else:
            flash("Upload video atau isi path file-nya dulu.", "error")
            return redirect(url_for("run_page"))

        if not video_path.exists():
            flash(f"File tidak ditemukan: {video_path}", "error")
            return redirect(url_for("run_page"))

        args = jobs.python_args("main.py", str(video_path))
        selected = request.form.getlist("platforms")
        if selected:
            args += ["--platforms", ",".join(selected)]
        for flag in ("dry_run", "skip_gdrive", "skip_gemini", "skip_watermark", "force"):
            if request.form.get(flag):
                args.append("--" + flag.replace("_", "-"))
        args.append("-v")

        try:
            jobs.start("pipeline", args, PIPELINE_LOG)
            flash(f"Pipeline dimulai untuk {video_path.name}.", "success")
        except RuntimeError as exc:
            flash(str(exc), "error")
        return redirect(url_for("run_page"))

    @app.route("/run/stop", methods=["POST"])
    @login_required
    def run_stop():
        jobs.stop("pipeline")
        return redirect(url_for("run_page"))

    @app.route("/run/log.json")
    @login_required
    def run_log_json():
        job = jobs.get("pipeline")
        return jsonify(job.status() if job else {"running": False, "returncode": None, "log": ""})

    return app


if __name__ == "__main__":
    # Convenience for `python -m dashboard.app` during development; the
    # documented entry point is the repo-root dashboard.py.
    logging.basicConfig(level=logging.INFO)
    create_app().run(
        host=pipeline_config.dashboard_host,
        port=pipeline_config.dashboard_port,
        debug=False,
    )
    sys.exit(0)
