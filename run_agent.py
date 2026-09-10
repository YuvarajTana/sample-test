"""Absolute-path launcher for editors; does not depend on their working directory."""
from review_agent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
