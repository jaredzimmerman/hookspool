# Releasing

How a version of hookspool reaches GitHub Releases and PyPI. Pushing a tag does the work: `.github/workflows/release.yml`
checks the tag, builds, creates the GitHub Release, and publishes to PyPI. No PyPI token is stored anywhere. PyPI trusts
that one workflow through Trusted Publishing.

## One-time setup

These steps happen once, in a browser, by someone with admin rights on the repository and an account on pypi.org.
Nothing in the repository can do them.

1. On PyPI, add a pending trusted publisher. Sign in at <https://pypi.org/manage/account/publishing/> and, under
   "Add a new pending publisher", choose GitHub and fill in:

   | field | value |
   |---|---|
   | PyPI project name | `hookspool` |
   | Owner | `jaredzimmerman` |
   | Repository name | `hookspool` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   The first successful publish creates the project on PyPI, and the pending publisher becomes a normal one.
2. On GitHub, create the `pypi` environment: go to Settings, then Environments, then New
   environment, and name it `pypi`. A required reviewer is optional: with one, every publish waits for a click. Under
   "Deployment branches and tags", limit it to tags that match `v*`.
3. Turn on GitHub Pages: Settings, then Pages, then Source: GitHub Actions. From then on `.github/workflows/docs.yml`
   publishes the site from `main`.
4. Turn on private vulnerability reporting: Settings, then Security, then Private vulnerability reporting.
   [SECURITY.md](../../SECURITY.md) and the code of conduct both send reports there.
5. Set the social preview: Settings, then General, then Social preview, and upload `docs/assets/social-preview.png`. The docs
   site uses the same image for its link cards.

## Cutting a release

1. On a branch, set the version in `hookspool/__init__.py` (`__version__ = "X.Y.Z"`). `pyproject.toml` reads it from
   there.
2. In `CHANGELOG.md`, rename the entries under `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, and leave an empty
   `## [Unreleased]` above it. At the bottom, point `[Unreleased]` at `compare/vX.Y.Z...HEAD` and add a
   `[X.Y.Z]` link. `tests/test_release.py` fails if the version, the newest changelog entry, and the links disagree.
3. Check it locally:

   ```sh
   python .github/scripts/release_notes.py --tag vX.Y.Z    # prints the notes the release will carry
   python -m pip install -r requirements-release.txt
   python -m build && python -m twine check --strict dist/*
   python -m pytest -q
   ```

4. Open a pull request, let CI pass, and merge it.
5. Tag the merge commit on `main` and push the tag:

   ```sh
   git checkout main && git pull
   git tag -a vX.Y.Z -m "hookspool X.Y.Z"
   git push origin vX.Y.Z
   ```

6. Watch the `release` workflow. It runs three jobs in order:
   - `build` stops unless the tag is `v` plus `hookspool.__version__`, the version is final (no `.dev`, `a`, `b` or
     `rc`), and the changelog has a dated section for it. Then it builds the sdist and the wheel, runs
     `twine check --strict`, and installs the wheel into a fresh environment to run `hookspool --version` and
     `hookspool bobbin list`.
   - `github-release` creates the release for the tag, attaches both files, and uses the changelog section as its
     notes.
   - `pypi` uploads both files from the `pypi` environment. Only this job can ask GitHub for an identity
     token (`id-token: write`).
7. Check the result: `pip install hookspool==X.Y.Z` in a fresh environment, then `hookspool --version`.

## When something goes wrong

- If the build job failed on the tag check, nothing was published. Delete the tag (`git push origin :refs/tags/vX.Y.Z`
  and `git tag -d vX.Y.Z`), fix the version or the changelog in a pull request, and tag again.
- If the GitHub Release exists but the PyPI job failed, re-run that job from the Actions page. If PyPI refuses the upload
  because of the trusted publisher, check the five values in step 1 of the one-time setup against the workflow.
- If a broken release reached PyPI, you cannot replace it: PyPI never accepts the same version twice, even after you delete it. Yank it on
  pypi.org, so `pip` skips it unless someone pins it, and release a fixed `X.Y.Z+1`.

## Why the workflows look the way they do

- Every action in the workflows is pinned to a full commit SHA, with its version in a comment. Dependabot
  (`.github/dependabot.yml`) opens a weekly pull request when a pinned action or a Python requirement has a new
  release.
- The pull request workflows get a read-only token. In `release.yml`, only the GitHub Release job can write to the
  repository, and only the PyPI job can request an identity token. In `docs.yml`, only the deploy job holds the Pages
  permissions.
- The build tools (`requirements-release.txt`) and the docs tools (`requirements-docs.txt`) are pinned to exact versions.
