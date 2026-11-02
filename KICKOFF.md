# First message to paste into Claude Code

Open a terminal inside this folder, run `claude`, and paste everything below the line.

---

Read `CLAUDE.md`, `docs/SPEC.md`, and `docs/ROADMAP.md` fully before doing anything.

From now on, always reply to me in English, even when I write in Arabic. You own every git and GitHub operation as described in `CLAUDE.md`; I will not run git commands myself.

Start now:

1. Check the environment: `git config user.name`, `git config user.email`, `gh auth status`, and whether `uv`, `pnpm`, `aria2c`, `ffmpeg`, and `certutil` are installed. Report what is missing with the exact install command for my distro, then continue with what you can.
2. Initialize the repository and make the initial commit with the existing files.
3. Create the GitHub remote if `gh` is authenticated, and push `main`.
4. Create `phase/0-scaffold`, complete Phase 0, open the PR, and merge it when checks pass.
5. Create `phase/0.5-poc` and build the proof of concept. Stop after writing `poc/RESULTS.md` and give me the go / no-go summary with the exact steps I need to run to test capture in my own Chrome and Firefox.

Do not start Phase 1 until I reply.
