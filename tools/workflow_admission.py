#!/usr/bin/env python3
"""Require the latest completed checks of an exact source before publication."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import ssl
from typing import Any


class AdmissionError(ValueError):
    pass


def require_success(
    runs: list[dict[str, Any]], revision: str, required: tuple[str, ...]
) -> None:
    if not re.fullmatch(r"[a-f0-9]{40}", revision) or not required:
        raise AdmissionError("INVALID_SOURCE_OR_REQUIRED_WORKFLOWS")
    for name in required:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", name):
            raise AdmissionError("INVALID_REQUIRED_WORKFLOW_NAME")
        current = [
            run
            for run in runs
            if run.get("head_sha") == revision and run.get("name") == name
        ]
        if any(
            type(run.get(key)) is not int or run[key] < 1
            for run in current
            for key in ("run_number", "run_attempt")
        ):
            raise AdmissionError("INVALID_WORKFLOW_RUN_METADATA")
        current.sort(
            key=lambda run: (run["run_number"], run["run_attempt"]), reverse=True
        )
        if (
            not current
            or current[0].get("status") != "completed"
            or current[0].get("conclusion") != "success"
        ):
            raise AdmissionError(
                "REQUIRED_EXACT_SOURCE_WORKFLOW_NOT_SUCCESSFUL: " + name
            )


def fetch_runs(repository: str) -> list[dict[str, Any]]:
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", repository):
        raise AdmissionError("INVALID_REPOSITORY")
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise AdmissionError("GITHUB_TOKEN_REQUIRED")
    connection = http.client.HTTPSConnection(
        "api.github.com", context=ssl.create_default_context(), timeout=30
    )
    try:
        connection.request(
            "GET",
            "/repos/" + repository + "/actions/runs?per_page=100",
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "AELEV-workflow-admission",
            },
        )
        response = connection.getresponse()
        if response.status != 200:
            raise AdmissionError("GITHUB_WORKFLOW_QUERY_FAILED")
        body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise AdmissionError("GITHUB_WORKFLOW_RESPONSE_TOO_LARGE")
        document = json.loads(body)
        runs = document.get("workflow_runs") if isinstance(document, dict) else None
        if not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs):
            raise AdmissionError("GITHUB_WORKFLOW_RESPONSE_INVALID")
        return runs
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--require", action="append", required=True)
    args = parser.parse_args()
    try:
        require_success(fetch_runs(args.repository), args.revision, tuple(args.require))
    except AdmissionError as error:
        raise SystemExit(str(error)) from None
    print("Exact-source workflow gates passed")


if __name__ == "__main__":
    main()
