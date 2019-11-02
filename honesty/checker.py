import difflib
import hashlib
import time
from pathlib import Path
from typing import Dict, List, Set, Tuple

import arlib
import click

from .cache import fetch
from .releases import SDIST_EXTENSIONS, FileEntry, FileType, Package

ZIP_EXTENSIONS = (".zip", ".egg", ".whl")


def run_checker(package: Package, version: str, verbose: bool) -> int:
    try:
        rel = package.releases[version]
    except KeyError:
        raise click.ClickException(f"version={version} not available")

    sdists = [f for f in rel.files if f.file_type == FileType.SDIST]

    if not sdists:
        click.secho(f"{package.name} {version} no sdist", fg="red")
        return 1
    elif len(sdists) == len(rel.files):
        click.secho(f"{package.name} {version} only sdist", fg="green")
        return 0

    local_paths: List[Tuple[FileEntry, Path]] = []
    with click.progressbar(rel.files) as bar:
        for fe in bar:
            local_paths.append(
                (fe, fetch(pkg=package.name, filename=fe.basename, url=fe.url))
            )
            # TODO verify checksum

    # [filename][sha] = set(archives)
    paths_present: Dict[str, Dict[str, Set[str]]] = {}
    # [sha] = last path
    sdist_paths_present: Dict[str, str] = {}
    # import pdb
    # pdb.set_trace()
    for fe, lp in local_paths:
        if fe.file_type == FileType.UNKNOWN:
            continue

        is_sdist = fe.file_type == FileType.SDIST
        engine = arlib.ZipArchive if str(lp).endswith(ZIP_EXTENSIONS) else None
        with arlib.open(lp, "r", engine=engine) as archive:
            for name in archive.member_names:
                # archive.member_is_file is way too slow
                if "." not in name.split("/")[-1]:
                    continue

                # strips prefix of <pkg>-<ver>/
                namekey = name
                if is_sdist and "/" in name:
                    namekey = name.split("/", 1)[1]
                    if namekey.startswith("src/"):
                        namekey = namekey[4:]

                if name.endswith(".py"):
                    t0 = time.time()
                    with archive.open_member(name, "rb") as buf:
                        data = buf.read().replace(b"\r\n", b"\n")
                    t1 = time.time()
                    # print("Read", name, t1-t0)
                    sha = hashlib.sha1(data).hexdigest()
                    paths_present.setdefault(namekey, {}).setdefault(sha, set()).add(
                        lp.name
                    )
                    if is_sdist:
                        sdist_paths_present[sha] = namekey

    rc = 0
    for contained_path in sorted(paths_present):
        if len(paths_present[contained_path]) != 1:
            # different hashes
            click.secho(f"  {contained_path} different hashes", fg="red")
            if verbose:
                for k, v in paths_present[contained_path].items():
                    if k in sdist_paths_present:
                        for f in v:
                            click.secho(f"    {shorten(f)}: {k}", fg="yellow")
                    else:
                        for f in v:
                            click.secho(f"    {shorten(f)}: {k}", fg="red")
            rc |= 8
        elif next(iter(paths_present[contained_path])) not in sdist_paths_present:
            click.secho(f"  {contained_path} not in sdist", fg="red")
            if verbose:
                for k, v in paths_present[contained_path].items():
                    for f in v:
                        click.secho(f"    {shorten(f)}: {k}", fg="yellow")
            rc |= 4
        elif verbose:
            click.secho(f"  {contained_path}: OK", fg="green")

    if rc == 0:
        click.secho(f"{package.name} {version} OK", fg="green")
    else:
        # It's a little unusual to print this at the end, but otherwise we need
        # to buffer messages.  Open to other ideas.
        click.secho(f"{package.name} {version} problems", fg="yellow")

    return rc


def is_pep517(package: Package, version: str, verbose: bool) -> bool:
    try:
        rel = package.releases[version]
    except KeyError:
        raise click.ClickException(f"version={version} not available")

    # Find *a* sdist
    sdists = [f for f in rel.files if f.file_type == FileType.SDIST]
    if not sdists:
        raise click.ClickException(f"{package.name} no sdists")

    lp = fetch(pkg=package.name, filename=sdists[0].basename, url=sdists[0].url)

    engine = arlib.ZipArchive if str(lp).endswith(ZIP_EXTENSIONS) else None
    with arlib.open(lp, "r", engine=engine) as archive:
        for name in archive.member_names:
            # TODO for a couple of projects this is finding test fixtures, we
            # should only be looking alongside the rootmost setup.py
            if name.endswith("pyproject.toml"):
                with archive.open_member(name, "rb") as buf:
                    data = buf.read().replace(b"\r\n", b"\n")
                if b"[build-system]" in data:
                    click.echo(f"{package.name} build-system {name}")
                    return True
                else:
                    click.echo(f"{package.name} has-toml {name}")
    return False


def shorten(subj: str, n=50):
    if len(subj) <= n:
        return subj
    return subj[:22] + "..." + subj[-n + 22 + 3 :]


def show_diff(a: List[str], b: List[str]):
    click.echo("".join(difflib.unified_diff(a, b)))
