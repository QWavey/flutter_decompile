# Pushing this to GitHub

This repo is complete and committed locally. It is not on GitHub yet, because
creating a repository needs the GitHub API and no credentials are configured
here (`gh auth status` reports not logged in). Git's credential manager can
push to repos that already exist, but it cannot create one.

## One command

```bash
gh auth login && gh repo create flutter_decompile --public --source=. --push
```

Run it from this directory. `gh auth login` opens a browser once; after that
`gh repo create` makes the repo and pushes `main` in the same step.

## If you would rather not use gh

Create an empty repo on github.com (no README, no .gitignore, no licence -
this repo already has all three), then:

```bash
git remote add origin https://github.com/<you>/flutter_decompile.git
git push -u origin main
```

The credential manager will handle the authentication.

## Check it first

```bash
python main.py --check
```

Should report git, cmake, a compiler and Python all present. Blutter is
fetched on first use, so it is fine for that line to say "not present".
