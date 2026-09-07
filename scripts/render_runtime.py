#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from job_scout.portability import RuntimePaths, render_template


def _reject_symlink_ancestors(path: Path) -> None:
    current = path
    while True:
        if os.path.lexists(current) and current.is_symlink():
            raise ValueError(f"runtime destination contains a symlink: {current}")
        if current == current.parent:
            return
        current = current.parent


def _atomic_write_text(destination: Path, text: str, mode: int) -> None:
    _reject_symlink_ancestors(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink_ancestors(destination)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.render-",
            delete=False,
            mode="w",
            encoding="utf-8",
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, destination)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render host-specific private runtime files")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--skill-template", type=Path, default=ROOT / "skill/continuous-job-search/SKILL.example.md")
    parser.add_argument("--prompt-template", type=Path, default=ROOT / "cron/prompt.example.md")
    args = parser.parse_args()

    paths = RuntimePaths.from_environment(
        ROOT,
        home=args.home,
        environment=os.environ,
    )
    profile = json.loads(paths.search_profile.read_text(encoding="utf-8"))
    candidate = profile.get("candidate") if isinstance(profile, dict) else None
    name = candidate.get("name") if isinstance(candidate, dict) else None
    if not isinstance(name, str) or not name.strip() or name == "Candidate Name":
        raise ValueError("configure candidate.name in search-profile.json before rendering")
    values = {
        "AUTOMATION": str(paths.automation),
        "WORKSPACE": str(paths.workspace),
        "RESUME": str(paths.resume),
        "TECTONIC": str(paths.tectonic),
        "CANDIDATE_NAME": name.strip(),
    }
    destinations = (
        (args.skill_template, paths.skill),
        (args.prompt_template, paths.workspace / "cron-prompt.md"),
    )
    for source, destination in destinations:
        rendered = render_template(source.read_text(encoding="utf-8"), values)
        _atomic_write_text(destination, rendered, 0o600)
        print(f"Rendered {destination}")
    launcher = paths.hermes_home / "scripts/job-scout-shadow.py"
    launcher.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    launcher_text = """#!/usr/bin/env python3
import os
import runpy

os.environ.setdefault("JOB_SEARCH_AUTOMATION", {automation!r})
os.environ.setdefault("JOB_SEARCH_WORKDIR", {workspace!r})
runpy.run_path({entrypoint!r}, run_name="__main__")
""".format(
        automation=str(paths.automation),
        workspace=str(paths.workspace),
        entrypoint=str(paths.automation / "scripts/live_scout_input.py"),
    )
    _atomic_write_text(launcher, launcher_text, 0o700)
    print(f"Rendered {launcher}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
