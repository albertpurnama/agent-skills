# agent-skills — AI Agent Skills Hub

A centralized repository for skills, helpers, and setup across all AI agents.

## Structure

| Directory | Runtime | Description |
|-----------|---------|-------------|
| `.claude/` | Claude Code | Skills and config for Claude |
| `openclaw/` | OpenClaw | Skills for OpenClaw agents |

## Skills

### OpenClaw

| Skill | Description |
|-------|-------------|
| `agentmail/` | Email inbox management via AgentMail API |
| `create-proposal/` | Indonesian construction quotation PDFs |
| `gemini-image-gen/` | Image generation via Google's Gemini API |

### Claude

| Skill | Description |
|-------|-------------|
| `github/` | Authenticated GitHub operations |
| `implement-and-review/` | Loop-engineering pattern: implement → adversarial review (diff-only context, optional per-role model) → fix, via the Workflow tool. See [bun.com/blog/bun-in-rust](https://bun.com/blog/bun-in-rust#loops-that-write-review-code). |
| `mac-input-lag/` | Diagnose and fix macOS input lag: WindowServer load, leaked keyboard event taps, stale Chrome GPU processes, memory pressure, busy containers. Asks before restarting anything, then re-measures. For intermittent lag, records actual input delay alongside a process timeline and reports what was running during each episode. |
| `mac-memory/` | Where a Mac's RAM goes: memory by app, crediting MCP servers and child processes to the agent session or app that launched them. Records for hours in the background and reports spikes (what started or grew), possible leaks, swap and memory-pressure warnings as an HTML page. |

---

## Claude Setup (Fresh Install)

On a new system, credentials aren't set up yet so you can't use the normal
clone flow. Bootstrap manually:

### 1. Place GitHub App credentials

```bash
mkdir -p ~/.config/github-app
chmod 700 ~/.config/github-app

# Create ~/.config/github-app/credentials:
cat > ~/.config/github-app/credentials <<'EOF'
GITHUB_APP_ID=<your-app-id>
GITHUB_APP_CLIENT_ID=<your-client-id>
GITHUB_APP_CLIENT_SECRET=<your-client-secret>
EOF

# Copy your private key:
cp /path/to/private-key.pem ~/.config/github-app/private-key.pem

chmod 600 ~/.config/github-app/credentials ~/.config/github-app/private-key.pem
```

### 2. Generate a token and clone this repo

No tooling needed — pure bash + openssl + curl:

```bash
source ~/.config/github-app/credentials

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
NOW=$(date +%s)
H=$(echo -n '{"alg":"RS256","typ":"JWT"}' | b64url)
P=$(echo -n "{\"iat\":$((NOW-60)),\"exp\":$((NOW+600)),\"iss\":\"$GITHUB_APP_ID\"}" | b64url)
SIG=$(echo -n "$H.$P" | openssl dgst -sha256 -sign ~/.config/github-app/private-key.pem | b64url)
JWT="$H.$P.$SIG"

INSTALL_ID=$(curl -s -H "Authorization: Bearer $JWT" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/app/installations \
  | python3 -c "import sys,json; [print(i['id']) for i in json.load(sys.stdin) if i['account']['login']=='albertpurnama']")

TOKEN=$(curl -s -X POST \
  -H "Authorization: Bearer $JWT" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/app/installations/$INSTALL_ID/access_tokens \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

git clone https://x-access-token:$TOKEN@github.com/albertpurnama/agent-skills ~/agent-skills
```

### 3. Install

```bash
cd ~/agent-skills
make install
```

---

## Subsequent use (already set up)

```bash
make install    # install / re-link everything
make check      # verify credentials and installation
make uninstall  # remove installed files
```

---

## Using the /github skill in Claude

Once installed, Claude can perform authenticated GitHub operations:

- *"clone https://github.com/org/private-repo"*
- *"push my changes to origin"*
- *"create a PR for this branch"*
- *"list open issues on owner/repo"*

---

## Contributing Skills

1. Add your skill to the appropriate runtime directory
2. Include a `SKILL.md` with description and usage
3. Update the table above
4. Submit a PR or push directly
