#!/usr/bin/env python3
"""
NasTech Guardian — State Machine
Manages the sequential pipeline state with persistence.

States:
  IDLE → VERIFY_REPO → IDENTIFY_REPO → SCAN → BUILD → FIX → VERIFY → RELEASE → NOTIFY → COMPLETE
  Any state → FAILED (on error)
"""

import json
import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional


class GuardianState(str, Enum):
    IDLE          = "IDLE"
    VERIFY_REPO   = "VERIFY_REPO"
    IDENTIFY_REPO = "IDENTIFY_REPO"
    SCAN          = "SCAN"
    BUILD         = "BUILD"
    FIX           = "FIX"
    VERIFY        = "VERIFY"
    RELEASE       = "RELEASE"
    NOTIFY        = "NOTIFY"
    COMPLETE      = "COMPLETE"
    FAILED        = "FAILED"
    WAITING       = "WAITING"


# Legal transitions — strictly sequential
TRANSITIONS = {
    GuardianState.IDLE:          GuardianState.VERIFY_REPO,
    GuardianState.VERIFY_REPO:   GuardianState.IDENTIFY_REPO,
    GuardianState.IDENTIFY_REPO: GuardianState.SCAN,
    GuardianState.SCAN:          GuardianState.BUILD,
    GuardianState.BUILD:         GuardianState.FIX,
    GuardianState.FIX:           GuardianState.VERIFY,
    GuardianState.VERIFY:        GuardianState.RELEASE,
    GuardianState.RELEASE:       GuardianState.NOTIFY,
    GuardianState.NOTIFY:        GuardianState.COMPLETE,
    GuardianState.WAITING:       GuardianState.FIX,
}


class StateMachine:
    """
    NasTech Guardian state machine.
    Persists state to a JSON file for cross-job access.
    """

    def __init__(self, state_file: str = "guardian_state.json"):
        self.state_file = Path(state_file)
        self._data: dict = {}
        self._load()

    def _load(self):
        if self.state_file.exists():
            try:
                self._data = json.loads(self.state_file.read_text())
            except json.JSONDecodeError:
                self._data = {}
        if "state" not in self._data:
            self._data["state"] = GuardianState.IDLE
        if "history" not in self._data:
            self._data["history"] = []

    def _save(self):
        self._data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.state_file.write_text(json.dumps(self._data, indent=2, default=str))

    @property
    def state(self) -> GuardianState:
        return GuardianState(self._data.get("state", GuardianState.IDLE))

    @property
    def profile(self) -> dict:
        return self._data.get("profile", {})

    def initialize(self, org: str, repo: str, sha: str, run_id: str, event: str):
        """Initialize a new Guardian run."""
        self._data = {
            "guardian_version": "1.0.0",
            "organization":     org,
            "repository":       repo,
            "sha":              sha,
            "run_id":           run_id,
            "event":            event,
            "state":            GuardianState.IDLE,
            "history":          [],
            "stage_results":    {},
            "started_at":       datetime.now(timezone.utc).isoformat(),
            "guardian_enabled": True,
        }
        self._save()
        return self

    def advance(self, result: dict = None) -> GuardianState:
        """Advance to next state. Returns new state."""
        current = self.state
        next_state = TRANSITIONS.get(current)
        if next_state is None:
            raise ValueError(f"No transition defined from state: {current}")

        entry = {
            "from":      current,
            "to":        next_state,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "result":    result or {},
        }
        self._data["history"].append(entry)
        self._data["state"] = next_state

        if result:
            self._data["stage_results"][current] = result

        self._save()
        return GuardianState(next_state)

    def fail(self, reason: str, stage: str = None):
        """Mark the pipeline as failed and stop."""
        current = self.state
        entry = {
            "from":      current,
            "to":        GuardianState.FAILED,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason":    reason,
            "stage":     stage or current,
        }
        self._data["history"].append(entry)
        self._data["state"]       = GuardianState.FAILED
        self._data["failed_at"]   = datetime.now(timezone.utc).isoformat()
        self._data["fail_reason"] = reason
        self._data["fail_stage"]  = stage or current
        self._save()

    def wait(self, reason: str):
        """Pause pipeline — waiting for human approval or retry."""
        self._data["state"]     = GuardianState.WAITING
        self._data["wait_reason"] = reason
        self._data["waiting_since"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def complete(self):
        """Mark pipeline complete."""
        self._data["state"]        = GuardianState.COMPLETE
        self._data["completed_at"] = datetime.now(timezone.utc).isoformat()
        elapsed = None
        if "started_at" in self._data:
            start = datetime.fromisoformat(self._data["started_at"])
            now   = datetime.now(timezone.utc)
            elapsed = str(now - start)
        self._data["elapsed"] = elapsed
        self._save()

    def set_profile(self, profile: dict):
        """Store the repository identity profile."""
        self._data["profile"] = profile
        self._save()

    def set_stage_result(self, stage: str, result: dict):
        """Store per-stage results."""
        if "stage_results" not in self._data:
            self._data["stage_results"] = {}
        self._data["stage_results"][stage] = result
        self._save()

    def summary(self) -> dict:
        """Return a human-readable pipeline summary."""
        results = self._data.get("stage_results", {})
        return {
            "state":      self.state,
            "org":        self._data.get("organization"),
            "repo":       self._data.get("repository"),
            "sha":        self._data.get("sha", "")[:7],
            "stages":     {s: r.get("status", "?") for s, r in results.items()},
            "elapsed":    self._data.get("elapsed"),
            "failed_at":  self._data.get("fail_stage"),
            "fail_reason": self._data.get("fail_reason"),
        }

    def to_json(self) -> str:
        return json.dumps(self._data, indent=2, default=str)


def load_state(path: str = "guardian_state.json") -> StateMachine:
    sm = StateMachine(path)
    return sm


def create_state(org: str, repo: str, sha: str, run_id: str,
                 event: str, path: str = "guardian_state.json") -> StateMachine:
    sm = StateMachine(path)
    sm.initialize(org, repo, sha, run_id, event)
    return sm
