"""Put secrets into webhook-sync's .env without showing them on screen.

  python scripts/set_env.py google <path to the service-account .json>
  python scripts/set_env.py firecrawl
  python scripts/set_env.py show          (names only, never values)
  python scripts/set_env.py clip          (copies the whole .env to the clipboard, for the cloud settings box)
  python scripts/set_env.py github        (copies the weekly job's secrets to the repository's Actions secrets)
"""
import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys

ENV = pathlib.Path(os.environ.get("WSYNC_ENV_FILE") or pathlib.Path(__file__).resolve().parent.parent / ".env")


def read_lines():
    return ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []


def set_value(name, value):
    lines = [x for x in read_lines() if not x.startswith(name + "=")]
    lines.append(f"{name}={value}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{name}: written to {ENV} ({len(value)} characters)")


def env_values():
    values = {}
    for line in read_lines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            value = value.strip()
            if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[name.strip()] = value
    return values


# The test-mode values .env.example names that the weekly real run needs, and the demo
# spreadsheet's id (Q176). FIRECRAWL_API_KEY is for research only and stays local.
GITHUB_SECRETS = (
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET",
    "GOOGLE_SERVICE_ACCOUNT_B64",
    "SHEET_ID",
)
# Named, not taken from the current folder: the .env is found from this file, so the command
# runs from anywhere, and gh would otherwise set the secrets on whichever repository that is.
REPO = "lalitkarthik/webhook-sync"


def to_github():
    """Each of GITHUB_SECRETS from .env to REPO's Actions secrets, with
    `gh secret set NAME` reading the value from standard input. Prints names only. Refuses,
    before setting anything, a missing value or a Razorpay key that is not a test key."""
    values = env_values()
    missing = [name for name in GITHUB_SECRETS if not values.get(name)]
    if missing:
        sys.exit(f"{ENV} does not set {', '.join(missing)}: nothing was copied.")
    if not values["RAZORPAY_KEY_ID"].startswith("rzp_test_"):
        sys.exit("RAZORPAY_KEY_ID does not start rzp_test_: only test-mode keys go to CI. Nothing was copied.")
    gh = shutil.which("gh")
    if gh is None:
        sys.exit("gh is not installed or not on PATH: nothing was copied.")
    for name in GITHUB_SECRETS:
        done = subprocess.run(
            [gh, "secret", "set", name, "--repo", REPO], input=values[name], text=True, capture_output=True
        )
        if done.returncode != 0:
            sys.exit(f"{name}: gh secret set failed (exit {done.returncode}): {done.stderr.strip()}")
        print(f"{name}: set in {REPO}'s Actions secrets")


def drop(name):
    lines = [x for x in read_lines() if not x.startswith(name + "=")]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "google":
        if len(sys.argv) < 3:
            sys.exit("Give the path to the downloaded key file.")
        data = json.load(open(sys.argv[2], encoding="utf-8"))
        if data.get("type") != "service_account" or "private_key" not in data:
            sys.exit("That file is not a service-account key.")
        packed = base64.b64encode(json.dumps(data).encode("utf-8")).decode("ascii")
        drop("GOOGLE_SERVICE_ACCOUNT_JSON")  # the old name, or a path pasted by hand
        set_value("GOOGLE_SERVICE_ACCOUNT_B64", packed)
        print("Share your sheet (Editor) with:", data.get("client_email"))
    elif cmd == "firecrawl":
        cfg = json.loads((pathlib.Path.home() / ".claude.json").read_text(encoding="utf-8"))
        set_value("FIRECRAWL_API_KEY", cfg["mcpServers"]["firecrawl"]["env"]["FIRECRAWL_API_KEY"])
    elif cmd == "show":
        for line in read_lines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, value = line.split("=", 1)
                print(f"{name}: {'set' if value.strip() else 'EMPTY'} ({len(value.strip())} characters)")
    elif cmd == "clip":
        text = "\n".join(x for x in read_lines() if "=" in x and not x.lstrip().startswith("#")) + "\n"
        subprocess.run("clip", input=text.encode("utf-16"), check=True)
        print("The .env is on the clipboard. Paste it into the cloud environment's variables box, then copy something else.")
    elif cmd == "github":
        to_github()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
