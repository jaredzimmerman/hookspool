"""Release readiness (R2): one version everywhere, the workflows that build and publish, the docs site, the packages.

* ``hookspool.__version__`` is the version: pyproject reads it, ``hookspool --version`` prints it, it is the newest
  CHANGELOG.md release, and the changelog's compare links follow it
* ``.github/scripts/release_notes.py`` passes only a tag equal to ``v<version>`` of a final version with a dated section
* the workflows parse, run on the events they should, pin every action to a full commit SHA with its version in a
  comment, and hand out write access only where it is needed (the PyPI job alone may request an identity token in
  release.yml; the pull request jobs read)
* the docs site: its nav covers every page, links that leave docs/ go to GitHub, banners follow the palette, and
  ``mkdocs build --strict`` passes when mkdocs-material is installed
* ``python -m build`` makes an sdist and a wheel; the wheel carries every bobbin's files and, installed into a fresh
  environment, runs ``hookspool --version`` and ``hookspool bobbin list``; the sdist carries the docs and their images
"""
from __future__ import annotations

import glob
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _miniyaml  # noqa: E402

import hookspool  # noqa: E402
from hookspool import __main__ as cli  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
VERSION = hookspool.__version__
REPO = "https://github.com/jaredzimmerman/hookspool"


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def load_module(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


notes = load_module("release_notes", ".github/scripts/release_notes.py")
hooks = load_module("docs_site_hooks", "docs-site/hooks.py")


# ---- one version ----------------------------------------------------------------------------------------------------
def test_version_is_final_and_pyproject_reads_it():
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION), VERSION
    proj = tomllib.loads(read("pyproject.toml"))
    assert proj["project"]["dynamic"] == ["version"]
    assert proj["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "hookspool.__version__"}
    assert notes.package_version(ROOT) == VERSION


def test_cli_version_prints_the_package_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"hookspool {VERSION}"


def test_changelog_top_release_and_links_follow_the_version():
    text = read("CHANGELOG.md")
    heads = re.findall(r"^## \[([^\]]+)\](.*)$", text, re.M)
    assert heads[0] == ("Unreleased", ""), "an empty [Unreleased] stays on top"
    assert heads[1][0] == VERSION and re.fullmatch(r" - \d{4}-\d{2}-\d{2}", heads[1][1]), heads[1]
    assert notes.top_release(text) == VERSION
    assert f"[Unreleased]: {REPO}/compare/v{VERSION}...HEAD" in text
    assert re.search(rf"^\[{re.escape(VERSION)}\]: {re.escape(REPO)}/(compare/v[\d.]+\.\.\.|releases/tag/)"
                     rf"v{re.escape(VERSION)}$", text, re.M)


def test_release_notes_are_the_changelog_section():
    body = notes.check(f"v{VERSION}", ROOT)
    assert body.startswith("### Added") and "## [" not in body and f"[{VERSION}]:" not in body


def _fake_root(tmp_path, version: str, changelog: str):
    (tmp_path / "hookspool").mkdir()
    (tmp_path / "hookspool" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    return str(tmp_path)


def test_release_check_refuses_a_mismatched_tag_a_dev_version_and_a_missing_section(tmp_path, capsys):
    good = "# Changelog\n\n## [Unreleased]\n\n## [1.2.3] - 2026-01-02\n\n### Fixed\n\n- a thing\n\n## [1.2.2] - x\n"
    (tmp_path / "a").mkdir()
    root = _fake_root(tmp_path / "a", "1.2.3", good)
    assert notes.main(["--tag", "v1.2.3", "--root", root]) == 0
    assert capsys.readouterr().out == "### Fixed\n\n- a thing\n"
    assert notes.main(["--tag", "v1.2.4", "--root", root]) == 1
    assert "does not match" in capsys.readouterr().err
    (tmp_path / "b").mkdir()
    dev = _fake_root(tmp_path / "b", "1.2.3.dev0", good.replace("1.2.3", "1.2.3.dev0"))
    assert notes.main(["--tag", "v1.2.3.dev0", "--root", dev]) == 1
    assert "final release" in capsys.readouterr().err
    (tmp_path / "c").mkdir()
    undated = _fake_root(tmp_path / "c", "1.2.3", good.replace("[1.2.3] - 2026-01-02", "[1.2.3]"))
    assert notes.main(["--tag", "v1.2.3", "--root", undated]) == 1
    (tmp_path / "d").mkdir()
    stale = _fake_root(tmp_path / "d", "1.2.4", good)
    assert notes.main(["--tag", "v1.2.4", "--root", stale]) == 1


# ---- workflows ------------------------------------------------------------------------------------------------------
def wf(name: str) -> dict:
    return _miniyaml.load(read(f".github/workflows/{name}"))


USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$", re.M)


@pytest.mark.parametrize("name", sorted(os.listdir(WORKFLOWS)))
def test_every_action_is_pinned_to_a_commit_with_its_version_in_a_comment(name):
    text = read(f".github/workflows/{name}")
    found = USES.findall(text)
    assert found, name
    for ref, rest in found:
        action, _, sha = ref.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", sha), f"{name}: {ref} is not pinned to a full commit SHA"
        assert re.match(r"\s+# v\d+(\.\d+)*\b", rest), f"{name}: {action} has no '# vX.Y.Z' comment"


def _jobs_with(doc: dict, perm: str) -> list[str]:
    return [j for j, job in doc["jobs"].items() if (job.get("permissions") or {}).get(perm) == "write"]


def test_release_workflow_runs_on_version_tags_and_only_the_pypi_job_gets_an_identity_token():
    doc = wf("release.yml")
    assert doc["on"] == {"push": {"tags": ["v*"]}}
    assert doc["permissions"] == {"contents": "read"}
    assert list(doc["jobs"]) == ["build", "github-release", "pypi"]
    assert _jobs_with(doc, "id-token") == ["pypi"]
    assert _jobs_with(doc, "contents") == ["github-release"]
    build, gh, pypi = (doc["jobs"][j] for j in ("build", "github-release", "pypi"))
    assert build["permissions"] == {"contents": "read"}
    runs = [s.get("run", "") for s in build["steps"]]
    check = runs.index(next(r for r in runs if "release_notes.py" in r))
    assert check < runs.index("python -m build"), "the tag is checked before anything is built"
    assert 'release_notes.py --tag "$TAG"' in runs[check]
    assert any("twine check --strict" in r for r in runs)
    assert gh["needs"] == "build" and pypi["needs"] == "github-release"
    assert any("gh release create" in s.get("run", "") and "--notes-file" in s.get("run", "") for s in gh["steps"])
    assert pypi["permissions"] == {"id-token": "write"}
    assert pypi["environment"]["name"] == "pypi"
    assert pypi["steps"][-1]["uses"].startswith("pypa/gh-action-pypi-publish@")
    text = read(".github/workflows/release.yml")
    assert "${{ secrets." not in text and "password:" not in text, "Trusted Publishing: no stored token"


def test_docs_workflow_builds_strict_on_pull_requests_and_only_the_deploy_job_holds_pages_rights():
    doc = wf("docs.yml")
    assert doc["on"] == {"push": {"branches": ["main"]}, "pull_request": None}
    assert doc["permissions"] == {"contents": "read"}
    build, deploy = doc["jobs"]["build"], doc["jobs"]["deploy"]
    assert build["permissions"] == {"contents": "read"}
    assert any("mkdocs build --strict" in s.get("run", "") for s in build["steps"])
    assert _jobs_with(doc, "pages") == ["deploy"] and _jobs_with(doc, "id-token") == ["deploy"]
    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}
    assert deploy["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert deploy["steps"][-1]["uses"].startswith("actions/deploy-pages@")
    upload = next(s for s in build["steps"] if str(s.get("uses", "")).startswith("actions/upload-pages-artifact@"))
    assert upload["if"] == deploy["if"], "a pull request builds the site but never uploads it"


@pytest.mark.parametrize("name", ["ci.yml", "plugins.yml", "bobbin.yml", "docs.yml"])
def test_pull_request_jobs_read_only(name):
    doc = wf(name)
    assert "pull_request" in doc["on"] and doc["permissions"] == {"contents": "read"}
    for job_name, job in doc["jobs"].items():
        perms = job.get("permissions") or doc["permissions"]
        if job.get("if", "").startswith("github.event_name == 'push'"):
            continue                                  # never runs for a pull request
        assert perms == {"contents": "read"}, (name, job_name, perms)


def test_ci_build_job_builds_checks_and_smoke_installs_the_wheel():
    doc = wf("ci.yml")
    job = doc["jobs"]["build"]
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    assert "python -m build" in runs and "twine check --strict dist/*" in runs
    assert "pip install --no-deps dist/*.whl" in runs
    assert "hookspool\" --version" in runs and "hookspool\" bobbin list" in runs
    assert "requirements-release.txt" in runs


def test_dependabot_updates_actions_and_pip_weekly():
    doc = _miniyaml.load(read(".github/dependabot.yml"))
    assert doc["version"] == 2
    got = {(u["package-ecosystem"], u["directory"], u["schedule"]["interval"]) for u in doc["updates"]}
    assert got == {("github-actions", "/", "weekly"), ("pip", "/", "weekly")}


def test_tool_requirements_are_pinned_exactly():
    for rel in ("requirements-release.txt", "requirements-docs.txt"):
        reqs = [ln for ln in read(rel).splitlines() if ln.strip() and not ln.startswith("#")]
        assert reqs and all(re.fullmatch(r"[A-Za-z0-9_.-]+==[\w.]+", r) for r in reqs), (rel, reqs)


# ---- community files ------------------------------------------------------------------------------------------------
def test_community_files():
    sec = read("SECURITY.md")
    assert "0.2.x" in sec and "/security/advisories/new" in sec and "@" not in sec
    coc = read("CODE_OF_CONDUCT.md")
    assert coc.startswith("# Contributor Covenant Code of Conduct") and "version 2.1" in coc
    assert "INSERT CONTACT METHOD" not in coc and "/security/advisories/new" in coc
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", coc), "no email address: reports go through GitHub"
    assert read(".github/CODEOWNERS").split() == ["*", "@jaredzimmerman"]
    cfg = _miniyaml.load(read(".github/ISSUE_TEMPLATE/config.yml"))
    assert cfg["blank_issues_enabled"] is False
    assert {link["url"] for link in cfg["contact_links"]} >= {"https://jaredzimmerman.github.io/hookspool/"}
    assert not any("discussions" in link["url"] for link in cfg["contact_links"])
    bug = _miniyaml.load(read(".github/ISSUE_TEMPLATE/bug_report.yml"))
    assert {b.get("id") for b in bug["body"]} >= {"version", "what-happened"}
    new = _miniyaml.load(read(".github/ISSUE_TEMPLATE/new_bobbin.yml"))
    ids = {b.get("id"): b for b in new["body"]}
    assert {"name", "webhook-docs", "signature", "maintain"} <= set(ids)
    assert all(ids[i]["validations"]["required"] is True for i in ("name", "webhook-docs", "signature", "maintain"))


def test_social_preview_is_a_1280_by_640_png_and_is_referenced():
    with open(os.path.join(DOCS, "assets", "social-preview.png"), "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    assert (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")) == (1280, 640)
    assert "social-preview.png" in read("docs/assets/banners/README.md")
    assert 'assets/social-preview.png' in read("docs-site/overrides/main.html")


def test_readme_badge_line_sits_under_the_title():
    lines = read("README.md").splitlines()
    assert lines[0] == "# hookspool" and lines[1] == ""
    badges = lines[2]
    for want in ("actions/workflows/ci.yml/badge.svg", "img.shields.io/pypi/v/hookspool",
                 "img.shields.io/pypi/pyversions/hookspool", "license-MIT", "jaredzimmerman.github.io/hookspool/"):
        assert want in badges, want
    assert lines[3] == ""


# ---- docs site ------------------------------------------------------------------------------------------------------
def _nav_pages(nav) -> list[str]:
    out = []
    for item in nav:
        if isinstance(item, str):
            out.append(item)
        else:
            for v in item.values():
                out += _nav_pages(v) if isinstance(v, list) else [v]
    return out


def test_site_nav_follows_the_docs_index_and_covers_every_page():
    cfg = _miniyaml.load(read("mkdocs.yml"))
    assert cfg["site_url"] == "https://jaredzimmerman.github.io/hookspool/" and cfg["repo_url"] == REPO
    assert "nav" not in cfg, "the nav is built from docs/README.md by docs-site/hooks.py"
    nav = hooks.build_nav(DOCS)
    pages = _nav_pages(nav)
    folders = [next(iter(item.values()))[0].split("/")[0] for item in nav[1:]]
    assert folders == ["getting-started", "configuration", "bobbins", "operations", "architecture"]
    every = {os.path.relpath(p, DOCS).replace(os.sep, "/") for p in glob.glob(os.path.join(DOCS, "*", "*.md"))}
    assert set(pages) == every | {"README.md"} and len(pages) == len(set(pages))
    for item in nav[1:]:
        (section,) = item.values()
        assert section[0].endswith("/README.md"), "each folder's README.md is its section index"


def test_site_links_outside_docs_go_to_github_and_banners_follow_the_palette():
    page_dir = os.path.join(DOCS, "operations")
    md = ('<picture><source media="(prefers-color-scheme: dark)" srcset="../assets/banners/x-dark.svg">'
          '<img alt="A thing." src="../assets/banners/x-light.svg" width="960"></picture>\n\n'
          "[cl](../../CHANGELOG.md#020) [dep](../../deploy/) [cli](cli.md) [web](https://example.com/a.md)\n\n"
          "```\n[keep](../../CHANGELOG.md)\n```\n")
    out = hooks._rewrite(md, page_dir, DOCS, f"{REPO}/blob/main/")
    assert "![A thing.](../assets/banners/x-light.svg#only-light)![A thing.](../assets/banners/x-dark.svg#only-dark)" in out
    assert f"[cl]({REPO}/blob/main/CHANGELOG.md#020)" in out
    assert f"[dep]({REPO}/tree/main/deploy)" in out
    assert "[cli](cli.md)" in out and "[web](https://example.com/a.md)" in out
    assert "```\n[keep](../../CHANGELOG.md)\n```" in out, "code fences are examples, left alone"


def _mkdocs() -> list[str] | None:
    if importlib.util.find_spec("material") is not None:
        return [sys.executable, "-m", "mkdocs"]
    exe = shutil.which("mkdocs")
    if exe:
        probe = subprocess.run([os.path.join(os.path.dirname(exe), "python"), "-c", "import material"],
                               capture_output=True)
        if probe.returncode == 0:
            return [exe]
    return None


def test_mkdocs_build_strict(tmp_path):
    tool = _mkdocs()
    if tool is None:
        pytest.skip("mkdocs-material is not installed (pip install -r requirements-docs.txt); CI's docs job runs it")
    r = subprocess.run([*tool, "build", "--strict", "--config-file", os.path.join(ROOT, "mkdocs.yml"),
                        "--site-dir", str(tmp_path / "site")], capture_output=True, text=True, timeout=300, cwd=ROOT)
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
    home = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert 'og:image" content="https://jaredzimmerman.github.io/hookspool/assets/social-preview.png"' in home
    assert (tmp_path / "site" / "assets" / "banners" / "breakers-dark.svg").is_file()
    ops = (tmp_path / "site" / "operations" / "index.html").read_text(encoding="utf-8")
    assert "breakers-light.svg#only-light" in ops and "breakers-dark.svg#only-dark" in ops


# ---- packages -------------------------------------------------------------------------------------------------------
def _build_tool() -> list[str] | None:
    try:
        if importlib.util.find_spec("build.__main__") is not None:
            return [sys.executable, "-m", "build", "--outdir"]
    except (ImportError, ValueError):
        pass
    uv = shutil.which("uv")
    return [uv, "build", "--out-dir"] if uv else None


def _source_files() -> list[str]:
    """The files of this checkout a clean clone would have: tracked plus untracked-but-not-ignored."""
    try:
        out = subprocess.run(["git", "-C", ROOT, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, check=True).stdout.decode()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [p for p in out.split("\0") if p and os.path.isfile(os.path.join(ROOT, p))]


@pytest.fixture(scope="module")
def dists(tmp_path_factory):
    tool = _build_tool()
    if tool is None:
        pytest.skip("neither `python -m build` nor `uv` is available")
    src = tmp_path_factory.mktemp("src")
    for rel in _source_files():
        os.makedirs(os.path.dirname(src / rel), exist_ok=True)
        shutil.copy2(os.path.join(ROOT, rel), src / rel)
    out = tmp_path_factory.mktemp("dist")
    r = subprocess.run([*tool, str(out), str(src)], capture_output=True, text=True, timeout=600)
    blob = r.stdout + r.stderr
    if r.returncode != 0 and re.search(r"(?i)failed to fetch|no matching distribution|could not find a version|"
                                       r"name resolution|network is unreachable|connection (?:error|refused|reset)|"
                                       r"max retries exceeded|failed to download|offline", blob):
        pytest.skip("the build backend could not be fetched (offline): " + blob[-300:])
    assert r.returncode == 0, blob[-2000:]
    (wheel,) = glob.glob(str(out / "*.whl"))
    (sdist,) = glob.glob(str(out / "*.tar.gz"))
    return wheel, sdist


def test_dists_are_named_for_the_version(dists):
    wheel, sdist = dists
    assert os.path.basename(wheel).startswith(f"hookspool-{VERSION}-py3-none-any")
    assert os.path.basename(sdist) == f"hookspool-{VERSION}.tar.gz"


def test_wheel_carries_every_bobbin_file(dists):
    names = set(zipfile.ZipFile(dists[0]).namelist())
    bobbins = os.path.join(ROOT, "hookspool", "bobbins")
    tomls = glob.glob(os.path.join(bobbins, "*", "*", "bobbin.toml"))
    assert len(tomls) >= 9
    for path in glob.glob(os.path.join(bobbins, "**", "*"), recursive=True):
        if os.path.isfile(path) and "__pycache__" not in path and not path.endswith(".pyc"):
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            assert rel in names, f"{rel} is not in the wheel"
    assert not any("assets/banners" in n for n in names), "the banners are for the docs, not the wheel"
    meta = zipfile.ZipFile(dists[0]).read(f"hookspool-{VERSION}.dist-info/METADATA").decode()
    author = tomllib.loads(read("pyproject.toml"))["project"]["authors"][0]["name"]
    for want in (f"Author: {author}", "License-Expression: MIT",
                 "Project-URL: Homepage, https://jaredzimmerman.github.io/hookspool/",
                 "Classifier: Programming Language :: Python :: 3.14", "Keywords: "):
        assert want in meta, want


def test_sdist_carries_the_docs_their_images_and_the_site_config(dists):
    with tarfile.open(dists[1]) as t:
        names = {n.split("/", 1)[1] for n in t.getnames() if "/" in n}
    for want in ("docs/assets/banners/brake-dark.gif", "docs/assets/banners/rack-light.svg",
                 "docs/assets/social-preview.png", "docs/operations/releasing.md", "mkdocs.yml", "docs-site/hooks.py",
                 "docs-site/overrides/main.html", "SECURITY.md", "CODE_OF_CONDUCT.md", "CHANGELOG.md",
                 "requirements-docs.txt", "requirements-release.txt", "hookspool/bobbins/github/repo/bobbin.toml"):
        assert want in names, want


def test_twine_check_passes(dists):
    if importlib.util.find_spec("twine") is None:
        pytest.skip("twine is not installed (pip install -r requirements-release.txt); CI's build job runs it")
    r = subprocess.run([sys.executable, "-m", "twine", "check", "--strict", *dists], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_wheel_installs_into_a_fresh_environment_and_runs(dists, tmp_path):
    venv = tmp_path / "venv"
    uv = shutil.which("uv")
    if uv:
        subprocess.run([uv, "venv", "-q", "--python", sys.executable, str(venv)], check=True, timeout=120)
        r = subprocess.run([uv, "pip", "install", "-q", "--offline", "--no-deps", "--python", str(venv / "bin" / "python"),
                            dists[0]], capture_output=True, text=True, timeout=300)
    else:
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=300)
        r = subprocess.run([str(venv / "bin" / "python"), "-m", "pip", "install", "-q", "--no-index", "--no-deps",
                            dists[0]], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    exe = str(venv / "bin" / "hookspool")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "HOOKSPOOL_HOME": str(tmp_path / "home")}
    v = subprocess.run([exe, "--version"], capture_output=True, text=True, cwd=tmp_path, env=env, timeout=60)
    assert v.returncode == 0 and v.stdout.strip() == f"hookspool {VERSION}", v.stdout + v.stderr
    b = subprocess.run([exe, "bobbin", "list"], capture_output=True, text=True, cwd=tmp_path, env=env, timeout=60)
    assert b.returncode == 0, b.stdout + b.stderr
    listed = {ln.split()[0] for ln in b.stdout.splitlines() if ln.strip()}
    shipped = {tomllib.load(open(p, "rb"))["name"]
               for p in glob.glob(os.path.join(ROOT, "hookspool", "bobbins", "*", "*", "bobbin.toml"))}
    assert shipped <= listed, shipped - listed
    where = subprocess.run([str(venv / "bin" / "python"), "-I", "-c", "import hookspool; print(hookspool.__file__)"],
                           capture_output=True, text=True, cwd=tmp_path, env=env, timeout=60)
    assert where.stdout.strip().startswith(str(venv)), "the installed package ran, not the checkout"
