"""family-budget CLI — the single entrypoint the Helm chart invokes.

Usage: app {serve|worker|web|migrate|backup}
"""
import os
import sys


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"

    if cmd == "serve":
        import uvicorn
        uvicorn.run("app.api:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
    elif cmd == "web":
        import uvicorn
        uvicorn.run("app.web:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
    elif cmd == "worker":
        from app.worker import run
        run()
    elif cmd == "migrate":
        from app.migrate import run
        run()
    elif cmd == "backup":
        from app.backup import run
        run()
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
