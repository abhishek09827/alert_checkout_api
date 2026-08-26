#!/usr/bin/env python3
"""Deployment record script for OpsPilot.ai compatibility.

Appends deployment records to ops/deployments.json after a release.
Each record includes: id, timestamp, service, environment, version, commit_sha, status, deployed_by.
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


DEPLOYMENTS_FILE = Path("ops/deployments.json")
SERVICE_NAME = "checkout-api"


def load_deployments() -> List[Dict[str, Any]]:
    """Load existing deployment records."""
    if DEPLOYMENTS_FILE.exists():
        try:
            with open(DEPLOYMENTS_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return []
    return []


def save_deployments(deployments: List[Dict[str, Any]]) -> None:
    """Save deployment records to file."""
    DEPLOYMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DEPLOYMENTS_FILE, "w") as f:
        json.dump(deployments, f, indent=2, default=str)


def create_deployment_record(
    environment: str,
    version: str,
    commit_sha: str,
    status: str,
    deployed_by: str,
) -> Dict[str, Any]:
    """Create a new deployment record."""
    return {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": SERVICE_NAME,
        "environment": environment,
        "version": version,
        "commit_sha": commit_sha,
        "status": status,
        "deployed_by": deployed_by,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Record deployment for OpsPilot.ai")
    parser.add_argument("--environment", required=True, help="Deployment environment (staging, production)")
    parser.add_argument("--version", required=True, help="Application version")
    parser.add_argument("--commit-sha", required=True, help="Git commit SHA")
    parser.add_argument("--status", required=True, choices=["success", "failed", "rolled_back"], help="Deployment status")
    parser.add_argument("--deployed-by", required=True, help="Who/what deployed this (user, CI system)")
    args = parser.parse_args()

    deployments = load_deployments()
    record = create_deployment_record(
        environment=args.environment,
        version=args.version,
        commit_sha=args.commit_sha,
        status=args.status,
        deployed_by=args.deployed_by,
    )
    deployments.append(record)
    save_deployments(deployments)

    print(f"Deployment recorded: {record['id']}")
    print(f"  Service: {record['service']}")
    print(f"  Environment: {record['environment']}")
    print(f"  Version: {record['version']}")
    print(f"  Commit: {record['commit_sha']}")
    print(f"  Status: {record['status']}")
    print(f"  Deployed by: {record['deployed_by']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())