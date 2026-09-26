# Trikon Docs Site

End-user-facing documentation, built with [Mintlify](https://mintlify.com/).

## Local development

```bash
npm i -g mintlify
cd docs-site
mintlify dev
```

Docs served at `http://localhost:3000`.

## Deployment

Mintlify auto-deploys on push to `main` when the docs-site is connected via the Mintlify dashboard. Target production URL: `https://trikon.unideploy.com`.

## Structure

```
docs-site/
├── docs.json              # Mintlify config (nav, theme, tabs)
├── introduction.mdx       # Landing page
├── quickstart.mdx         # 5-minute install → first verdict
├── installation.mdx       # Detailed install options
├── concepts/              # What is a Verdict? Blast radius? Policy?
├── integrations/          # MCP, Claude Code, GitHub App, etc.
├── cli/                   # Command-line reference
├── deployment/            # Team tier and Enterprise onboarding
├── api-reference/         # REST API for hosted control plane
├── security.mdx           # Trust page — architecture, encryption, data handling
├── pricing.mdx            # Public pricing (mirrors PRICING.md)
└── changelog.mdx          # Public release notes
```

## Style

- **Second person, imperative mood.** "Install the CLI" not "The user installs the CLI."
- **Show, don't tell.** Every concept page has a code example.
- **Link back to source.** For every doc, link to the underlying implementation file so readers can go deeper.
- **No corporate voice.** Match the tone of the main README.

## What NOT to put here

- Internal decisions (see `PRICING.md`, `OPERATIONS.md`, `EXECUTION_PLAN.md` in repo root).
- Anything unreleased. Docs describe what shipped, not what's coming.
- Marketing copy. That lives on `trikon.unideploy.com` (the landing page), not in docs.
