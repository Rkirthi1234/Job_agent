"""One-time Wellfound login helper.

Run this script ONCE to open a headed Chromium window pointed at your
WELLFOUND_USER_DATA_DIR profile directory. Log in to Wellfound by hand,
then close the browser window. The session is saved to disk and the job
discovery adapter will reuse it automatically on every future run.

Usage::

    .venv\\Scripts\\python scripts\\wellfound_login.py

Prerequisites:
    - WELLFOUND_USER_DATA_DIR must be set in .env (or the environment).
      Example .env entry::

          WELLFOUND_USER_DATA_DIR=./wellfound_profile

    - Playwright must be installed::

          .venv\\Scripts\\pip install playwright
          .venv\\Scripts\\playwright install chromium
"""
import sys
from pathlib import Path

# Make sure the project root is on sys.path when run directly
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402


def main() -> None:
    settings = get_settings()
    user_data_dir = (settings.wellfound_user_data_dir or "").strip()
    if not user_data_dir:
        print(
            "\nERROR: WELLFOUND_USER_DATA_DIR is not set.\n"
            "Add it to your .env file, e.g.:\n\n"
            "    WELLFOUND_USER_DATA_DIR=./wellfound_profile\n"
        )
        sys.exit(1)

    profile_dir = Path(user_data_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nOpening Chromium with profile: {profile_dir.resolve()}")
    print("Log in to Wellfound, then close the browser window.\n")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright is not installed. Run:\n\n"
            "    .venv\\Scripts\\pip install playwright\n"
            "    .venv\\Scripts\\playwright install chromium\n"
        )
        sys.exit(1)

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            args=["--no-sandbox"],
        )
        page = context.new_page()
        page.goto("https://wellfound.com/login", wait_until="domcontentloaded")
        print("Browser is open. Log in now, then close the window when done.")
        # Block until the browser window is closed by the user
        context.wait_for_event("close", timeout=0)  # timeout=0 = wait forever

    print("\nDone! Session saved. The adapter will use it automatically.")


if __name__ == "__main__":
    main()
