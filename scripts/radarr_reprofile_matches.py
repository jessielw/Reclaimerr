"""Move a match-only rule's movies onto another Radarr quality profile.

Example script for docs/usage/scripting-arr-changes.md. Standard library only.

Dry run by default; pass --apply to change Radarr.

    $env:RECLAIMERR = "http://localhost:8000"
    $env:RECLAIMERR_TOKEN = "rcl_..."
    $env:RADARR = "http://localhost:7878"
    $env:RADARR_API_KEY = "..."

    python scripts/radarr_reprofile_matches.py --lookup --rule-id 12
    python scripts/radarr_reprofile_matches.py --rule-id 12 --radarr-config-id 1 --profile-id 4 --tag-id 3
    python scripts/radarr_reprofile_matches.py --rule-id 12 --radarr-config-id 1 --profile-id 4 --tag-id 3 --apply
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

# every matches request is a full rule evaluation, which can be slow
MATCHES_TIMEOUT = 600
ARR_TIMEOUT = 60


def request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: Any = None,
    timeout: int = ARR_TIMEOUT,
) -> Any:
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
    except urllib.error.HTTPError as e:
        sys.exit(
            f"{method} {url} failed: HTTP {e.code} {e.read().decode(errors='replace')}"
        )
    except urllib.error.URLError as e:
        sys.exit(f"{method} {url} failed: {e.reason}")
    return json.loads(raw) if raw else None


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Set the {name} environment variable")
    return value.rstrip("/")


def fetch_matches(rule_id: int) -> dict[str, Any]:
    return request(
        "GET",
        f"{env('RECLAIMERR')}/api/v1/rules/{rule_id}/matches",
        {"Authorization": f"Bearer {env('RECLAIMERR_TOKEN')}"},
        timeout=MATCHES_TIMEOUT,
    )


def radarr(method: str, path: str, body: Any = None) -> Any:
    return request(
        method,
        f"{env('RADARR')}/api/v3/{path}",
        {"X-Api-Key": env("RADARR_API_KEY")},
        body,
    )


def lookup(rule_id: int | None) -> None:
    """Print the IDs the other options need."""
    rules = request(
        "GET",
        f"{env('RECLAIMERR')}/api/v1/rules",
        {"Authorization": f"Bearer {env('RECLAIMERR_TOKEN')}"},
    )
    print("Match-only rules:")
    for rule in rules["items"]:
        if rule["outcome"] == "match":
            state = "enabled" if rule["enabled"] else "disabled"
            print(
                f"  {rule['id']:>4}  {rule['name']} ({rule['target_scope']}, {state})"
            )

    if rule_id is not None:
        matches = fetch_matches(rule_id)
        instances = {
            ref["service_config_id"]: ref
            for item in matches["items"]
            for ref in item["arr"]
            if ref["service"] == "radarr"
        }
        print(f"\nRadarr instances in rule {rule_id}'s matches:")
        for config_id, ref in sorted(instances.items()):
            print(f"  {config_id:>4}  {ref['service_name'] or '(unnamed)'}")

    print("\nRadarr quality profiles:")
    for profile in radarr("GET", "qualityprofile"):
        print(f"  {profile['id']:>4}  {profile['name']}")
    print("\nRadarr tags:")
    for tag in radarr("GET", "tag"):
        print(f"  {tag['id']:>4}  {tag['label']}")


def reprofile(args: argparse.Namespace) -> None:
    matches = fetch_matches(args.rule_id)
    if not matches["complete"]:
        print("Incomplete data, skipping this run:", file=sys.stderr)
        for warning in matches["warnings"]:
            print(f"  {warning}", file=sys.stderr)
        sys.exit(1)

    # a movie with several matching versions appears once per version
    titles: dict[int, str] = {}
    for item in matches["items"]:
        for ref in item["arr"]:
            if (
                ref["service"] == "radarr"
                and ref["service_config_id"] == args.radarr_config_id
            ):
                titles[ref["arr_id"]] = f"{item['title']} ({item['year']})"

    print(
        f"Rule {matches['rule']['name']!r}: {matches['total']} match(es), "
        f"{len(titles)} movie(s) in Radarr instance {args.radarr_config_id}"
    )
    if not titles:
        return
    for arr_id, title in sorted(titles.items(), key=lambda pair: pair[1]):
        print(f"  {arr_id:>6}  {title}")

    if not args.apply:
        print("\nDry run. Pass --apply to change Radarr.")
        return

    movie_ids = sorted(titles)
    editor: dict[str, Any] = {
        "movieIds": movie_ids,
        "qualityProfileId": args.profile_id,
    }
    if args.tag_id is not None:
        editor["tags"] = [args.tag_id]
        editor["applyTags"] = "add"
    radarr("PUT", "movie/editor", editor)
    print(f"\nMoved {len(movie_ids)} movie(s) to quality profile {args.profile_id}")

    if args.search:
        radarr("POST", "command", {"name": "MoviesSearch", "movieIds": movie_ids})
        print("Queued a search for them")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rule-id", type=int, help="match-only rule ID")
    parser.add_argument(
        "--radarr-config-id",
        type=int,
        help="service_config_id of the Radarr instance in Reclaimerr",
    )
    parser.add_argument(
        "--profile-id", type=int, help="target Radarr quality profile ID"
    )
    parser.add_argument(
        "--tag-id",
        type=int,
        help="Radarr tag to add; pair it with an 'Arr tags does not contain' rule condition",
    )
    parser.add_argument(
        "--search", action="store_true", help="queue a search after the change"
    )
    parser.add_argument(
        "--apply", action="store_true", help="change Radarr (default is a dry run)"
    )
    parser.add_argument(
        "--lookup",
        action="store_true",
        help="print rule, instance, profile, and tag IDs",
    )
    args = parser.parse_args()

    if args.lookup:
        lookup(args.rule_id)
        return
    missing = [
        flag
        for flag, value in (
            ("--rule-id", args.rule_id),
            ("--radarr-config-id", args.radarr_config_id),
            ("--profile-id", args.profile_id),
        )
        if value is None
    ]
    if missing:
        parser.error(f"missing {', '.join(missing)} (run with --lookup to find them)")
    reprofile(args)


if __name__ == "__main__":
    main()
