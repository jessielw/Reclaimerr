import subprocess
from collections.abc import Sequence


def run(args: Sequence[str], *, capture: bool = False) -> str:
    result = subprocess.run(  # noqa: S603
        args,
        check=True,
        capture_output=capture,
        text=True,
    )
    return result.stdout.strip() if capture else ""


def confirm() -> bool:
    response = input("\nContinue? [y/N]: ")
    return response.strip().lower() in {"y", "yes"}


def main() -> None:
    print("Fetching latest changes from origin...")
    run(["git", "fetch", "origin"])

    local_changes = run(
        ["git", "status", "--porcelain"],
        capture=True,
    )

    ahead_behind = run(
        [
            "git",
            "rev-list",
            "--left-right",
            "--count",
            "origin/main...origin/dev",
        ],
        capture=True,
    )

    behind, ahead = map(int, ahead_behind.split())

    content_diff = run(
        [
            "git",
            "diff",
            "--stat",
            "origin/main..origin/dev",
        ],
        capture=True,
    )

    print("\nBranch status:")
    print(f"  dev is {ahead} commit(s) ahead of main")
    print(f"  dev is {behind} commit(s) behind main")

    if content_diff:
        print("\nWARNING: dev and main have different file contents:")
        print(content_diff)
    else:
        print("\n✓ dev and main have identical file contents.")

    if local_changes:
        print("\nWARNING: You have uncommitted local changes:")
        print(local_changes)

    print(
        "\nThis will:\n"
        "  1. Switch to dev\n"
        "  2. Reset dev to origin/main\n"
        "  3. Force-push dev using --force-with-lease\n"
    )

    if ahead:
        print(
            f"WARNING: {ahead} commit(s) unique to dev will no longer "
            "be referenced by dev."
        )

    if local_changes:
        print("WARNING: Uncommitted changes may be permanently lost.")

    if not confirm():
        print("Aborted.")
        return

    print("\nSynchronizing dev...")
    run(["git", "switch", "dev"])
    run(["git", "reset", "--hard", "origin/main"])
    run(["git", "push", "--force-with-lease", "origin", "dev"])

    print("\n✓ dev is now synchronized with origin/main.")


if __name__ == "__main__":
    main()
