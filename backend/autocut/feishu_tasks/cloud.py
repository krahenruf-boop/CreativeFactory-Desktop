"""Official Quark CLI adapter. Transfers use argument arrays, never shell input."""
import json
import math
import os
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from ..file_storage import VIDEO_EXTENSIONS, sha256


class CloudError(ValueError):
    def __init__(self, code, notice=''):
        super().__init__('QUARK_' + str(code))
        self.notice = notice


def share_url(value):
    value = str(value).strip()
    # Quark's Copy link action includes prose and an app token. Extract only
    # an unambiguous official HTTPS URL; never send pasted prose to the CLI.
    links = re.findall(r'https://pan\.quark\.cn/s/[A-Za-z0-9]+/?(?:\?[^\s<>\"\u3000-\u303f\uff00-\uffef]*)?(?=$|[\s<>\"\u3000-\u303f\uff00-\uffef])', value)
    if len(set(links)) == 1:
        value = links[0]
    parsed = urlparse(value)
    if parsed.scheme != 'https' or parsed.netloc != 'pan.quark.cn' or not re.fullmatch(r'/s/[A-Za-z0-9]+/?', parsed.path):
        raise ValueError('QUARK_LINK_INVALID')
    return parsed.geturl()


def share_passcode(value):
    match = re.search(r'(?:提取码|提取密码|访问码)\s*[:：]?\s*([A-Za-z0-9]{4})(?![A-Za-z0-9])', str(value))
    return match[1] if match else ''


def natural(value):
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold()) for part in re.split(r'(\d+)', value))


