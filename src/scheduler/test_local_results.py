import io
import hashlib
import os
import shutil
import sys
import tempfile
import time
import unittest
import uuid
import zipfile

from werkzeug.datastructures import MultiDict

sys.path.insert(0, os.path.dirname(__file__))
import headnode_service


class TestLocalResultRoutes(unittest.TestCase):
    def setUp(self):
        self.results_root = tempfile.mkdtemp()
        self.original_repos_dir = headnode_service.REPOS_DIR
        self.original_cluster_token = headnode_service.CLUSTER_TOKEN
        self.original_chunk_size = headnode_service.LOCAL_TRANSFER_CHUNK_SIZE
        headnode_service.REPOS_DIR = self.results_root
        headnode_service.CLUSTER_TOKEN = None
        headnode_service.LOCAL_TRANSFER_CHUNK_SIZE = 8
        self.client = headnode_service.app.test_client()
        self.job_id = str(uuid.uuid4())

    def tearDown(self):
        headnode_service.REPOS_DIR = self.original_repos_dir
        headnode_service.CLUSTER_TOKEN = self.original_cluster_token
        headnode_service.LOCAL_TRANSFER_CHUNK_SIZE = self.original_chunk_size
        shutil.rmtree(self.results_root)

    def upload_transfer(self, payload, purpose='results'):
        create_payload = {
            'purpose': purpose,
            'total_size': len(payload),
            'sha256': hashlib.sha256(payload).hexdigest(),
        }
        if purpose == 'results':
            create_payload['job_id'] = self.job_id
        create = self.client.post('/api/local_transfers', json=create_payload)
        self.assertEqual(create.status_code, 201)
        transfer = create.get_json()
        create.close()
        transfer_id = transfer['transfer_id']
        chunk_size = transfer['chunk_size']
        for index, offset in enumerate(range(0, len(payload), chunk_size)):
            chunk = payload[offset:offset + chunk_size]
            response = self.client.put(
                f'/api/local_transfers/{transfer_id}/chunks/{index}',
                data=chunk,
                headers={'X-Chunk-SHA256': hashlib.sha256(chunk).hexdigest()},
                content_type='application/octet-stream',
            )
            self.assertEqual(response.status_code, 200)
            response.close()
        return transfer_id

    def test_sync_results_preserves_multiple_files_with_same_field_name(self):
        response = self.client.post(
            f'/api/jobs/{self.job_id}/sync_results',
            data=MultiDict([
                ('files', (io.BytesIO(b'lock'), 'dvc.lock')),
                ('files', (io.BytesIO(b'{"accuracy": 1}'), 'metrics.json')),
            ]),
            content_type='multipart/form-data',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['synced_files'], 2)
        result_dir = os.path.join(self.results_root, '_local_results', self.job_id)
        self.assertEqual(sorted(os.listdir(result_dir)), ['dvc.lock', 'metrics.json'])

    def test_archive_round_trip_and_cleanup(self):
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, 'w') as archive:
            archive.writestr('results/predictions.jsonl', '{"prediction": 1}\n')
            archive.writestr('metrics.json', '{"accuracy": 1}\n')

        transfer_id = self.upload_transfer(archive_buffer.getvalue())
        upload = self.client.post(
            f'/api/local_transfers/{transfer_id}/complete', json={},
        )
        self.assertEqual(upload.status_code, 200)
        self.assertEqual(upload.get_json()['archived_files'], 2)

        download = self.client.get(f'/api/jobs/{self.job_id}/results')
        self.assertEqual(download.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
            self.assertEqual(
                sorted(archive.namelist()),
                ['metrics.json', 'results/predictions.jsonl'],
            )
        download.close()

        cleanup = self.client.delete(f'/api/jobs/{self.job_id}/results')
        self.assertEqual(cleanup.status_code, 200)
        download_after_cleanup = self.client.get(f'/api/jobs/{self.job_id}/results')
        self.assertEqual(download_after_cleanup.status_code, 404)
        download_after_cleanup.close()

    def test_result_routes_require_cluster_token(self):
        headnode_service.CLUSTER_TOKEN = 'expected-token'
        response = self.client.get(f'/api/jobs/{self.job_id}/results')
        self.assertEqual(response.status_code, 401)
        response.close()

    def test_archive_rejects_parent_directory_member(self):
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, 'w') as archive:
            archive.writestr('../outside.txt', 'not allowed')

        transfer_id = self.upload_transfer(archive_buffer.getvalue())
        response = self.client.post(
            f'/api/local_transfers/{transfer_id}/complete', json={},
        )
        self.assertEqual(response.status_code, 400)
        response.close()
        archive_path = os.path.join(
            self.results_root, '_local_results', f'{self.job_id}.zip'
        )
        self.assertFalse(os.path.exists(archive_path))

    def test_archive_rejects_protected_project_file(self):
        for protected_name in ('.env', '.env.production'):
            archive_buffer = io.BytesIO()
            with zipfile.ZipFile(archive_buffer, 'w') as archive:
                archive.writestr(protected_name, 'must not be restored')

            transfer_id = self.upload_transfer(archive_buffer.getvalue())
            response = self.client.post(
                f'/api/local_transfers/{transfer_id}/complete', json={},
            )
            self.assertEqual(response.status_code, 400)
            response.close()

    def test_stale_completed_results_are_removed(self):
        results_root = os.path.join(self.results_root, '_local_results')
        os.makedirs(results_root)
        stale_archive = os.path.join(results_root, f'{self.job_id}.zip')
        with open(stale_archive, 'wb') as handle:
            handle.write(b'stale')
        stale_time = time.time() - headnode_service.LOCAL_RESULTS_MAX_AGE_SECONDS - 1
        os.utime(stale_archive, (stale_time, stale_time))

        headnode_service._cleanup_stale_local_results()
        self.assertFalse(os.path.exists(stale_archive))

    def test_source_transfer_is_consumed_atomically(self):
        payload = b'chunked-source-archive-content'
        transfer_id = self.upload_transfer(payload, purpose='source')
        complete = self.client.post(
            f'/api/local_transfers/{transfer_id}/complete', json={},
        )
        self.assertEqual(complete.status_code, 200)
        complete.close()

        destination = os.path.join(self.results_root, 'submitted-source.tar.gz')
        self.assertTrue(headnode_service._consume_source_transfer(transfer_id, destination))
        with open(destination, 'rb') as handle:
            self.assertEqual(handle.read(), payload)
        paths = headnode_service._transfer_paths(transfer_id)
        self.assertFalse(os.path.exists(paths['directory']))

    def test_bad_chunk_can_be_retried_without_corrupting_transfer(self):
        payload = b'retry-after-checksum-failure'
        create = self.client.post('/api/local_transfers', json={
            'purpose': 'source',
            'total_size': len(payload),
            'sha256': hashlib.sha256(payload).hexdigest(),
        })
        transfer = create.get_json()
        transfer_id = transfer['transfer_id']
        chunk = payload[:transfer['chunk_size']]
        bad = self.client.put(
            f'/api/local_transfers/{transfer_id}/chunks/0',
            data=chunk,
            headers={'X-Chunk-SHA256': '0' * 64},
            content_type='application/octet-stream',
        )
        self.assertEqual(bad.status_code, 400)

        good = self.client.put(
            f'/api/local_transfers/{transfer_id}/chunks/0',
            data=chunk,
            headers={'X-Chunk-SHA256': hashlib.sha256(chunk).hexdigest()},
            content_type='application/octet-stream',
        )
        self.assertEqual(good.status_code, 200)

    def test_incomplete_transfer_is_rejected_and_can_be_deleted(self):
        payload = b'more-than-one-test-chunk'
        create = self.client.post('/api/local_transfers', json={
            'purpose': 'source',
            'total_size': len(payload),
            'sha256': hashlib.sha256(payload).hexdigest(),
        })
        transfer_id = create.get_json()['transfer_id']
        incomplete = self.client.post(
            f'/api/local_transfers/{transfer_id}/complete', json={},
        )
        self.assertEqual(incomplete.status_code, 409)

        cleanup = self.client.delete(f'/api/local_transfers/{transfer_id}')
        self.assertEqual(cleanup.status_code, 200)
        paths = headnode_service._transfer_paths(transfer_id)
        self.assertFalse(os.path.exists(paths['directory']))


if __name__ == '__main__':
    unittest.main()
