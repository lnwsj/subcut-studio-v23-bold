from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
import sys

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PACKAGE_ROOT / 'app'
sys.path.insert(0, str(APP_DIR))

root = Path(tempfile.mkdtemp(prefix='subcut_api_test_'))
try:
    db_path = root / 'jobs.db'
    workspace = root / 'workspaces'
    os.environ['APP_DB_ENGINE'] = 'sqlite'
    os.environ['APP_DB_PATH'] = str(db_path)
    os.environ['APP_USER_WORKSPACE_ROOT'] = str(workspace)
    os.environ['APP_DOWNLOAD_CACHE_DIR'] = str(root / 'download_cache')
    os.environ['APP_ENABLE_WORKER'] = '1'
    os.environ['APP_WORKER_MAX_CONCURRENCY'] = '1'
    os.environ['APP_WORKER_POLL_INTERVAL'] = '0.2'
    os.environ['APP_AUTH_SECRET'] = 'test-secret-that-is-long-and-not-default'
    os.environ['APP_BROWSER_IDENTITY_SECRET'] = 'test-browser-identity-secret-that-is-stable'
    os.environ['APP_AUTH_REQUIRED'] = '1'

    source = root / 'sample.mp4'
    cmd = [
        'ffmpeg', '-y',
        '-f', 'lavfi', '-i', 'color=c=0x2563eb:s=640x360:r=25:d=5',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=1',
        '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo:d=2',
        '-f', 'lavfi', '-i', 'sine=frequency=660:sample_rate=48000:duration=2',
        '-filter_complex', '[1:a][2:a][3:a]concat=n=3:v=0:a=1[a]',
        '-map', '0:v', '-map', '[a]', '-c:v', 'libx264', '-preset', 'ultrafast',
        '-c:a', 'aac', '-shortest', str(source),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    from fastapi.testclient import TestClient
    from backend.subcut_main import app
    import backend.services.subcut_worker as subcut_worker

    def poll(client, token, job_id, timeout=60):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            response = client.get(f'/api/jobs/{job_id}', headers={'Authorization': f'Bearer {token}'})
            assert response.status_code == 200, response.text
            last = response.json()
            if last['status'] in {'done', 'error', 'cancelled'}:
                return last
            time.sleep(0.2)
        raise AssertionError(f'timeout: {last}')

    with TestClient(app) as client:
        health = client.get('/api/health')
        assert health.status_code == 200, health.text
        assert health.json()['service'] == 'sj88-subcut-studio'

        register = client.post('/api/auth/register', json={
            'display_name': 'Owner Test', 'email': 'owner@example.com', 'password': 'pass1234'
        })
        assert register.status_code == 200, register.text
        register_payload = register.json()
        assert register_payload.get('access_token'), register_payload
        assert register_payload['user']['role'] == 'owner'
        token = register_payload['access_token']
        headers = {'Authorization': f'Bearer {token}'}
        assert client.get('/api/auth/me', headers=headers).status_code == 200

        # Real silence trim: direct upload -> background worker -> history/download.
        created = client.post('/api/jobs', headers=headers, json={
            'name': 'Silence Integration',
            'mode': 'silence_trim_only',
            'settings': {
                'workflow': 'silence', 'trim_silence': True,
                'trim_silence_threshold_db': -40,
                'trim_silence_min_silence_sec': 0.5,
                'trim_silence_margin_sec': 0,
            },
        })
        assert created.status_code == 200, created.text
        silence_id = created.json()['job']['id']
        data = source.read_bytes()
        upload = client.post(
            f'/api/jobs/{silence_id}/upload?append=0&file_index=0&total_files=1&file_size={len(data)}',
            headers=headers,
            files=[('files', ('sample.mp4', data, 'video/mp4'))],
        )
        assert upload.status_code == 200, upload.text
        queued = client.post(f'/api/jobs/{silence_id}/process', headers=headers)
        assert queued.status_code == 200, queued.text
        silence_job = poll(client, token, silence_id)
        assert silence_job['status'] == 'done', silence_job
        metrics = silence_job['result']['runtime_metrics']
        assert metrics['removed_duration_sec'] > 1.0, metrics
        assert silence_job['result']['output_count'] == 1

        history = client.get('/api/history?limit=20', headers=headers)
        assert history.status_code == 200, history.text
        assert any(item['id'] == silence_id for item in history.json()['items'])
        detail = client.get(f'/api/history/{silence_id}', headers=headers)
        assert detail.status_code == 200, detail.text
        output_files = detail.json()['output_files']
        assert output_files and output_files[0]['name'].startswith('output/')
        one = client.get(f"/api/history/{silence_id}/files/{output_files[0]['name']}", headers=headers)
        assert one.status_code == 200 and len(one.content) > 1000
        archive = client.get(f'/api/jobs/{silence_id}/download/output/direct', headers=headers)
        assert archive.status_code == 200 and archive.content[:2] == b'PK'

        # Chunked upload contract and assembly integrity.
        chunked = client.post('/api/jobs', headers=headers, json={
            'name': 'Chunk Upload', 'mode': 'silence_trim_only', 'settings': {'workflow': 'silence'}
        })
        chunked_id = chunked.json()['job']['id']
        chunk_hash = hashlib.sha256(data).hexdigest()
        manifest_hash = hashlib.sha256(chunk_hash.encode()).hexdigest()
        params = (
            f'chunk_index=0&total_chunks=1&total_files=1&file_name=chunked.mp4'
            f'&file_size={len(data)}&file_index=0&chunk_sha256={chunk_hash}'
            f'&chunk_manifest_sha256={manifest_hash}&append=0'
        )
        upload_chunk = client.post(
            f'/api/jobs/{chunked_id}/upload/chunk?{params}', headers=headers,
            files=[('files', ('chunked.mp4.part0', data, 'application/octet-stream'))],
        )
        assert upload_chunk.status_code == 200, upload_chunk.text
        complete = client.post(f'/api/jobs/{chunked_id}/upload/chunked/complete', headers=headers)
        assert complete.status_code == 200, complete.text
        assert complete.json()['files'][0]['sha256'] == hashlib.sha256(data).hexdigest()

        # Mock only the expensive Whisper phase; exercise real subtitle queue/output/history API.
        original_runner = subcut_worker.run_autosu_on_outputs
        def fake_runner(output_paths, settings, *, output_root, log_cb, cancel_check, progress_callback, trim_settings):
            out_dir = Path(output_root) / 'withsub'
            out_dir.mkdir(parents=True, exist_ok=True)
            outputs = []
            items = []
            for index, raw in enumerate(output_paths, start=1):
                progress_callback(index, len(output_paths), raw)
                src = Path(raw)
                dst = out_dir / f'{src.stem}.autosu.mp4'
                shutil.copy2(src, dst)
                (out_dir / f'{src.stem}.autosu.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nทดสอบซับ\n', encoding='utf-8')
                outputs.append(str(dst))
                items.append({'input': str(src), 'output': str(dst), 'ok': True, 'subtitle_trim': {'skipped': True}})
            return {
                'attempted': len(outputs), 'succeeded': len(outputs), 'failed': 0,
                'outputs': outputs, 'items': items, 'errors': [], 'trim_manifests': [],
                'trim_applied_count': 0, 'initial_encoder': 'libx264', 'used_encoder': 'libx264',
                'encoder_fallback_used': False,
            }
        subcut_worker.run_autosu_on_outputs = fake_runner
        try:
            subtitle = client.post('/api/jobs', headers=headers, json={
                'name': 'Subtitle Integration', 'mode': 'autosu_only',
                'settings': {'workflow': 'subtitle', 'trim_silence': False, 'subtitle_language': 'th'},
            })
            subtitle_id = subtitle.json()['job']['id']
            up = client.post(
                f'/api/jobs/{subtitle_id}/upload?append=0&file_index=0&total_files=1&file_size={len(data)}',
                headers=headers, files=[('files', ('subtitle.mp4', data, 'video/mp4'))],
            )
            assert up.status_code == 200, up.text
            assert client.post(f'/api/jobs/{subtitle_id}/process', headers=headers).status_code == 200
            subtitle_job = poll(client, token, subtitle_id)
            assert subtitle_job['status'] == 'done', subtitle_job
            subtitle_detail = client.get(f'/api/history/{subtitle_id}', headers=headers).json()
            names = [item['name'] for item in subtitle_detail['output_files']]
            assert any(name.endswith('.autosu.mp4') for name in names), names
            assert any(name.endswith('.autosu.srt') for name in names), names
        finally:
            subcut_worker.run_autosu_on_outputs = original_runner

        # A second account preserves approval workflow.
        second = client.post('/api/auth/register', json={
            'display_name': 'Pending User', 'email': 'pending@example.com', 'password': 'pass1234'
        })
        assert second.status_code == 200, second.text
        assert second.json().get('pending_approval') is True

        members = client.get('/api/members?status=all&limit=20', headers=headers)
        assert members.status_code == 200, members.text
        pending_row = next(item for item in members.json()['members'] if item['email'] == 'pending@example.com')
        assert pending_row['account_status'] == 'pending'
        approved = client.patch(f"/api/members/{pending_row['id']}", headers=headers, json={'action': 'approve'})
        assert approved.status_code == 200, approved.text
        pending_login = client.post('/api/auth/login', json={'email': 'pending@example.com', 'password': 'pass1234'})
        assert pending_login.status_code == 200, pending_login.text
        denied_self = client.patch(f"/api/members/{register_payload['user']['id']}", headers=headers, json={'action': 'disable'})
        assert denied_self.status_code == 409, denied_self.text

        print(json.dumps({
            'ok': True,
            'silence_job': silence_id,
            'removed_duration_sec': metrics['removed_duration_sec'],
            'output_files': len(output_files),
            'chunked_upload': True,
            'subtitle_queue_mocked_engine': True,
            'first_user_bootstrap': True,
            'second_user_pending': True,
            'member_approval_ui_api': True,
        }, ensure_ascii=False, indent=2))
finally:
    shutil.rmtree(root, ignore_errors=True)
