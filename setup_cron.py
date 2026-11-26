#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create the 10-minute keep-awake job on cron-job.org.

Usage (API key supplied at runtime, NEVER written to disk):
    CRON_API_KEY=... python setup_cron.py https://khazna-xxxx.onrender.com/

Reads the official REST API: PUT https://api.cron-job.org/jobs
"""
import os
import sys
import requests

API = "https://api.cron-job.org/jobs"
KEY = os.environ.get("CRON_API_KEY", "").strip()


def main():
    if not KEY:
        raise SystemExit("CRON_API_KEY is required in the environment")
    if len(sys.argv) < 2 or not sys.argv[1].startswith("https://"):
        raise SystemExit("Pass the Render URL: python setup_cron.py https://xxx.onrender.com/")
    url = sys.argv[1].rstrip("/") + "/"
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    payload = {
        "job": {
            "url": url,
            "enabled": True,
            "title": "خزنة keep-awake",
            "saveResponses": False,
            "requestMethod": 0,
            "schedule": {
                "timezone": "UTC",
                "expiresAt": 0,
                "minutes": [-1],
                "hours": [-1],
                "mdays": [-1],
                "months": [-1],
                "wdays": [-1],
            },
        }
    }
    # every 10 minutes: minute values 0..59 step 10
    payload["job"]["schedule"]["minutes"] = list(range(0, 60, 10))
    r = requests.put(API, headers=headers, json=payload, timeout=30)
    print("status:", r.status_code)
    print("response:", r.text[:500])
    if r.status_code in (200, 201):
        print("OK: keep-awake job created")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
