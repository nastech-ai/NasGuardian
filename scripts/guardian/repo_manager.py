"""
NasTech Guardian — Multi-Repo Manager
Handles: registry, full pre-join audit, fix-plan generation,
         cross-repo dashboard, health scoring.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# ── Severity levels ───────────────────────────────────────────────
SEV_CRITICAL = "CRITICAL"
SEV_HIGH     = "HIGH"
SEV_MEDIUM   = "MEDIUM"
SEV_LOW      = "LOW"
SEV_INFO     = "INFO"
SEV_OK       = "OK"

SEV_ICONS = {
    SEV_CRITICAL: "🔴",
    SEV_HIGH:     "🟠",
    SEV_MEDIUM:   "🟡",
    SEV_LOW:      "🔵",
    SEV_INFO:     "ℹ️",
    SEV_OK:       "✅",
}

SEV_WEIGHTS = {SEV_CRITICAL: 40, SEV_HIGH: 20, SEV_MEDIUM: 8, SEV_LOW: 3, SEV_INFO: 0, SEV_OK: 0}


# ── Finding dataclass ─────────────────────────────────────────────
@dataclass
class Finding:
    category:    str
    severity:    str
    title:       str
    detail:      str
    fix:         str
    fix_cmd:     str = ""      # shell / Telegram command to apply the fix
    refs:        list = field(default_factory=list)

    @property
    def icon(self) -> str:
        return SEV_ICONS.get(self.severity, "•")


# ── Per-chat repo registry ─────────────────────────────────────────
class RepoRegistry:
    """Stores per-chat repo list and active selection."""

    def __init__(self):
        self._data: dict[str, dict] = defaultdict(lambda: {
            "active": "",
            "repos":  {},   # repo_slug → {"added_at": ts, "last_audit": None, "score": None}
        })

    def add(self, chat_id: str, repo: str, score: Optional[int] = None) -> None:
        slug = _slug(repo)
        self._data[chat_id]["repos"][slug] = {
            "slug":        slug,
            "added_at":    _now(),
            "last_audit":  _now(),
            "score":       score,
        }
        if not self._data[chat_id]["active"]:
            self._data[chat_id]["active"] = slug

    def remove(self, chat_id: str, repo: str) -> bool:
        slug = _slug(repo)
        d    = self._data[chat_id]
        if slug not in d["repos"]:
            return False
        del d["repos"][slug]
        if d["active"] == slug:
            d["active"] = next(iter(d["repos"]), "")
        return True

    def switch(self, chat_id: str, repo: str) -> bool:
        slug = _slug(repo)
        if slug not in self._data[chat_id]["repos"]:
            return False
        self._data[chat_id]["active"] = slug
        return True

    def active(self, chat_id: str, default: str = "") -> str:
        return self._data[chat_id]["active"] or default

    def list_repos(self, chat_id: str) -> list[dict]:
        return list(self._data[chat_id]["repos"].values())

    def has(self, chat_id: str, repo: str) -> bool:
        return _slug(repo) in self._data[chat_id]["repos"]

    def update_score(self, chat_id: str, repo: str, score: int) -> None:
        slug = _slug(repo)
        if slug in self._data[chat_id]["repos"]:
            self._data[chat_id]["repos"][slug]["score"]      = score
            self._data[chat_id]["repos"][slug]["last_audit"] = _now()


# ── Global registry singleton ─────────────────────────────────────
registry = RepoRegistry()


# ── GitHub helpers ────────────────────────────────────────────────
def _gh(path: str, token: str, method: str = "GET", body: dict = None) -> Any:
    url  = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization":        f"Bearer {token}",
            "Accept":               "application/vnd.github+json",
            "Content-Type":         "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()[:300]}
    except Exception as e:
        return {"_error": str(e)}


def _b64decode(s: str) -> str:
    return base64.b64decode(s.encode()).decode("utf-8", errors="replace")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _slug(repo: str) -> str:
    return repo.strip().lower().strip("/")


def _days_ago(iso: str) -> int:
    try:
        dt  = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return max(0, (now - dt).days)
    except Exception:
        return 999


# ── Full repo audit ───────────────────────────────────────────────

REQUIRED_SECRETS = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "GITHUB_TOKEN",
]

REQUIRED_WORKFLOWS = [
    ("debug_build.yml",    "Main build + APK release workflow"),
    ("nastech_guardian.yml","Guardian orchestrator (7-stage pipeline)"),
    ("nastech_bot.yml",    "Bot PR review + daily digest"),
]

NICE_WORKFLOWS = [
    ("nastech_pr_manager.yml", "PR auto-labeler / manager"),
    ("renovate.json",           "Dependency auto-updater"),
    (".github/auto-me-bot.yml", "PR automation rules"),
]


def audit_repo(repo: str, token: str) -> dict:
    """
    Run a full pre-join audit of a GitHub repo.
    Returns dict with: findings, score, summary, fix_plan, meta.
    """
    findings: list[Finding] = []
    owner, rname = (repo.split("/") + [""])[:2]
    if not rname:
        return _err_result(f"Invalid repo format: '{repo}'. Use owner/name.")

    # ── 1. Repo existence & access ────────────────────────────────
    repo_info = _gh(f"/repos/{owner}/{rname}", token)
    if "_error" in repo_info:
        code = repo_info["_error"]
        if code == 404:
            return _err_result(f"Repo <code>{repo}</code> not found or private without access.")
        if code == 403:
            return _err_result(f"GitHub token lacks read access to <code>{repo}</code>.")
        return _err_result(f"GitHub API error {code} for <code>{repo}</code>.")

    private     = repo_info.get("private", False)
    default_br  = repo_info.get("default_branch", "main")
    fork        = repo_info.get("fork", False)
    archived    = repo_info.get("archived", False)
    stars       = repo_info.get("stargazers_count", 0)
    open_issues = repo_info.get("open_issues_count", 0)
    pushed_at   = repo_info.get("pushed_at", "")
    pushed_days = _days_ago(pushed_at)
    lang        = repo_info.get("language") or "Unknown"
    full_name   = repo_info.get("full_name", repo)
    html_url    = repo_info.get("html_url", f"https://github.com/{repo}")

    if archived:
        findings.append(Finding("repo", SEV_HIGH, "Repository is archived",
            "Archived repos cannot receive pushes or trigger workflows.",
            "Un-archive via GitHub Settings → Danger Zone → Unarchive.",
            f"gh repo unarchive {repo}"))

    if fork:
        findings.append(Finding("repo", SEV_INFO, "Repository is a fork",
            "Some Guardian features (secrets, Actions) need upstream sync.",
            "Keep fork synced: gh repo sync",
            f"gh repo sync {repo}"))

    if pushed_days > 90:
        findings.append(Finding("activity", SEV_MEDIUM,
            f"No commits in {pushed_days} days",
            "Repository appears dormant — Guardian won't find much to do.",
            "Push a commit or use workflow_dispatch to trigger pipelines.", ""))

    findings.append(Finding("repo", SEV_OK,
        f"Repo accessible — {full_name}",
        f"{'Private' if private else 'Public'} · {lang} · ⭐{stars} · default branch: {default_br}",
        "", ""))

    # ── 2. Branches ───────────────────────────────────────────────
    branches_r = _gh(f"/repos/{owner}/{rname}/branches?per_page=30", token)
    branch_names = [b.get("name","") for b in (branches_r if isinstance(branches_r, list) else [])]

    if default_br not in branch_names:
        findings.append(Finding("branches", SEV_HIGH,
            f"Default branch '{default_br}' not found",
            "Guardian workflows target the default branch.",
            f"Create branch: git checkout -b {default_br} && git push origin {default_br}", ""))
    else:
        findings.append(Finding("branches", SEV_OK,
            f"Default branch '{default_br}' exists", "", "", ""))

    # ── 3. Branch protection ──────────────────────────────────────
    bp = _gh(f"/repos/{owner}/{rname}/branches/{default_br}/protection", token)
    if "_error" in bp:
        findings.append(Finding("security", SEV_MEDIUM,
            f"Branch protection not set on '{default_br}'",
            "Anyone can push directly to the default branch.",
            "Enable: Settings → Branches → Add rule → Require PR reviews.",
            f"gh api repos/{repo}/branches/{default_br}/protection -X PUT "
            f"--field required_status_checks=null "
            f"--field enforce_admins=true "
            f"--field required_pull_request_reviews={{\"required_approving_review_count\":1}} "
            f"--field restrictions=null"))
    else:
        findings.append(Finding("security", SEV_OK,
            f"Branch protection enabled on '{default_br}'", "", "", ""))

    # ── 4. GitHub Actions secrets ─────────────────────────────────
    secrets_r = _gh(f"/repos/{owner}/{rname}/actions/secrets", token)
    if "_error" in secrets_r:
        findings.append(Finding("secrets", SEV_HIGH,
            "Cannot read Actions secrets (token needs admin scope)",
            "Add 'repo' scope to your GITHUB_TOKEN.",
            "Create a new token: github.com/settings/tokens → Generate new token (classic) → check 'repo'.", ""))
        existing_secrets = set()
    else:
        existing_secrets = {s.get("name","") for s in secrets_r.get("secrets", [])}
        if existing_secrets:
            findings.append(Finding("secrets", SEV_OK,
                f"{len(existing_secrets)} Actions secrets found",
                ", ".join(sorted(existing_secrets)[:8]), "", ""))

    for sec in REQUIRED_SECRETS:
        if sec not in existing_secrets and existing_secrets:
            findings.append(Finding("secrets", SEV_HIGH,
                f"Missing secret: {sec}",
                f"Workflows that need {sec} will fail silently.",
                f"Set it: gh secret set {sec} --repo {repo}",
                f"gh secret set {sec} --repo {repo}"))

    # ── 5. Workflows ──────────────────────────────────────────────
    wf_list_r = _gh(f"/repos/{owner}/{rname}/actions/workflows", token)
    wf_files  = {w.get("path","").replace(".github/workflows/",""):w
                 for w in wf_list_r.get("workflows", [])}

    for wf_file, wf_desc in REQUIRED_WORKFLOWS:
        if wf_file in wf_files:
            state = wf_files[wf_file].get("state","?")
            icon  = SEV_OK if state == "active" else SEV_MEDIUM
            findings.append(Finding("workflows", icon,
                f"Workflow present: {wf_file}",
                f"{wf_desc} — state: {state}", "", ""))
        else:
            findings.append(Finding("workflows", SEV_HIGH,
                f"Missing workflow: {wf_file}",
                f"{wf_desc} is not set up.",
                f"Copy from nastech-ai/NasTerminal: "
                f"gh api repos/nastech-ai/NasTerminal/contents/.github/workflows/{wf_file} | "
                f"jq -r .content | base64 -d > .github/workflows/{wf_file}",
                ""))

    for wf_file, wf_desc in NICE_WORKFLOWS:
        if wf_file in wf_files:
            findings.append(Finding("workflows", SEV_OK,
                f"Nice-to-have present: {wf_file}", wf_desc, "", ""))
        else:
            findings.append(Finding("workflows", SEV_LOW,
                f"Optional missing: {wf_file}",
                f"{wf_desc}",
                f"Add from nastech-ai/NasTerminal repo.", ""))

    # ── 6. Recent workflow runs ───────────────────────────────────
    runs_r  = _gh(f"/repos/{owner}/{rname}/actions/runs?per_page=20", token)
    runs    = runs_r.get("workflow_runs", [])
    if runs:
        total   = len(runs)
        passed  = sum(1 for r in runs if r.get("conclusion") == "success")
        failed  = sum(1 for r in runs if r.get("conclusion") == "failure")
        rate    = round(passed / total * 100)
        sev     = SEV_OK if rate >= 80 else (SEV_MEDIUM if rate >= 50 else SEV_HIGH)
        findings.append(Finding("builds", sev,
            f"Build success rate: {rate}%",
            f"{passed}/{total} passed, {failed} failed (last {total} runs)",
            "Use /errors to see failed runs and /repair to auto-fix." if failed else "", ""))

        # Check for consecutive failures
        recent_conclusions = [r.get("conclusion","") for r in runs[:5]]
        consec_fails = 0
        for c in recent_conclusions:
            if c == "failure": consec_fails += 1
            else: break
        if consec_fails >= 3:
            findings.append(Finding("builds", SEV_CRITICAL,
                f"{consec_fails} consecutive build failures!",
                "The last 3+ runs all failed — pipeline is broken.",
                "Run /repair to trigger the Guardian repair bot, or check /logs for root cause.",
                ""))
    else:
        findings.append(Finding("builds", SEV_MEDIUM,
            "No workflow runs found",
            "Either workflows haven't been triggered or repo just set up.",
            "Trigger: go to Actions tab and run any workflow manually.", ""))

    # ── 7. Open issues ────────────────────────────────────────────
    issues_r = _gh(f"/repos/{owner}/{rname}/issues?state=open&per_page=20", token)
    if isinstance(issues_r, list):
        real_issues = [i for i in issues_r if "pull_request" not in i]
        if len(real_issues) > 20:
            findings.append(Finding("issues", SEV_MEDIUM,
                f"High issue count: {len(real_issues)} open",
                "Consider triaging and closing stale issues.",
                "Use labels: bug, help wanted, good first issue to organize.", ""))
        elif real_issues:
            bug_count = sum(1 for i in real_issues
                            if any(l["name"] in ("bug","critical")
                                   for l in i.get("labels",[])))
            if bug_count:
                findings.append(Finding("issues", SEV_HIGH,
                    f"{bug_count} open bug issues",
                    "Bug-labelled issues need attention.",
                    "Review: /issues", ""))
            else:
                findings.append(Finding("issues", SEV_OK,
                    f"{len(real_issues)} open issues (no bugs)", "", "", ""))
        else:
            findings.append(Finding("issues", SEV_OK, "No open issues", "", "", ""))

    # ── 8. Pull requests ──────────────────────────────────────────
    prs_r  = _gh(f"/repos/{owner}/{rname}/pulls?state=open&per_page=10", token)
    if isinstance(prs_r, list):
        stale_prs = [p for p in prs_r if _days_ago(p.get("updated_at","")) > 14]
        if stale_prs:
            findings.append(Finding("prs", SEV_LOW,
                f"{len(stale_prs)} stale PR(s) (>14 days)",
                "Stale PRs may block releases.",
                "Review: /pr", ""))
        elif prs_r:
            findings.append(Finding("prs", SEV_OK,
                f"{len(prs_r)} open PR(s) — all recent", "", "", ""))
        else:
            findings.append(Finding("prs", SEV_OK, "No open PRs", "", "", ""))

    # ── 9. Security (Dependabot) ──────────────────────────────────
    dep_r = _gh(f"/repos/{owner}/{rname}/vulnerability-alerts", token)
    if not isinstance(dep_r, str) and "_error" not in dep_r:
        findings.append(Finding("security", SEV_OK,
            "Dependabot vulnerability alerts enabled", "", "", ""))
    else:
        findings.append(Finding("security", SEV_MEDIUM,
            "Dependabot vulnerability alerts not enabled",
            "Automated dependency vulnerability detection is off.",
            "Enable: Settings → Security → Dependabot alerts → Enable.",
            f"gh api repos/{repo}/vulnerability-alerts -X PUT"))

    # Check dependabot.yml
    dep_conf = _gh(f"/repos/{owner}/{rname}/contents/.github/dependabot.yml", token)
    if "_error" not in dep_conf and "content" in dep_conf:
        findings.append(Finding("security", SEV_OK,
            "dependabot.yml present", "", "", ""))
    else:
        findings.append(Finding("security", SEV_LOW,
            "Missing .github/dependabot.yml",
            "Auto dependency update PRs not configured.",
            "Add dependabot.yml — see nastech-ai/NasTerminal for template.", ""))

    # ── 10. Renovate ─────────────────────────────────────────────
    ren_r = _gh(f"/repos/{owner}/{rname}/contents/renovate.json", token)
    if "_error" not in ren_r and "content" in ren_r:
        findings.append(Finding("dependencies", SEV_OK,
            "renovate.json present", "Automated dependency updates configured.", "", ""))
    else:
        findings.append(Finding("dependencies", SEV_LOW,
            "Missing renovate.json",
            "Dependency updates not automated via Renovate.",
            "Add renovate.json from nastech-ai/NasTerminal template.", ""))

    # ── 11. README ────────────────────────────────────────────────
    readme_r = _gh(f"/repos/{owner}/{rname}/readme", token)
    if "_error" not in readme_r and "content" in readme_r:
        readme_len = len(_b64decode(readme_r["content"]))
        if readme_len < 200:
            findings.append(Finding("docs", SEV_LOW,
                "README is very short (<200 chars)",
                "Better documentation helps contributors.",
                "Expand README with setup, usage, contributing sections.", ""))
        else:
            findings.append(Finding("docs", SEV_OK,
                f"README present ({readme_len:,} chars)", "", "", ""))
    else:
        findings.append(Finding("docs", SEV_MEDIUM,
            "No README found",
            "Repository has no README — hard to understand.",
            "Create README.md with project overview.", ""))

    # ── 12. Latest release ────────────────────────────────────────
    rel_r   = _gh(f"/repos/{owner}/{rname}/releases/latest", token)
    if "_error" not in rel_r and "tag_name" in rel_r:
        tag      = rel_r.get("tag_name","?")
        rel_days = _days_ago(rel_r.get("published_at",""))
        if rel_days > 60:
            findings.append(Finding("releases", SEV_LOW,
                f"Last release was {rel_days} days ago ({tag})",
                "Consider cutting a new release.",
                "Use /release workflow to publish.", ""))
        else:
            findings.append(Finding("releases", SEV_OK,
                f"Recent release: {tag} ({rel_days}d ago)", "", "", ""))
    else:
        findings.append(Finding("releases", SEV_INFO,
            "No releases found",
            "No GitHub releases have been published yet.",
            "Trigger: /release", ""))

    # ── 13. Commit activity ───────────────────────────────────────
    commits_r = _gh(f"/repos/{owner}/{rname}/commits?per_page=5", token)
    if isinstance(commits_r, list) and commits_r:
        last_commit_days = _days_ago(
            commits_r[0].get("commit",{}).get("author",{}).get("date",""))
        if last_commit_days > 30:
            findings.append(Finding("activity", SEV_LOW,
                f"Last commit {last_commit_days} days ago",
                "Repository activity is low.", "", ""))
        else:
            findings.append(Finding("activity", SEV_OK,
                f"Active — last commit {last_commit_days}d ago", "", "", ""))

    # ── 14. Build config (Android-specific checks) ───────────────
    bg_r = _gh(f"/repos/{owner}/{rname}/contents/app/build.gradle", token)
    if "_error" not in bg_r and "content" in bg_r:
        bg_text = _b64decode(bg_r["content"])
        if "compileSdkVersion" not in bg_text and "compileSdk" not in bg_text:
            findings.append(Finding("build-config", SEV_HIGH,
                "compileSdk not found in app/build.gradle",
                "Build will fail without compileSdkVersion.",
                "Add: compileSdkVersion 36 to android block.", ""))
        if "signingConfig" not in bg_text:
            findings.append(Finding("build-config", SEV_MEDIUM,
                "No signingConfig in app/build.gradle",
                "Release APKs won't be signed.",
                "Add signing config with keystore secrets.", ""))
        if "minSdkVersion" in bg_text or "minSdk" in bg_text:
            findings.append(Finding("build-config", SEV_OK,
                "app/build.gradle — Android config present", "", "", ""))
    else:
        findings.append(Finding("build-config", SEV_INFO,
            "No app/build.gradle (may not be Android)",
            "If this is an Android app, ensure app/build.gradle exists.", "", ""))

    # ── 15. .github/CODEOWNERS ────────────────────────────────────
    co_r = _gh(f"/repos/{owner}/{rname}/contents/.github/CODEOWNERS", token)
    if "_error" in co_r:
        findings.append(Finding("governance", SEV_INFO,
            "No CODEOWNERS file",
            "Auto-assign reviewers when PRs are opened.",
            "Create .github/CODEOWNERS with: * @your-username", ""))

    # ── Scoring ───────────────────────────────────────────────────
    score    = _compute_score(findings)
    summary  = _summarize(findings)
    fix_plan = _build_fix_plan(findings, repo)

    return {
        "repo":      full_name,
        "url":       html_url,
        "findings":  findings,
        "score":     score,
        "summary":   summary,
        "fix_plan":  fix_plan,
        "meta": {
            "default_branch": default_br,
            "language":       lang,
            "private":        private,
            "fork":           fork,
            "stars":          stars,
            "pushed_days":    pushed_days,
        },
    }


def _compute_score(findings: list[Finding]) -> int:
    """0–100 health score. Deduct for non-OK findings."""
    score = 100
    for f in findings:
        score -= SEV_WEIGHTS.get(f.severity, 0)
    return max(0, min(100, score))


def _summarize(findings: list[Finding]) -> dict:
    counts: dict[str, int] = defaultdict(int)
    for f in findings:
        counts[f.severity] += 1
    return dict(counts)


def _build_fix_plan(findings: list[Finding], repo: str) -> list[dict]:
    """Return ordered fix steps for non-OK findings, by severity."""
    order = [SEV_CRITICAL, SEV_HIGH, SEV_MEDIUM, SEV_LOW, SEV_INFO]
    plan  = []
    step  = 1
    for sev in order:
        for f in findings:
            if f.severity == sev and f.fix:
                plan.append({
                    "step":     step,
                    "sev":      sev,
                    "icon":     SEV_ICONS[sev],
                    "title":    f.title,
                    "fix":      f.fix,
                    "fix_cmd":  f.fix_cmd,
                    "category": f.category,
                })
                step += 1
    return plan


def _err_result(msg: str) -> dict:
    return {
        "repo":     "",
        "url":      "",
        "findings": [],
        "score":    -1,
        "summary":  {},
        "fix_plan": [],
        "error":    msg,
    }


# ── Telegram HTML formatters ──────────────────────────────────────

def format_audit_html(result: dict, compact: bool = False) -> str:
    """Format full audit result as Telegram HTML."""
    if "error" in result:
        return f"❌ <b>Audit Failed</b>\n{result['error']}"

    repo  = result["repo"]
    url   = result["url"]
    score = result["score"]
    meta  = result.get("meta", {})
    summ  = result["summary"]
    finds = result["findings"]

    # Score emoji
    if score >= 85:   score_emoji = "🟢"
    elif score >= 65: score_emoji = "🟡"
    elif score >= 40: score_emoji = "🟠"
    else:             score_emoji = "🔴"

    lines = [
        f"🔍 <b>Repo Audit — <a href='{url}'>{_esc(repo)}</a></b>",
        "",
        f"{score_emoji} <b>Health Score: {score}/100</b>",
        f"🌿 Branch: <code>{meta.get('default_branch','?')}</code>  "
        f"{'🔒' if meta.get('private') else '🌐'}  "
        f"{'🍴 fork' if meta.get('fork') else ''}  "
        f"⭐{meta.get('stars',0)}",
        "",
    ]

    # Summary bar
    crit = summ.get(SEV_CRITICAL, 0)
    high = summ.get(SEV_HIGH, 0)
    med  = summ.get(SEV_MEDIUM, 0)
    low  = summ.get(SEV_LOW, 0)
    ok   = summ.get(SEV_OK, 0)
    lines.append(
        f"🔴 {crit} CRITICAL  🟠 {high} HIGH  🟡 {med} MEDIUM  "
        f"🔵 {low} LOW  ✅ {ok} OK"
    )
    lines.append("")

    if compact:
        # Only show CRITICAL + HIGH in compact mode
        shown = [f for f in finds if f.severity in (SEV_CRITICAL, SEV_HIGH) and f.severity != SEV_OK]
        if shown:
            lines.append("🚨 <b>Critical & High Findings:</b>")
            for f in shown:
                lines.append(f"  {f.icon} <b>{_esc(f.title)}</b>")
                if f.detail:
                    lines.append(f"      {_esc(f.detail[:80])}")
    else:
        # Group by category
        cats: dict[str, list[Finding]] = defaultdict(list)
        for f in finds:
            if f.severity != SEV_OK:
                cats[f.category].append(f)

        if not any(cats.values()):
            lines.append("✅ <b>All checks passed!</b>")
        else:
            for cat, cat_finds in cats.items():
                if not cat_finds:
                    continue
                lines.append(f"<b>▸ {cat.upper()}</b>")
                for f in cat_finds:
                    lines.append(f"  {f.icon} {_esc(f.title)}")
                    if f.detail:
                        lines.append(f"     <i>{_esc(f.detail[:100])}</i>")
                lines.append("")

    lines.append(f"<i>Use /fixplan to see step-by-step fix instructions</i>")
    return "\n".join(lines)


def format_fix_plan_html(result: dict, page: int = 1, per_page: int = 5) -> str:
    """Format fix plan as Telegram HTML (paginated)."""
    if "error" in result:
        return f"❌ {result['error']}"

    plan  = result["fix_plan"]
    repo  = result["repo"]
    score = result["score"]

    if not plan:
        return (
            f"🎉 <b>No fixes needed for {_esc(repo)}!</b>\n"
            f"Health score: {score}/100\n"
            "All checks passed — repo is in great shape."
        )

    total_pages = max(1, (len(plan) + per_page - 1) // per_page)
    page        = max(1, min(page, total_pages))
    start       = (page - 1) * per_page
    chunk       = plan[start : start + per_page]

    lines = [
        f"🔧 <b>Fix Plan — {_esc(repo)}</b>",
        f"Page {page}/{total_pages} · {len(plan)} total fixes",
        "",
    ]

    for item in chunk:
        lines.append(
            f"<b>Step {item['step']}</b> {item['icon']} [{item['sev']}] "
            f"<b>{_esc(item['title'])}</b>"
        )
        lines.append(f"  {_esc(item['fix'][:160])}")
        if item.get("fix_cmd"):
            lines.append(f"  <code>{_esc(item['fix_cmd'][:120])}</code>")
        lines.append("")

    if total_pages > 1:
        nav = []
        if page > 1:             nav.append(f"/fixplan_{page-1}")
        if page < total_pages:   nav.append(f"/fixplan_{page+1}")
        lines.append("  ".join(nav))

    return "\n".join(lines)


def format_dashboard_html(chat_id: str, token: str) -> str:
    """Format multi-repo dashboard."""
    repos = registry.list_repos(chat_id)
    if not repos:
        return (
            "📊 <b>Multi-Repo Dashboard</b>\n\n"
            "No repos tracked yet.\n"
            "Add one with: <code>/repo add owner/name</code>"
        )

    active = registry.active(chat_id)
    lines  = [f"📊 <b>Guardian Dashboard — {len(repos)} repos</b>\n"]

    for r in repos:
        slug   = r["slug"]
        score  = r.get("score")
        active_marker = " ← active" if slug == active else ""
        if score is None:
            score_str = "⏳ not scanned"
        elif score >= 85:
            score_str = f"🟢 {score}/100"
        elif score >= 65:
            score_str = f"🟡 {score}/100"
        elif score >= 40:
            score_str = f"🟠 {score}/100"
        else:
            score_str = f"🔴 {score}/100"
        added = r.get("added_at","?")[:10]
        lines.append(
            f"  {'▶' if slug == active else '·'} <code>{_esc(slug)}</code> "
            f"{score_str}{active_marker}\n"
            f"    Added: {added}"
        )

    lines.append("")
    lines.append("<i>Switch: /repo switch owner/name</i>")
    lines.append("<i>Audit:  /repo audit owner/name</i>")
    lines.append("<i>Remove: /repo remove owner/name</i>")
    return "\n".join(lines)


def _esc(s: str) -> str:
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
