"""Command-line interface for the cq multi-host installer."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cq_install.context import Action, ChangeResult, InstallContext, RunState
from cq_install.hosts import REGISTRY, get_host
from cq_install.hosts.base import HostDef


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    plugin_root = _resolve_plugin_root()

    try:
        targets = [get_host(name) for name in args.target]
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.host_isolated_skills:
        unsupported = [host.name for host in targets if not host.supports_host_isolated]
        if unsupported:
            names = ", ".join(unsupported)
            print(
                f"error: --host-isolated-skills is not supported for host {names}",
                file=sys.stderr,
            )
            return 2

    return _run(args.action, targets, args, plugin_root)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cq_install",
        description="Multi-host installer for the cq plugin.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("install", "uninstall"):
        sp = sub.add_parser(name)
        sp.add_argument(
            "--target",
            action="append",
            required=True,
            choices=sorted(REGISTRY),
            help="host to (un)install. Repeat for multi-target runs.",
        )
        scope = sp.add_mutually_exclusive_group(required=False)
        scope.add_argument("--global", dest="globally", action="store_true")
        scope.add_argument("--project", type=Path, default=None)
        sp.add_argument(
            "--host-isolated-skills",
            action="store_true",
            help="copy skills into the host's private skills dir instead of the shared commons.",
        )
        sp.add_argument("--dry-run", action="store_true")
    return parser


def _print_results(host_name: str, results: list[ChangeResult]) -> None:
    marker = {
        Action.CREATED: "+",
        Action.UPDATED: "~",
        Action.UNCHANGED: "=",
        Action.REMOVED: "-",
        Action.SKIPPED: "!",
    }
    print(f"[{host_name}]")
    for r in results:
        suffix = f"  ({r.detail})" if r.detail else ""
        print(f"  {marker[r.action]} {r.path}{suffix}")


def _resolve_plugin_root() -> Path:
    override = os.environ.get("CQ_INSTALL_PLUGIN_ROOT")
    if override:
        resolved = Path(override).resolve()
        if not resolved.is_dir():
            raise SystemExit(
                f"CQ_INSTALL_PLUGIN_ROOT={override!r} does not point at a "
                f"directory (resolved to {resolved}). When installing from a "
                f"release tarball, set CQ_INSTALL_PLUGIN_ROOT to "
                f"<extract-dir>/plugins/cq."
            )
        return resolved
    # Source-tree fallback. Walks up to:
    #   scripts/install/src/cq_install/cli.py
    #     parents[0] = cq_install/
    #     parents[1] = src/
    #     parents[2] = install/
    #     parents[3] = scripts/
    #     parents[4] = repo root
    # If you're running from an UNPACKED RELEASE TARBALL (where the path
    # above won't exist), set CQ_INSTALL_PLUGIN_ROOT explicitly.
    fallback = (Path(__file__).resolve().parents[4] / "plugins" / "cq").resolve()
    if not fallback.is_dir():
        raise SystemExit(
            f"Could not locate plugins/cq directory. The source-tree fallback "
            f"({fallback}) does not exist, and CQ_INSTALL_PLUGIN_ROOT was not "
            f"set. When invoked from an unpacked release tarball, set "
            f"CQ_INSTALL_PLUGIN_ROOT=<extract-dir>/plugins/cq."
        )
    return fallback


def _resolve_target(host: HostDef, args: argparse.Namespace) -> Path:
    if args.project is not None:
        if not host.supports_project:
            raise ValueError(f"--project is not supported for host {host.name!s}")
        return host.project_target(args.project)
    return host.global_target()


def _run(
    action: str,
    targets: list[HostDef],
    args: argparse.Namespace,
    plugin_root: Path,
) -> int:
    run_state = RunState()
    for host in targets:
        try:
            target_dir = _resolve_target(host, args)
        except ValueError as exc:
            print(f"error: {host.name}: {exc}", file=sys.stderr)
            return 2

        ctx = InstallContext(
            target=target_dir,
            plugin_root=plugin_root,
            shared_skills_path=_shared_skills_path(args),
            host_isolated_skills=args.host_isolated_skills,
            dry_run=args.dry_run,
            run_state=run_state,
        )
        try:
            results = host.install(ctx) if action == "install" else host.uninstall(ctx)
        except NotImplementedError as exc:
            print(f"error: {host.name}: {exc}", file=sys.stderr)
            return 1
        _print_results(host.name, results)
    return 0


def _shared_skills_path(args: argparse.Namespace) -> Path:
    if args.project is not None:
        return args.project / ".agents" / "skills"
    return Path.home() / ".agents" / "skills"
