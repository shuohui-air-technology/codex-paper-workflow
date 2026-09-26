"""Launch a token-protected, loopback-only Workflow Studio session."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import webbrowser
from pathlib import Path

if __package__ in {None, ""}:
    # Direct-file execution sets sys.path to ``scripts/`` rather than the
    # installed Orchestrator root, so add the root before absolute imports.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.workflow_engine.studio_server import StudioConfig, create_server


_ROOT = Path(__file__).resolve().parents[1]


def _project_root(raw: str) -> Path:
    supplied = Path(raw).expanduser()
    if supplied.is_symlink():
        raise ValueError("Project root must not be a symlink or reparse point")
    try:
        root = supplied.resolve(strict=True)
    except OSError as exc:
        raise ValueError("Project root must be an existing directory") from exc
    if not root.is_dir():
        raise ValueError("Project root must be an existing directory")
    return root


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open the local browser-based Workflow Studio."
    )
    parser.add_argument("--project", required=True, help="research project directory")
    parser.add_argument(
        "--skills-root",
        action="append",
        default=[],
        help="additional root containing installed Skills (repeatable)",
    )
    parser.add_argument("--no-browser", action="store_true", help="print the session URL without opening a browser")
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=900,
        help="stop the local server after this many idle seconds (default: 900)",
    )
    parser.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    return parser


def main(argv=None, *, asset_root_override: Path | None = None) -> int:
    """Create a local session, print one JSON start record, and serve until exit."""
    args = _parser().parse_args(argv)
    try:
        project_root = _project_root(args.project)
        asset_root = (
            Path(asset_root_override).expanduser()
            if asset_root_override is not None
            else _ROOT / "assets" / "workflow-studio"
        )
        config = StudioConfig(
            project_root=project_root,
            asset_root=asset_root,
            host="127.0.0.1",
            port=args.port,
            session_token=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            idle_timeout_seconds=args.idle_timeout,
            open_browser=not args.no_browser,
            skill_roots=tuple(Path(item).expanduser() for item in args.skills_root),
        )
        server = create_server(config)
    except (OSError, ValueError) as exc:
        # Startup failures are machine-readable but intentionally omit local paths.
        sys.stderr.write(json.dumps({
            "status": "error",
            "error": {"code": "studio.startup_failed", "message": str(exc)[:500]},
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        return 2

    port = server.server_address[1]
    session_url = f"http://127.0.0.1:{port}/#session={config.session_token}"
    sys.stdout.write(json.dumps(
        {"status": "ready", "url": session_url, "port": port},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n")
    sys.stdout.flush()
    if config.open_browser:
        try:
            webbrowser.open(session_url)
        except Exception:
            # The printed URL remains usable when the system browser cannot open.
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
