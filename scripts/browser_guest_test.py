from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PACKAGE_ROOT / "app"
sys.path.insert(0, str(APP_DIR))

root = Path(tempfile.mkdtemp(prefix="subcut_browser_guest_"))
try:
    os.environ.update(
        {
            "APP_DB_ENGINE": "sqlite",
            "APP_DB_PATH": str(root / "jobs.db"),
            "APP_USER_WORKSPACE_ROOT": str(root / "workspaces"),
            "APP_DOWNLOAD_CACHE_DIR": str(root / "downloads"),
            "APP_ENABLE_WORKER": "0",
            "APP_AUTH_SECRET": "browser-guest-test-secret-long-enough",
            "APP_BROWSER_IDENTITY_SECRET": "browser-identity-test-secret-long-enough",
            "APP_AUTH_REQUIRED": "1",
        }
    )

    from fastapi.testclient import TestClient
    from backend.subcut_main import app

    with TestClient(app) as chrome_a:
        guest = chrome_a.post("/api/auth/browser", json={"browser_label": "Chrome A"})
        assert guest.status_code == 200, guest.text
        guest_payload = guest.json()
        assert guest_payload["user"]["is_guest"] is True
        guest_id = guest_payload["user"]["id"]
        browser_key_a = guest_payload["browser_key"]
        guest_headers = {"Authorization": f"Bearer {guest_payload['access_token']}"}

        created = chrome_a.post(
            "/api/jobs",
            headers=guest_headers,
            json={"name": "Guest preserved job", "mode": "silence_trim_only", "settings": {}},
        )
        assert created.status_code == 200, created.text
        guest_job_id = created.json()["job"]["id"]

        # The HttpOnly cookie alone must restore the same Chrome identity.
        cookie_restored = chrome_a.post("/api/auth/browser", json={})
        assert cookie_restored.status_code == 200, cookie_restored.text
        assert cookie_restored.json()["user"]["id"] == guest_id

        # The localStorage key is a fallback for environments where cookies are cleared selectively.
        restored = chrome_a.post("/api/auth/browser", json={"browser_key": browser_key_a})
        assert restored.status_code == 200, restored.text
        assert restored.json()["user"]["id"] == guest_id
        restored_headers = {"Authorization": f"Bearer {restored.json()['access_token']}"}
        jobs = chrome_a.get("/api/jobs", headers=restored_headers)
        assert any(item["id"] == guest_job_id for item in jobs.json()["jobs"])

        claimed = chrome_a.post(
            "/api/auth/register",
            headers=restored_headers,
            json={
                "display_name": "Owner Browser",
                "email": "owner-browser@example.com",
                "password": "pass1234",
                "browser_key": browser_key_a,
            },
        )
        assert claimed.status_code == 200, claimed.text
        claimed_payload = claimed.json()
        assert claimed_payload["user"]["id"] == guest_id
        assert claimed_payload["user"]["role"] == "owner"
        assert claimed_payload["user"]["is_guest"] is False
        assert claimed_payload["jobs_preserved"] == 1
        owner_headers = {"Authorization": f"Bearer {claimed_payload['access_token']}"}
        assert chrome_a.get(f"/api/jobs/{guest_job_id}", headers=owner_headers).status_code == 200

        with TestClient(app) as chrome_b:
            guest_b = chrome_b.post("/api/auth/browser", json={"browser_label": "Chrome B"}).json()
            browser_key_b = guest_b["browser_key"]
            guest_b_headers = {"Authorization": f"Bearer {guest_b['access_token']}"}
            created_b = chrome_b.post(
                "/api/jobs",
                headers=guest_b_headers,
                json={"name": "Merge on login", "mode": "autosu_only", "settings": {}},
            )
            assert created_b.status_code == 200, created_b.text
            guest_b_job_id = created_b.json()["job"]["id"]

            linked = chrome_b.post(
                "/api/auth/login",
                headers=guest_b_headers,
                json={
                    "email": "owner-browser@example.com",
                    "password": "pass1234",
                    "browser_key": browser_key_b,
                },
            )
            assert linked.status_code == 200, linked.text
            linked_payload = linked.json()
            assert linked_payload["guest_jobs_migrated"] == 1
            linked_headers = {"Authorization": f"Bearer {linked_payload['access_token']}"}
            assert chrome_b.get(f"/api/jobs/{guest_b_job_id}", headers=linked_headers).status_code == 200

            auto_member = chrome_b.post("/api/auth/browser", json={"browser_key": browser_key_b})
            assert auto_member.status_code == 200, auto_member.text
            assert auto_member.json()["user"]["id"] == guest_id
            assert auto_member.json()["user"]["is_guest"] is False

        members = chrome_a.get("/api/members?limit=100", headers=owner_headers)
        assert members.status_code == 200, members.text
        assert len(members.json()["members"]) == 1
        assert all(item["role"] != "guest" for item in members.json()["members"])

        fresh = chrome_a.post("/api/auth/browser", json={"force_new": True})
        assert fresh.status_code == 200, fresh.text
        assert fresh.json()["user"]["is_guest"] is True
        assert fresh.json()["user"]["id"] != guest_id

        print(
            json.dumps(
                {
                    "ok": True,
                    "guest_restore_same_browser": True,
                    "http_only_cookie_restore": True,
                    "claim_preserves_jobs": claimed_payload["jobs_preserved"],
                    "login_merges_guest_jobs": linked_payload["guest_jobs_migrated"],
                    "browser_auto_member": True,
                    "admin_list_hides_guests": True,
                    "fresh_guest_after_logout": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
finally:
    shutil.rmtree(root, ignore_errors=True)
