"""Helpers for building throwaway repositories in tests.

Detectors read real files and real git history, so testing them against mocks
would test the mocks. These helpers build actual directories — and, where a
test needs history, actual git repositories — in a temp dir that is cleaned up
afterwards.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Dict, Optional, Sequence

from agentsmith.repo import Repo


class FixtureRepo:
    """A disposable repository on disk."""

    def __init__(self, git: bool = False) -> None:
        self.root = tempfile.mkdtemp(prefix="agentsmith-test-")
        self.has_git = False
        if git:
            self.init_git()

    # -- construction ----------------------------------------------------

    def write(self, relative: str, content: str = "") -> "FixtureRepo":
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return self

    def write_json(self, relative: str, data: dict) -> "FixtureRepo":
        return self.write(relative, json.dumps(data, indent=2))

    def write_many(self, paths: Sequence[str], content: str = "") -> "FixtureRepo":
        for path in paths:
            self.write(path, content)
        return self

    # -- git -------------------------------------------------------------

    def init_git(self) -> "FixtureRepo":
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        self._git("config", "commit.gpgsign", "false")
        self.has_git = True
        return self

    def commit(self, subject: str, touch: Optional[str] = None) -> "FixtureRepo":
        if not self.has_git:
            self.init_git()
        target = touch or "log.txt"
        path = os.path.join(self.root, target)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(subject + "\n")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", subject, "--no-verify")
        return self

    def branch(self, name: str) -> "FixtureRepo":
        self._git("branch", name)
        return self

    def _git(self, *args: str) -> None:
        subprocess.run(
            ("git", "-C", self.root, *args),
            capture_output=True,
            check=False,
            timeout=20,
        )

    # -- use -------------------------------------------------------------

    def repo(self) -> Repo:
        return Repo(self.root)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> "FixtureRepo":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()


def keys(findings) -> Dict[str, object]:
    """Index findings by key, for readable assertions."""
    return {finding.key: finding for finding in findings}


TS_SOURCE = """\
import { useState } from 'react';
import { helper } from '@/lib/helper';

export const Widget = () => {
  const [value, setValue] = useState('');
  const label = helper('widget', 'primary');
  return value + label;
};
"""

TS_SOURCE_NO_SEMI = """\
import { useState } from 'react'
import { helper } from '@/lib/helper'

export const Widget = () => {
  const [value, setValue] = useState('')
  const label = helper('widget', 'primary')
  return value + label
}
"""

PY_SOURCE = '''\
"""A module with a docstring."""


def compute(value: int) -> int:
    """Return the value doubled."""
    return value * 2
'''
