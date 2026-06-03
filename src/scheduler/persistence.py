import sqlite3
import os
from contextlib import contextmanager

def get_db_path():
    return os.environ.get("CLUSTER_DB_PATH", "cluster_scheduler.db")

DB_PATH = get_db_path()

def init_db():
    conn = sqlite3.connect(get_db_path(), timeout=10.0)
    conn.execute('pragma journal_mode=wal')
    conn.execute('pragma synchronous=normal')
    cursor = conn.cursor()

    # Workers Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS workers (
            worker_id TEXT PRIMARY KEY,
            hostname TEXT,
            service_url TEXT,
            total_ram_gb REAL,
            available_ram_gb REAL,
            total_storage_gb REAL,
            available_storage_gb REAL,
            total_vram_gb REAL,
            gpu_count INTEGER,
            gpu_name TEXT,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'online'
        )
    ''')

    # Add service_url if it doesn't exist (migration)
    try:
        cursor.execute('ALTER TABLE workers ADD COLUMN service_url TEXT')
    except sqlite3.OperationalError:
        pass # Already exists

    # Add storage columns if they don't exist (migration)
    try:
        cursor.execute('ALTER TABLE workers ADD COLUMN total_storage_gb REAL')
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute('ALTER TABLE workers ADD COLUMN available_storage_gb REAL')
    except sqlite3.OperationalError:
        pass

    # Add GPU/VRAM columns if they don't exist (migration)
    try:
        cursor.execute('ALTER TABLE workers ADD COLUMN total_vram_gb REAL')
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute('ALTER TABLE workers ADD COLUMN gpu_name TEXT')
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute('ALTER TABLE workers ADD COLUMN gpu_count INTEGER')
    except sqlite3.OperationalError:
        pass

    # Jobs Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            repo TEXT,
            branch TEXT,
            commit_hash TEXT,
            ram_required_gb REAL,
            status TEXT DEFAULT 'pending',
            worker_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            exit_code INTEGER,
            viewer_port INTEGER,
            max_runtime_hours REAL,
            exposed_port INTEGER,
            gh_run_id TEXT,
            required_hashes TEXT,
            p2p_url TEXT,
            gh_token TEXT,
            custom_web_app INTEGER DEFAULT 0,
            FOREIGN KEY (worker_id) REFERENCES workers (worker_id)
        )
    ''')

    # commit_hash migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN commit_hash TEXT')
    except sqlite3.OperationalError:
        pass # Already exists

    # viewer_port migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN viewer_port INTEGER')
    except sqlite3.OperationalError:
        pass # Already exists

    # required_hashes migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN required_hashes TEXT')
    except sqlite3.OperationalError:
        pass # Already exists

    # p2p_url migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN p2p_url TEXT')
    except sqlite3.OperationalError:
        pass # Already exists

    # gh_token migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN gh_token TEXT')
    except sqlite3.OperationalError:
        pass # Already exists

    # env_vars migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN env_vars TEXT')
    except sqlite3.OperationalError:
        pass # Already exists

    # username migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN username TEXT')
    except sqlite3.OperationalError:
        pass # Already exists

    # max_runtime_hours migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN max_runtime_hours REAL')
    except sqlite3.OperationalError:
        pass

    # exposed_port migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN exposed_port INTEGER')
    except sqlite3.OperationalError:
        pass

    # gh_run_id migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN gh_run_id TEXT')
    except sqlite3.OperationalError:
        pass

    # custom_web_app migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN custom_web_app INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass

    # vram_required_gb migration
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN vram_required_gb REAL')
    except sqlite3.OperationalError:
        pass

    # available_vram_gb migration (dynamic free VRAM reported by worker heartbeats)
    try:
        cursor.execute('ALTER TABLE workers ADD COLUMN available_vram_gb REAL DEFAULT 0')
    except sqlite3.OperationalError:
        pass

    # allowed_workers migration (JSON list of hostnames to restrict job execution)
    try:
        cursor.execute('ALTER TABLE jobs ADD COLUMN allowed_workers TEXT')
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()

@contextmanager
def get_db_conn():
    conn = sqlite3.connect(get_db_path(), timeout=10.0)
    conn.execute('pragma journal_mode=wal')
    conn.execute('pragma synchronous=normal')
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