class QuarkCloud:
    def __init__(self, cli, work_root, stop=None, runner=None, notice=None):
        self.cli, self.work_root = Path(cli), Path(work_root)
        self.node = shutil.which('node') or '/opt/homebrew/bin/node'
        self.stop = stop or threading.Event()
        self.runner = runner
        self.notice = notice or (lambda message, work: None)
        # Background callbacks have no chat prompt. Do not fabricate one or
        # manufacture Agent environment variables; inherit the real launcher.
        self.session_id = str(int(time.time())) + '-' + secrets.token_hex(3)

    def run(self, args, task_dir=None, observe=lambda row: None, timeout=1800, notify=True):
        if self.runner:
            return self.runner(args, task_dir, observe)
        if not self.cli.is_file() or not Path(self.node).is_file():
            raise ValueError('QUARK_NOT_INSTALLED')
        directory = Path(task_dir or self.work_root)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        process = subprocess.Popen([self.node, str(self.cli), *map(str, args), '--session-id', self.session_id], cwd=directory,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        messages = queue.Queue()

        def read():
            for line in process.stdout:
                messages.put(line)
            messages.put(None)

        threading.Thread(target=read, daemon=True).start()
        deadline, rows, ended = time.monotonic() + timeout, [], False
        try:
            while not ended:
                if self.stop.is_set():
                    raise ValueError('FEISHU_TASK_STOPPED')
                if time.monotonic() > deadline:
                    raise CloudError('TIMEOUT')
                try:
                    line = messages.get(timeout=1)
                except queue.Empty:
                    continue
                if line is None:
                    ended = True
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                rows.append(row)
                observe(row)
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            process.stdout.close()
        result = next((r for r in reversed(rows) if r.get('type') == 'result'), None)
        message = ((result or {}).get('notice') or {}).get('text')
        if message and notify:
            self.notice(message, directory)
        if not result or result.get('code') != 0:
            raise CloudError(result.get('code', 'FORMAT') if result else 'FORMAT', (result or {}).get('notice', {}).get('text', ''))
        return result, rows

    def health(self):
        """Check the official CLI in this process's actual inherited context.

        Never return account details or credentials. Health notices belong in
        local connection status, not in an unrelated task's cloud directory.
        """
        try:
            result, _ = self.run(['get-user-info'], self.work_root / '_health', timeout=30, notify=False)
            return dict(ready=True, error='', notice=(result.get('notice') or {}).get('text', ''), checked_at=time.time())
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            code = str(error)
            if not re.fullmatch(r'QUARK_[A-Z0-9_-]+', code):
                code = 'QUARK_HEALTH_FAILED'
            return dict(ready=False, error=code, notice=getattr(error, 'notice', ''), checked_at=time.time())

    def directory(self, path, work):
        result, _ = self.run(['create-folder', '--dir-path', path, '--parent-fid', '0'], work)
        fid = (result.get('data') or {}).get('fid')
        if not fid:
            raise CloudError('DIRECTORY_FORMAT')
        return str(fid)

    def browse(self, fid, work):
        result, rows = self.run(['browse', '--parent-fid', fid, '--all', '--task-dir', str(work)], work)
        artifact = (result.get('data') or {}).get('file_path')
        if not artifact:
            artifact = next((r.get('file_path') or r.get('path') or (r.get('data') or {}).get('file_path') for r in rows if r.get('type') == 'artifact'), None)
        if not artifact:
            raise CloudError('BROWSE_ARTIFACT_MISSING')
        path = Path(artifact).resolve()
        if not path.is_relative_to(Path(work).resolve()) or path.is_symlink():
            raise ValueError('FEISHU_TASK_PATH_UNSAFE')
        values = []
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if row.get('type') == 'result':
                continue
            values.append(self.normalize(row.get('data', row)))
        return values

    def share_files(self, url, passcode, work):
        if passcode:
            parsed = urlparse(url)
            query = dict(parse_qsl(parsed.query))
            query['pwd'] = passcode
            url = urlunparse(parsed._replace(query=urlencode(query)))
        # The official CLI's child-directory requests read the URL's pwd query;
        # --passcode alone succeeds at the root but fails on child directories.
        directory, page, values = '0', 1, []
        while page <= 100:
            args = ['share-detail', '--url', url, '--pdir-fid', directory, '--page', str(page), '--size', '50']
            if passcode:
                args += ['--passcode', passcode]
            result, rows = self.run(args, work)
            data = result.get('data') or {}
            files = data.get('file_list') or data.get('files') or data.get('list')
            if files is None:
                files = [r['data'] for r in rows if r.get('type') == 'list']
            if not isinstance(files, list):
                raise CloudError('SHARE_FORMAT')
            values.extend(self.normalize(row) for row in files)
            if not data.get('has_more', data.get('has_next_page', False)):
                if directory == '0' and len(values) == 1 and self.is_directory(values[0]):
                    directory, page, values = str(values[0]['fid']), 1, []
                    continue
                return values
            page += 1
        raise CloudError('SHARE_TOO_LARGE')

    @staticmethod
    def is_directory(row):
        return row.get('dir') is True or row.get('is_dir') is True or row.get('file') is False or row.get('type') in {'folder', 'dir'} or str(row.get('file_type', '')) == '0'

    @staticmethod
    def normalize(row):
        # CLI share-detail and browse have different public FileVO shapes.
        # Do not persist share_fid_token, signed download URLs or user tokens.
        return dict(fid=str(row.get('fid') or ''), file_name=row.get('file_name') or row.get('filename') or '',
                    size=int(row.get('size') or row.get('file_size') or 0), dir=QuarkCloud.is_directory(row),
                    updated_at=row.get('updated_at'), sha1=row.get('sha1') or '')

    def intake(self, task, root, checkpoint):
        local_key = task.get('intake_key') or task['id']
        if not re.fullmatch(r'CF-[0-9]{8}-[A-F0-9]{8}(?:-R[0-9]+)?', local_key):
            raise ValueError('FEISHU_TASK_PATH_UNSAFE')
        folder = Path(root) / local_key
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if folder.is_symlink() or folder.resolve().parent != Path(root).resolve():
            raise ValueError('FEISHU_TASK_PATH_UNSAFE')
        work = self.work_root / task['id']
        work.mkdir(parents=True, exist_ok=True, mode=0o700)
        media = dict(task.get('media') or {})
        if not media.get('manifest'):
            url = share_url(task['share_url'])
            if not media.get('source_files'):
                files = self.share_files(url, task.get('passcode', ''), work)
                if any(self.is_directory(row) for row in files):
                    raise ValueError('QUARK_FLAT_FOLDER_REQUIRED')
                files = [r for r in files if Path(r.get('file_name', '')).suffix.lower() in VIDEO_EXTENSIONS]
                if len(files) != task['expected_count']:
                    raise ValueError('QUARK_SOURCE_COUNT_' + str(len(files)))
                names = [r['file_name'] for r in files]
                if len({n.casefold() for n in names}) != len(names) or any(Path(n).name != n or n.startswith('.') or '\\' in n or '\x00' in n for n in names):
                    raise ValueError('FEISHU_TASK_PATH_UNSAFE')
                files.sort(key=lambda r: natural(r['file_name']))
                media['source_files'] = files
                checkpoint(media)
            if not media.get('cloud_folder'):
                media['cloud_folder'] = self.directory('/Creative Factory/接单/' + local_key, work)
                checkpoint(media)
            if not media.get('saved'):
                args = ['saveas', '--url', url, '--fid-list', ','.join(str(r['fid']) for r in media['source_files']), '--to-pdir-fid', media['cloud_folder']]
                if task.get('passcode'):
                    args += ['--passcode', task['passcode']]
                try:
                    self.run(args, work)
                except CloudError as error:
                    # Quark forbids copying one's own share back into the same
                    # account. Its files are already owned and can be downloaded
                    # directly through the official download command.
                    if str(error) != 'QUARK_41017':
                        raise
                    media['own_share'] = True
                media['saved'] = True
                checkpoint(media)
            if not media.get('local_files'):
                files = media['source_files'] if media.get('own_share') else self.browse(media['cloud_folder'], work)
                if len(files) != len(media['source_files']):
                    raise ValueError('QUARK_SAVED_COUNT_MISMATCH')
                by_name = {r['file_name']: r for r in files}
                media['local_files'] = []
                for row in media['source_files']:
                    saved = by_name.get(row['file_name'])
                    if not saved or int(saved.get('size', 0)) != int(row.get('size', 0)):
                        raise ValueError('QUARK_SOURCE_CHANGED')
                    media['local_files'].append(saved)
                checkpoint(media)
            completed = dict(media.get('downloads') or {})
            manifest = []
            from app.s3.headless_media import probe
            for row in media['local_files']:
                name, size = row['file_name'], int(row.get('size', 0))
                target = folder / name
                state = completed.get(name, {})
                if not (state.get('sha256') and target.is_file() and not target.is_symlink() and target.stat().st_size == size and sha256(target) == state['sha256']):
                    import hashlib
                    temporary = work / ('download-' + hashlib.sha256(str(row['fid']).encode()).hexdigest()[:20])
                    temporary.mkdir(exist_ok=True)

                    def observe(value):
                        record = (value.get('data') or {}).get('recordId')
                        if record:
                            completed[name] = dict(completed.get(name, {}), record_id=record)
                            media['downloads'] = completed
                            checkpoint(media)

                    args = ['download', 'resume', '--record-id', state['record_id'], '--output-dir', str(temporary)] if state.get('record_id') else ['download', '--fid', str(row['fid']), '--output-dir', str(temporary)]
                    self.run(args, work, observe)
                    candidates = [p for p in temporary.iterdir() if p.is_file() and not p.is_symlink() and p.stat().st_size == size and p.suffix.lower() == Path(name).suffix.lower()]
                    if len(candidates) != 1 or size <= 0:
                        raise ValueError('QUARK_DOWNLOAD_INCOMPLETE')
                    if target.exists():
                        if target.is_symlink() or sha256(target) != sha256(candidates[0]):
                            raise ValueError('STORAGE_PATH_CONFLICT')
                    else:
                        os.replace(candidates[0], target)
                    completed[name] = dict(completed.get(name, {}), sha256=sha256(target), size=size)
                    media['downloads'] = completed
                    checkpoint(media)
                info = probe(target)
                video = next((s for s in info.get('streams', []) if s.get('codec_type') == 'video'), None)
                duration = float((info.get('format') or {}).get('duration') or (video or {}).get('duration') or 0)
                if not video or not math.isfinite(duration) or duration <= 0 or not video.get('width') or not video.get('height'):
                    raise ValueError('QUARK_VIDEO_UNREADABLE')
                manifest.append(dict(name=name, path=str(target), fid=row['fid'], size=size, sha256=sha256(target), duration=duration, width=video['width'], height=video['height']))
            media['manifest'] = manifest
            checkpoint(media)
        for entry in media['manifest']:
            path = Path(entry['path'])
            if path.parent != folder or path.is_symlink() or not path.is_file() or path.stat().st_size != entry['size'] or sha256(path) != entry['sha256']:
                raise ValueError('SOURCE_HASH_CHANGED')
        return folder, media

    def deliver(self, task, outputs, checkpoint):
        work = self.work_root / task['id']
        work.mkdir(parents=True, exist_ok=True, mode=0o700)
        delivery = dict(task.get('delivery') or {})
        if not delivery.get('folder_fid'):
            delivery['folder_fid'] = self.directory('/Creative Factory/成片/' + task['id'], work)
            checkpoint(delivery)
        uploaded = dict(delivery.get('uploaded') or {})
        for rank, file in outputs.items():
            path = Path(file)
            if path.is_symlink() or not path.is_file() or path.suffix.lower() != '.mp4':
                raise ValueError('OUTPUT_NOT_FOUND')
            digest = sha256(path)
            if uploaded.get(rank, {}).get('sha256') == digest and uploaded[rank].get('fid'):
                continue

            def observe(row):
                data = row.get('data') or {}
                if row.get('type') == 'list' and row.get('code') == 0 and data.get('fileId'):
                    uploaded[rank] = dict(fid=data['fileId'], sha256=digest, name=path.name, size=path.stat().st_size)
                    delivery['uploaded'] = uploaded
                    checkpoint(delivery)

            result, rows = self.run(['upload', str(path), '--parent-fid', delivery['folder_fid']], work, observe)
            if not uploaded.get(rank, {}).get('fid'):
                fids = (result.get('data') or {}).get('fids') or []
                if len(fids) != 1:
                    raise CloudError('UPLOAD_FORMAT')
                uploaded[rank] = dict(fid=fids[0], sha256=digest, name=path.name, size=path.stat().st_size)
                delivery['uploaded'] = uploaded
                checkpoint(delivery)
        if not delivery.get('share_url'):
            result, _ = self.run(['share', delivery['folder_fid'], '--url-type', '2', '--expired-type', '3', '--title', task['id'] + ' 成片'], work)
            data = result.get('data') or {}
            delivery.update(share_url=share_url(data.get('share_url', '')), passcode=data.get('passcode', ''), shared_at=time.time())
            if not delivery['passcode']:
                raise CloudError('PRIVATE_SHARE_FORMAT')
            checkpoint(delivery)
        return delivery
